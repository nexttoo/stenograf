"""HTTP-сервер: отдаёт интерфейс и JSON API (stdlib, без внешних зависимостей)."""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import asr, audio, config as C, dictation as D
from . import autostart as AUTOSTART
from . import overlay as OVL

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


# ------------------------------------------------------------------ задачи

MODEL_URL = (
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16/resolve/main/"
)
MODEL_FILES = ["encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"]


class JobManager:
    """Очередь задач распознавания (один рабочий поток)."""

    def __init__(self, engine: asr.Engine, get_cfg, history: C.History):
        self.engine = engine
        self.get_cfg = get_cfg
        self.history = history
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        threading.Thread(target=self._worker, daemon=True, name="jobs").start()

    def add(self, path: str, display_name: str | None = None) -> dict:
        job_id = uuid.uuid4().hex[:10]
        job = {
            "id": job_id,
            "name": display_name or os.path.basename(path),
            "path": path,
            "status": "queued",
            "progress": 0.0,
            "stage": "в очереди",
            "text": "",
            "segments": [],
            "duration": 0.0,
            "elapsed": 0.0,
            "rtf": 0.0,
            "error": None,
            "created": time.time(),
        }
        with self._lock:
            self._jobs[job_id] = job
            self._order.insert(0, job_id)
        self._q.put(job_id)
        return job

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(self._jobs[i]) for i in self._order if i in self._jobs]

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    def clear_finished(self):
        with self._lock:
            for i in list(self._order):
                j = self._jobs.get(i)
                if j and j["status"] in ("done", "error"):
                    self._jobs.pop(i, None)
                    self._order.remove(i)

    def remove(self, job_id: str):
        with self._lock:
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)

    def _worker(self):
        while True:
            job_id = self._q.get()
            with self._lock:
                job = self._jobs.get(job_id)
            if not job:
                continue
            try:
                self._run(job)
            except Exception as exc:  # pragma: no cover
                job["status"] = "error"
                job["error"] = str(exc)
                job["stage"] = "ошибка"
            finally:
                self._q.task_done()

    def _run(self, job: dict):
        cfg = self.get_cfg()
        t0 = time.time()
        job["status"] = "running"
        job["stage"] = "декодирование аудио"
        job["progress"] = 0.03

        samples, wav_path = audio.decode_to_16k(job["path"], C.data_dir() / "tmp")
        job["duration"] = round(len(samples) / 16000.0, 2)
        job["wav"] = wav_path
        job["stage"] = "распознавание"
        job["progress"] = 0.08

        def on_progress(done: int, total: int):
            if total:
                job["progress"] = 0.08 + 0.9 * (done / total)
                job["stage"] = f"распознавание {done}/{total}"

        text, segments, elapsed = self.engine.transcribe_long(
            samples, int(cfg.get("num_threads") or 4), on_progress
        )
        job["text"] = text
        job["segments"] = segments
        job["elapsed"] = round(elapsed, 2)
        job["rtf"] = round(elapsed / job["duration"], 3) if job["duration"] else 0
        job["progress"] = 1.0
        job["stage"] = "готово"
        job["status"] = "done"
        job["total_elapsed"] = round(time.time() - t0, 2)

        if cfg.get("save_history", True) and text:
            item = self.history.add(
                text=text, kind="file", source=job["name"],
                duration=job["duration"], segments=segments,
            )
            job["history_id"] = item["id"]

        # чистим временные копии, чтобы папка не разрасталась
        self._cleanup(job)

    @staticmethod
    def _cleanup(job: dict):
        if job.get("status") != "done":
            return
        for key in ("path", "wav"):
            p = job.get(key)
            if not p:
                continue
            try:
                path = Path(p)
                # исходник пользователя не трогаем — удаляем только наши копии
                if path.parent.name in ("uploads", "tmp") or ".16k" in path.name:
                    path.unlink(missing_ok=True)
            except Exception:
                pass


# ----------------------------------------------------------------- download


class Downloader:
    def __init__(self):
        self.state = {"status": "idle", "progress": 0.0, "stage": "", "error": None}
        self._thread: threading.Thread | None = None

    def start(self) -> dict:
        if self._thread and self._thread.is_alive():
            return self.state
        target = asr.model_dir()
        target.mkdir(parents=True, exist_ok=True)
        self.state = {"status": "running", "progress": 0.0, "stage": "подключение", "error": None}
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()
        return self.state

    def _run(self, target: Path):
        try:
            total_files = len(MODEL_FILES)
            for idx, name in enumerate(MODEL_FILES):
                url = MODEL_URL + name
                dest = target / name
                tmp = dest.with_suffix(dest.suffix + ".part")
                self.state["stage"] = f"загрузка {name}"
                with urllib.request.urlopen(url, timeout=60) as resp:
                    size = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    with open(tmp, "wb") as fh:
                        while True:
                            chunk = resp.read(1024 * 512)
                            if not chunk:
                                break
                            fh.write(chunk)
                            done += len(chunk)
                            frac = (done / size) if size else 0
                            self.state["progress"] = round((idx + frac) / total_files, 3)
                os.replace(tmp, dest)
            self.state.update({"status": "done", "progress": 1.0, "stage": "модель установлена"})
        except Exception as exc:
            self.state.update({"status": "error", "error": str(exc), "stage": "ошибка загрузки"})


# ------------------------------------------------------------------- стейт


class AppState:
    def __init__(self):
        self.cfg = C.load_config()
        self.history = C.History()
        self.engine = asr.Engine()
        self.jobs = JobManager(self.engine, lambda: self.cfg, self.history)
        self.downloader = Downloader()
        self.mic_test = False
        self.hotkey = D.KeyboardHook(self._on_key_down, self._on_key_up)
        self.window = None          # объект Window из main.py
        self.window_pid: int | None = None
        self.dictation = D.Dictation(
            self.engine, lambda: self.cfg, self._on_dictation_done,
            is_ui_foreground=self.ui_is_foreground,
        )
        self.hud = OVL.HUD(self.hud_state)
        self.dictation.on_state = self._on_dictation_state
        self.should_exit = False
        self.window_proc = None
        self.server: ThreadingHTTPServer | None = None
        self.on_tray_state = None
        self.on_tray_notify = None
        self.has_tray = False
        self._close_notified = False

    # -- жизненный цикл
    def start(self):
        self.hotkey.start()
        self.hotkey.bind(self.cfg.get("hotkey") or {})
        if self.cfg.get("widget", True):
            self.hud.start()
        threading.Thread(target=self._preload, daemon=True).start()

    def hud_state(self):
        """Состояние для плавающего индикатора (вызывается из его потока)."""
        if not self.cfg.get("widget", True):
            return ("idle", 0.0, 0.0, None)
        d = self.dictation
        if d.state == "recording":
            return ("recording", float(d.recorder.level), time.time() - d.started_at, d.error)
        if d.state == "working":
            return ("working", 0.0, 0.0, None)
        return ("idle", 0.0, 0.0, None)

    def _preload(self):
        if asr.model_status()["ready"]:
            self.engine.load(int(self.cfg.get("num_threads") or 4))
        try:
            self.dictation.recorder.warm(self.cfg)
        except Exception:
            pass

    def _on_key_down(self):
        self.dictation.on_key_down()

    def ui_is_foreground(self) -> bool:
        """Активно ли именно наше окно (а не чужое приложение).

        Проверяем и живость процесса окна: PID может быть переиспользован
        другой программой, и тогда текст ошибочно не вставился бы.
        """
        win = self.window
        try:
            if win is None or not win.is_alive():
                return False
            return D.foreground_pid() == win.pid
        except Exception:
            return False

    def _on_key_up(self):
        self.dictation.on_key_up()

    def _on_dictation_done(self, result: dict):
        text = (result or {}).get("text") or ""
        if text and self.cfg.get("save_history", True):
            self.history.add(
                text=text, kind="dictation", source="Микрофон",
                duration=float(result.get("duration") or 0),
            )

    def _on_dictation_state(self, state: str):
        if self.on_tray_state:
            try:
                self.on_tray_state(state)
            except Exception:
                pass


# -------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = f"Stenograf/{C.APP_VERSION}"
    state: AppState = None  # type: ignore[assignment]

    # -- утилиты
    def log_message(self, fmt, *args):  # тише в консоли
        pass

    def _send(self, code: int, data: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n else b""

    def _body_json(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -- GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path, qs)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._err("внутренняя ошибка", 500)

    # -- POST
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            return self._api_post(path, qs)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._err("внутренняя ошибка", 500)

    # -- статика
    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        web = C.res_dir() / "web"
        rel = path.lstrip("/").replace("\\", "/")
        if ".." in rel:
            return self._err("не найдено", 404)
        target = (web / rel).resolve()
        try:
            target.relative_to(web.resolve())
        except ValueError:
            return self._err("не найдено", 404)
        if not target.exists() or not target.is_file():
            return self._err("не найдено", 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # -- API GET
    def _api_get(self, path: str, qs: dict):
        st = self.state

        if path == "/api/bootstrap":
            cfg = st.cfg
            from . import dictation as DD

            return self._json({
                "ok": True,
                "version": C.APP_VERSION,
                "config": cfg,
                "hotkey_text": DD.describe_hotkey(cfg.get("hotkey") or {}),
                "hotkey_ok": st.hotkey.registered,
                "hotkey_error": st.hotkey.error,
                "model": asr.model_status(),
                "model_loaded": st.engine.ready,
                "model_error": st.engine.load_error,
                "load_seconds": st.engine.load_seconds,
                "devices": audio.list_input_devices(),
                "stats": st.history.stats(),
                "ffmpeg": C.ffmpeg_path() is not None,
                "data_dir": str(C.data_dir()),
                "platform": os.name,
                "autostart": bool(st.cfg.get("autostart", True)),
                "autostart_active": AUTOSTART.is_enabled(),
                "autostart_supported": AUTOSTART.is_supported(),
                "download": st.downloader.state,
            })

        if path == "/api/dictation":
            snap = st.dictation.snapshot()
            # уровень для теста микрофона
            if st.mic_test:
                snap["level"] = round(min(1.0, float(st.dictation.recorder.level) * 7), 3)
                snap["test"] = True
            return self._json({"ok": True, **snap})

        if path == "/api/devices":
            return self._json({"ok": True, "devices": audio.list_input_devices()})

        if path == "/api/jobs":
            return self._json({"ok": True, "jobs": st.jobs.list()})

        if path.startswith("/api/jobs/"):
            job = st.jobs.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._err("задача не найдена", 404)
            return self._json({"ok": True, "job": job})

        if path == "/api/history":
            q = (qs.get("q") or [""])[0]
            return self._json({
                "ok": True,
                "items": st.history.list(q),
                "stats": st.history.stats(),
            })

        if path.startswith("/api/history/") and path.endswith((".txt", ".srt")):
            name = path[len("/api/history/"):]
            item_id, ext = name.rsplit(".", 1)
            item = st.history.get(item_id)
            if not item:
                return self._err("запись не найдена", 404)
            if ext == "srt" and item.get("segments"):
                payload = asr.to_srt(item["segments"])
            else:
                payload = item.get("text") or ""
            fname = re.sub(r"[^\w\-.]+", "_", (item.get("source") or "текст"))[:40]
            cd = f'attachment; filename="{fname}.{ext}"; filename*=UTF-8\'\'{fname}.{ext}'
            return self._send(
                200,
                payload.encode("utf-8-sig"),
                "text/plain; charset=utf-8",
                {"Content-Disposition": cd},
            )

        if path == "/api/download-model":
            return self._json({"ok": True, "state": st.downloader.state})

        if path == "/api/ping":
            return self._json({"ok": True, "t": time.time()})

        return self._err("неизвестный запрос", 404)

    # -- API POST
    def _api_post(self, path: str, qs: dict):
        st = self.state

        if path == "/api/settings":
            patch = self._body_json()
            cfg = {**st.cfg}
            for k, v in patch.items():
                if k in C.DEFAULTS or k == "hotkey":
                    cfg[k] = v
            st.cfg = C.save_config(cfg)
            if "hotkey" in patch or "mode" in patch:
                st.hotkey.bind(st.cfg.get("hotkey") or {})
            return self._json({
                "ok": True,
                "config": st.cfg,
                "hotkey_text": D.describe_hotkey(st.cfg.get("hotkey") or {}),
                "hotkey_ok": st.hotkey.registered,
                "hotkey_error": st.hotkey.error,
            })

        if path == "/api/ui-focus":
            data = self._body_json()
            st.dictation.ui_focused = bool(data.get("focused"))
            return self._json({"ok": True})

        if path == "/api/hotkey-capture":
            data = self._body_json()
            st.hotkey.paused = bool(data.get("active"))
            return self._json({"ok": True, "paused": st.hotkey.paused})

        if path == "/api/insert-test":
            # Проверка вставки: даём время переключиться в Блокнот, затем пробуем
            # отправить Ctrl+V и, если Windows не приняла, — напечатать символы.
            data = self._body_json()
            delay = min(10.0, max(0.0, float(data.get("delay") or 3.0)))
            text = str(data.get("text") or "Стенограф — проверка вставки.")
            time.sleep(delay)
            previous = D.get_clipboard_text() if st.cfg.get("restore_clipboard", True) else None
            clip_ok = D.set_clipboard_text(text)
            time.sleep(0.06)
            paste_events = D.paste_from_clipboard()
            typed_events = 0
            if not paste_events:
                typed_events = D.type_text(text)
            if not paste_events and not typed_events and previous is not None:
                D.set_clipboard_text(text)  # ввод заблокирован — оставим текст в буфере
            return self._json({
                "ok": True,
                "clipboard": bool(clip_ok),
                "paste_events": int(paste_events),
                "typed_events": int(typed_events),
                "text": text,
            })

        if path == "/api/dictation/start":
            ok = st.dictation.start("ui")
            return self._json({"ok": ok, "error": st.dictation.error})

        if path == "/api/dictation/stop":
            st.dictation.stop("ui")
            return self._json({"ok": True})

        if path == "/api/mic-test/start":
            ok = st.dictation.recorder.start(st.cfg)
            st.mic_test = bool(ok)
            return self._json({"ok": ok, "error": st.dictation.recorder.error})

        if path == "/api/mic-test/stop":
            st.mic_test = False
            st.dictation.recorder.stop()
            return self._json({"ok": True})

        if path == "/api/transcribe":
            name = (qs.get("name") or ["audio"])[0]
            raw = self._body()
            if not raw:
                return self._err("пустой файл")
            tmp_dir = C.data_dir() / "uploads"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w\-. ()\[\]]+", "_", Path(name).name)[:120] or "audio.bin"
            dest = tmp_dir / f"{uuid.uuid4().hex[:6]}_{safe}"
            dest.write_bytes(raw)
            job = st.jobs.add(str(dest), display_name=safe)
            return self._json({"ok": True, "job": job})

        if path == "/api/jobs/clear":
            st.jobs.clear_finished()
            return self._json({"ok": True})

        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path.split("/")[3]
            st.jobs.remove(job_id)
            return self._json({"ok": True})

        if path == "/api/history/clear":
            st.history.clear()
            return self._json({"ok": True})

        if path.startswith("/api/history/") and path.endswith("/delete"):
            item_id = path.split("/")[3]
            return self._json({"ok": st.history.delete(item_id)})

        if path == "/api/copy":
            data = self._body_json()
            ok = D.set_clipboard_text(str(data.get("text") or ""))
            return self._json({"ok": ok})

        if path == "/api/autostart":
            data = self._body_json()
            enabled = bool(data.get("enabled"))
            changed = AUTOSTART.enable() if enabled else AUTOSTART.disable()
            if changed:
                st.cfg = C.save_config({**st.cfg, "autostart": enabled})
            return self._json({
                "ok": changed,
                "enabled": bool(st.cfg.get("autostart", True)),
                "active": AUTOSTART.is_enabled(),
                "supported": AUTOSTART.is_supported(),
            })

        if path == "/api/insert":
            data = self._body_json()
            text = str(data.get("text") or "")
            if not text:
                return self._err("нет текста")
            how = D.insert_text(text, st.cfg)
            return self._json({"ok": True, "how": how})

        if path == "/api/open-folder":
            data = self._body_json()
            which = data.get("which") or "data"
            target = C.data_dir() if which == "data" else C.data_dir() / which
            target.mkdir(parents=True, exist_ok=True)
            audio.open_folder(target)
            return self._json({"ok": True, "path": str(target)})

        if path == "/api/model/download":
            return self._json({"ok": True, "state": st.downloader.start()})

        if path == "/api/stats/reset":
            st.history.clear()
            return self._json({"ok": True, "stats": st.history.stats()})

        if path == "/api/shutdown":
            st.should_exit = True
            threading.Thread(target=self._shutdown_later, daemon=True).start()
            return self._json({"ok": True})

        return self._err("неизвестный запрос", 404)

    def _shutdown_later(self):
        time.sleep(0.3)
        try:
            self.state.server and self.state.server.shutdown()
        except Exception:
            pass


# --------------------------------------------------------------- запуск


def make_server(state: AppState, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    state.server = httpd
    return httpd
