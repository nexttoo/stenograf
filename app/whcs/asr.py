"""Локальный движок распознавания речи (GigaAM v3, sherpa-onnx)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import numpy as np

from . import config as C

MODEL_SUBDIR = "gigaam-v3-punct"
REQUIRED = ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")


def model_dir() -> Path:
    env = os.environ.get("STENOGRAF_MODEL_DIR")
    if env:
        return Path(env)
    return C.models_dir() / MODEL_SUBDIR


def model_status() -> dict:
    d = model_dir()
    missing = [f for f in REQUIRED if not (d / f).exists()]
    return {
        "dir": str(d),
        "ready": not missing,
        "missing": missing,
        "size_mb": round(
            sum((d / f).stat().st_size for f in REQUIRED if (d / f).exists()) / 1_048_576, 1
        ),
    }


# --------------------------------------------------------------- сегменты


def _frame_rms(samples: np.ndarray, sr: int, win: float = 0.025, hop: float = 0.010):
    w = max(1, int(sr * win))
    h = max(1, int(sr * hop))
    if len(samples) < w:
        return np.array([float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0]), hop
    n = 1 + (len(samples) - w) // h
    # через stride, чтобы не копировать слишком много
    strides = (samples.strides[0] * h, samples.strides[0])
    frames = np.lib.stride_tricks.as_strided(samples, shape=(n, w), strides=strides)
    return np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1)), hop


def plan_segments(samples: np.ndarray, sr: int = 16000,
                  target: float = 22.0, max_len: float = 30.0) -> list[tuple[int, int]]:
    """Разбивает длинную запись на куски по паузам. Возвращает (start, end) в сэмплах."""
    total = len(samples)
    dur = total / sr
    if dur <= max_len:
        return [(0, total)]

    rms, hop = _frame_rms(samples, sr)
    if len(rms) < 3:
        return [(0, total)]

    noise = float(np.percentile(rms, 20))
    loud = float(np.percentile(rms, 92))
    thr = max(noise * 2.4, loud * 0.06, 0.0035)
    voiced = rms > thr

    # границы пауз (в секундах)
    pauses: list[tuple[float, float]] = []
    i = 0
    n = len(voiced)
    while i < n:
        if not voiced[i]:
            j = i
            while j < n and not voiced[j]:
                j += 1
            pauses.append((i * hop, j * hop))
            i = j
        else:
            i += 1

    # накапливаем куски до target секунд, режем по самой длинной паузе рядом с целью
    cuts: list[int] = []
    pos = 0.0
    while dur - pos > target:
        want = pos + target
        best = None
        for (s, e) in pauses:
            mid = (s + e) / 2.0
            if pos + 3.0 < mid < min(want + 6.0, dur - 1.0):
                score = abs(mid - want) - (e - s) * 0.8
                if best is None or score < best[0]:
                    best = (score, mid)
        if best is None:
            cut = want
        else:
            cut = best[1]
        cuts.append(int(cut * sr))
        pos = cut

    bounds = [0] + cuts + [total]
    out: list[tuple[int, int]] = []
    pad = int(0.12 * sr)
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg_len = b - a
        if seg_len <= 0:
            continue
        # страховка: слишком длинные непрерывные куски режем жёстко
        if seg_len > max_len * sr:
            step = int(max_len * sr)
            p = a
            while p < b:
                q = min(p + step, b)
                out.append((max(0, p - pad), min(total, q + pad)))
                p = q
        else:
            out.append((max(0, a - pad), min(total, b + pad)))
    return out or [(0, total)]


# ------------------------------------------------------------- движок


class Engine:
    def __init__(self):
        self._rec = None
        self._lock = threading.RLock()
        self.load_error: str | None = None
        self.load_seconds: float = 0.0

    @property
    def ready(self) -> bool:
        return self._rec is not None

    def load(self, num_threads: int = 4) -> bool:
        with self._lock:
            if self._rec is not None:
                return True
            st = model_status()
            if not st["ready"]:
                self.load_error = "Модель не найдена: " + ", ".join(st["missing"])
                return False
            t0 = time.time()
            try:
                import sherpa_onnx

                d = model_dir()
                self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(d / "encoder.int8.onnx"),
                    decoder=str(d / "decoder.onnx"),
                    joiner=str(d / "joiner.onnx"),
                    tokens=str(d / "tokens.txt"),
                    num_threads=max(1, int(num_threads)),
                    sample_rate=16000,
                    feature_dim=64,
                    decoding_method="greedy_search",
                    model_type="nemo_transducer",
                )
                self.load_seconds = round(time.time() - t0, 2)
                self.load_error = None
                return True
            except Exception as exc:  # pragma: no cover
                self.load_error = f"Не удалось загрузить модель: {exc}"
                self._rec = None
                return False

    def transcribe(self, samples: np.ndarray, num_threads: int = 4) -> str:
        if not self.load(num_threads):
            raise RuntimeError(self.load_error or "модель недоступна")
        assert self._rec is not None
        s = self._rec.create_stream()
        s.accept_waveform(16000, samples.astype(np.float32))
        with self._lock:
            self._rec.decode_stream(s)
        return (s.result.text or "").strip()

    def transcribe_long(self, samples: np.ndarray, num_threads: int = 4,
                        on_progress=None) -> tuple[str, list[dict], float]:
        """Длинная запись: разбиение по паузам + пофрагментное распознавание."""
        if len(samples) == 0:
            return "", [], 0.0
        pieces = plan_segments(samples)
        t0 = time.time()
        texts: list[str] = []
        segs: list[dict] = []
        for idx, (a, b) in enumerate(pieces):
            chunk = samples[a:b]
            if len(chunk) < 1600:  # < 0.1 c
                continue
            txt = self.transcribe(chunk, num_threads)
            if txt:
                texts.append(txt)
                segs.append(
                    {
                        "start": round(a / 16000, 2),
                        "end": round(b / 16000, 2),
                        "text": txt,
                    }
                )
            if on_progress:
                on_progress(idx + 1, len(pieces))
        elapsed = time.time() - t0
        return clean_text(" ".join(texts)), segs, elapsed


# --------------------------------------------------------- постобработка


import re as _re

_MULTISPACE = _re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = _re.compile(r"\s+([,.;:!?…»])")
_SPACE_AFTER_OPEN = _re.compile(r"([«(])\s+")
_LEADING_JUNK = _re.compile(r"^[\-–,\s]+")
_DASH_AFTER_POINT = _re.compile(r"(?<=[.!?…])\s+[-–]\s*([А-Яа-яЁёA-Za-z])")


def _drop_stray_dash(m: "_re.Match") -> str:
    return " " + m.group(1).upper()


def clean_text(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\u2581", " ")
    t = _MULTISPACE.sub(" ", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = _SPACE_AFTER_OPEN.sub(r"\1", t)
    t = t.strip(" -")
    # типичные артефакты распознавания
    t = _re.sub(r"\b(э+|мм+|аа+)\b[,.]?\s*", "", t, flags=_re.IGNORECASE) if len(t) > 6 else t
    # лишний дефис, который модель ставит на паузе в начале фразы или после точки,
    # при этом законное тире в диалоге «— Привет» и «цена - 100» не трогаем
    t = _LEADING_JUNK.sub("", t)
    t = _DASH_AFTER_POINT.sub(_drop_stray_dash, t)
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    t = t.rstrip(" ,;:—–-")
    if t and t[-1] not in ".!?…":
        t += "."
    return t


def to_srt(segments: list[dict]) -> str:
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    out = []
    for i, seg in enumerate(segments, 1):
        out.append(f"{i}\n{ts(seg['start'])} --> {ts(seg['end'])}\n{seg['text']}\n")
    return "\n".join(out)
