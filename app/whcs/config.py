"""Стенограф — локальная диктовка и расшифровка (RU). Конфигурация и пути."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

APP_NAME = "Стенограф"
APP_VERSION = "1.0.1"

_lock = threading.RLock()


def res_dir() -> Path:
    """Каталог со статикой (web/), внутри сборки — _internal."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Каталог установки: рядом с exe (портативно) или корень проекта."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def _first_existing(paths) -> Path | None:
    for p in paths:
        p = Path(p)
        if p.exists():
            return p
    return None


def models_dir() -> Path:
    found = _first_existing(
        [
            app_dir() / "models",
            res_dir() / "models",
            app_dir().parent / "models",
        ]
    )
    return found or (app_dir() / "models")


def ffmpeg_path() -> str | None:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found = _first_existing(
        [
            app_dir() / "tools" / exe,
            app_dir() / exe,
            res_dir() / "tools" / exe,
        ]
    )
    if found:
        return str(found)
    from shutil import which

    return which(exe)


def data_dir() -> Path:
    """Пользовательские данные: история, записи, конфиг."""
    base = app_dir() / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
        test = base / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return base
    except Exception:
        fallback = Path(os.environ.get("APPDATA", str(Path.home()))) / "Stenograf"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


CONFIG_PATH = data_dir() / "config.json"
HISTORY_PATH = data_dir() / "history.json"
RECORDINGS_DIR = data_dir() / "recordings"

DEFAULTS: dict = {
    "mic_device": None,          # индекс или имя устройства
    "hotkey": {"mods": ["ctrl"], "key": "space"},
    "mode": "hold",              # hold | toggle
    "insert_mode": "clipboard",  # clipboard | type | none
    "restore_clipboard": True,
    "restore_delay_ms": 350,
    "keep_in_clipboard": False,
    "sound_cues": True,
    "widget": True,
    "theme": "dark",
    "num_threads": 4,
    "save_history": True,
    "minimize_to_tray": True,
}


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    with _lock:
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return _merge(DEFAULTS, raw)
            except Exception:
                pass
        return dict(DEFAULTS)


def save_config(cfg: dict) -> dict:
    with _lock:
        merged = _merge(DEFAULTS, cfg)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        return merged


# ---------------------------------------------------------------- история


class History:
    """Простая история расшифровок с ограничением по количеству."""

    LIMIT = 500

    def __init__(self, path: Path = HISTORY_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._items = data
            except Exception:
                self._items = []

    def _flush(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def add(self, *, text: str, kind: str, source: str,
            duration: float = 0.0, segments: list | None = None) -> dict:
        words = len(text.split()) if text else 0
        item = {
            "id": uuid.uuid4().hex[:12],
            "created": time.time(),
            "kind": kind,
            "source": source,
            "text": text,
            "words": words,
            "duration": round(duration, 2),
            "chars": len(text or ""),
        }
        if segments:
            item["segments"] = segments
        with self._lock:
            self._items.insert(0, item)
            del self._items[self.LIMIT:]
            self._flush()
        return item

    def list(self, query: str = "") -> list[dict]:
        with self._lock:
            items = list(self._items)
        if query:
            q = query.lower()
            items = [
                i for i in items
                if q in (i.get("text") or "").lower() or q in (i.get("source") or "").lower()
            ]
        return items

    def get(self, item_id: str) -> dict | None:
        for i in self.list():
            if i["id"] == item_id:
                return i
        return None

    def delete(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != item_id]
            changed = len(self._items) != before
            if changed:
                self._flush()
            return changed

    def clear(self):
        with self._lock:
            self._items = []
            self._flush()

    def stats(self) -> dict:
        items = self.list()
        today = time.strftime("%Y-%m-%d")
        words_today = sum(
            i.get("words", 0) for i in items
            if time.strftime("%Y-%m-%d", time.localtime(i.get("created", 0))) == today
        )
        total_words = sum(i.get("words", 0) for i in items)
        total_sec = sum(i.get("duration", 0) for i in items)
        wpm = (total_words / (total_sec / 60)) if total_sec > 5 else 0
        return {
            "items": len(items),
            "words_today": words_today,
            "words_total": total_words,
            "minutes": round(total_sec / 60, 1),
            "wpm": int(round(wpm)),
        }
