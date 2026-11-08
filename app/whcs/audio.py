"""Аудио: декодирование файлов, запись с микрофона."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from . import config as C

TARGET_SR = 16000
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ------------------------------------------------------------------ WAV


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Читает PCM WAV -> (float32 моно, sample_rate)."""
    with wave.open(str(path), "rb") as w:
        n_ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"неподдерживаемая разрядность WAV: {sw} байт")
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    return data.astype(np.float32), sr


def write_wav(path: str | Path, samples: np.ndarray, sr: int = TARGET_SR):
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def duration_of(path: str | Path) -> float:
    try:
        data, sr = read_wav(path)
        return len(data) / sr
    except Exception:
        return 0.0


# ------------------------------------------------------- декодирование


AUDIO_EXT = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".amr",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".3gp",
}


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
        timeout=timeout,
        check=False,
    )


def decode_to_16k(path: str | Path, out_dir: str | Path | None = None) -> tuple[np.ndarray, str]:
    """Приводит любой аудио/видео файл к моно 16 кГц float32.

    Возвращает (samples, путь_к_временному_wav). Временный файл переиспользуется
    для проигрывания в интерфейсе.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"файл не найден: {path}")

    # быстрый путь: уже нужный WAV
    try:
        data, sr = read_wav(path)
        if sr == TARGET_SR:
            return data, str(path)
    except Exception:
        pass

    tmp_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="whcs_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = tmp_dir / (path.stem[:60] + ".16k.wav")

    ff = C.ffmpeg_path()
    if ff:
        cmd = [
            ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", str(path), "-vn", "-ac", "1", "-ar", str(TARGET_SR),
            "-acodec", "pcm_s16le", str(out),
        ]
        r = _run(cmd)
        if r.returncode != 0 or not out.exists():
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"ffmpeg не смог прочитать файл: {err[:400] or 'неизвестная ошибка'}")
        data, sr = read_wav(out)
        return data, str(out)

    # запасной путь без ffmpeg
    try:
        import soundfile as sf  # type: ignore

        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != TARGET_SR:
            raise RuntimeError("формат требует ffmpeg для передискретизации")
        write_wav(out, data)
        return data.astype(np.float32), str(out)
    except ImportError:
        raise RuntimeError(
            "Для этого формата нужен ffmpeg.exe рядом с программой (папка tools)."
        )


# ----------------------------------------------------------- микрофон


def list_input_devices() -> list[dict]:
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover
        return [{"index": -1, "name": f"нет доступа к звуку: {exc}", "default": False}]

    out: list[dict] = []
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1
    try:
        hostapis = sd.query_hostapis()
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) < 1:
                continue
            api = ""
            try:
                api = hostapis[dev["hostapi"]]["name"]
            except Exception:
                pass
            out.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "api": api,
                    "channels": dev["max_input_channels"],
                    "sr": int(dev.get("default_samplerate") or 0),
                    "default": idx == default_in,
                }
            )
    except Exception:
        pass
    # WASAPI-устройства первыми, затем остальные
    out.sort(key=lambda d: (0 if "WASAPI" in d["api"] else 1, d["name"].lower()))
    return out


class Recorder:
    """Запись с микрофона. Поток держится «тёплым» недолго после диктовки,
    чтобы старт следующей записи был мгновенным и не терялся первый слог."""

    WARM_SECONDS = 25

    def __init__(self):
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.RLock()
        self._capturing = False
        self._device_used = "__none__"
        self._warm_timer: threading.Timer | None = None
        self.level = 0.0
        self.peak = 0.0
        self.samplerate = TARGET_SR
        self.error: str | None = None
        self.active = False

    # -- внутреннее
    def _callback(self, indata, frames, time_info, status):  # noqa: ANN001
        if not self._capturing:
            return
        block = indata.copy()
        if block.ndim > 1:
            block = block.mean(axis=1)
        block = block.astype(np.float32)
        with self._lock:
            self._chunks.append(block)
        rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
        self.level = max(rms, self.level * 0.72)
        self.peak = max(self.peak, rms)

    def _device(self, cfg: dict):
        """Индекс выбранного устройства или None (системное по умолчанию)."""
        import sounddevice as sd

        dev = cfg.get("mic_device")
        if dev is None or dev == "":
            return None
        if isinstance(dev, int):
            return dev
        try:
            for idx, d in enumerate(sd.query_devices()):
                if d["name"] == dev and d.get("max_input_channels", 0) > 0:
                    return idx
        except Exception:
            pass
        return None

    @staticmethod
    def _rates_for(device) -> list[int]:
        """Сначала 16 кГц (без пересчёта), затем родная частота устройства."""
        import sounddevice as sd

        rates: list[int] = [TARGET_SR]
        try:
            info = sd.query_devices(device, "input")
            native = int(info.get("default_samplerate") or 0)
            if native and native not in rates:
                rates.append(native)
        except Exception:
            pass
        for extra in (48000, 44100):
            if extra not in rates:
                rates.append(extra)
        return rates

    def warm(self, cfg: dict) -> bool:
        """Открывает поток заранее, чтобы первая диктовка началась мгновенно."""
        if self._stream is not None and getattr(self._stream, "active", False):
            self._schedule_warm_close()
            return True
        with self._lock:
            self._capturing = False
        ok = self._open(cfg)
        if ok:
            with self._lock:
                self._capturing = False
        self._schedule_warm_close()
        return ok

    def _open(self, cfg: dict) -> bool:
        import sounddevice as sd

        device = self._device(cfg)
        self._close_stream()
        with self._lock:
            self._chunks = []
        self.level = 0.0
        self.peak = 0.0
        self.error = None

        # если выбранное устройство капризничает — пробуем системное по умолчанию
        attempts = [device] if device is None else [device, None]
        last_err = None
        for dev in attempts:
            for rate in self._rates_for(dev):
                try:
                    stream = sd.InputStream(
                        samplerate=rate,
                        channels=1,
                        dtype="float32",
                        device=dev,
                        blocksize=0,
                        callback=self._callback,
                    )
                    stream.start()
                    with self._lock:
                        self._stream = stream
                        self._device_used = device
                    self.samplerate = rate
                    self.active = True
                    return True
                except Exception as exc:
                    last_err = exc
                    continue
        self.error = f"не удалось открыть микрофон: {last_err}" if last_err else "не удалось открыть микрофон"
        self.active = False
        return False

    def _cancel_warm(self):
        if self._warm_timer is not None:
            self._warm_timer.cancel()
            self._warm_timer = None

    def _schedule_warm_close(self):
        self._cancel_warm()
        t = threading.Timer(self.WARM_SECONDS, self._close_stream)
        t.daemon = True
        t.start()
        self._warm_timer = t

    def _close_stream(self):
        with self._lock:
            if self._capturing:
                return
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def start(self, cfg: dict) -> bool:
        device = self._device(cfg)
        with self._lock:
            warm_ok = (
                self._stream is not None
                and getattr(self._stream, "active", False)
                and self._device_used == device
            )
            if warm_ok:
                self._chunks = []
                self.level = 0.0
                self.peak = 0.0
                self.error = None
                self._capturing = True
                self.active = True
                self._cancel_warm()
                return True

        if not self._open(cfg):
            return False
        with self._lock:
            self._chunks = []
            self._capturing = True
        self._cancel_warm()
        return True

    def stop(self, silent: bool = False) -> np.ndarray:
        with self._lock:
            self._capturing = False
            chunks, self._chunks = self._chunks, []
        self.active = False
        self._schedule_warm_close()
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        data = np.concatenate(chunks).astype(np.float32)
        if self.samplerate != TARGET_SR and len(data):
            data = _resample_to_16k(data, self.samplerate)
        return data

    def shutdown(self):
        self._cancel_warm()
        self._close_stream()


def _resample_to_16k(data: np.ndarray, sr: int) -> np.ndarray:
    """Передискретизация: ffmpeg, при его отсутствии — линейная интерполяция."""
    ff = C.ffmpeg_path()
    if ff and len(data) > 0:
        tmp = Path(tempfile.mkdtemp(prefix="whcs_rs_"))
        src = tmp / "in.wav"
        dst = tmp / "out.wav"
        write_wav(src, data, sr)
        r = _run(
            [
                ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                "-i", str(src), "-ac", "1", "-ar", str(TARGET_SR),
                "-acodec", "pcm_s16le", str(dst),
            ]
        )
        if r.returncode == 0 and dst.exists():
            out, _ = read_wav(dst)
            return out
    n_out = int(round(len(data) * TARGET_SR / sr))
    if n_out <= 1:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=len(data), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, data).astype(np.float32)


# ------------------------------------------------------------- сигналы


def beep(kind: str = "start"):
    """Короткий звуковой сигнал без внешних зависимостей (Windows)."""
    try:
        import winsound

        if kind == "start":
            winsound.Beep(880, 90)
        elif kind == "stop":
            winsound.Beep(620, 90)
        elif kind == "error":
            winsound.Beep(320, 220)
    except Exception:
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass


def open_folder(path: str | Path):
    path = str(path)
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass
