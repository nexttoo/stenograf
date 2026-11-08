"""Точка входа Стенограф: сервер, окно, трей."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from whcs import config as C
from whcs import server as S


def setup_logging():
    """В оконной сборке stdout отсутствует — пишем в файл рядом с данными."""
    try:
        log = C.data_dir() / "stenograf.log"
        if log.exists() and log.stat().st_size > 2_000_000:
            log.unlink(missing_ok=True)
        fh = open(log, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = fh
        if sys.stderr is None:
            sys.stderr = fh
        print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} запуск {C.APP_VERSION} =====", file=fh)
    except Exception:
        pass


def find_browser() -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


class Window:
    """Окно приложения в режиме app-окна Edge/Chrome."""

    def __init__(self, url: str, on_close=None, on_open=None):
        self.url = url
        self.proc: subprocess.Popen | None = None
        self.on_close = on_close
        self.on_open = on_open

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def is_alive(self) -> bool:
        """Окно реально живо? Нужен, чтобы не спутать его с чужим процессом с тем же PID."""
        return bool(self.proc and self.proc.poll() is None)

    def open(self) -> bool:
        exe = find_browser()
        if not exe:
            try:
                import webbrowser

                webbrowser.open(self.url)
                return True
            except Exception:
                return False
        profile = C.data_dir() / "window-profile"
        profile.mkdir(parents=True, exist_ok=True)
        args = [
            exe,
            f"--app={self.url}",
            "--window-size=1240,840",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-features=Translate,MediaRouter",
            "--disable-background-networking",
            "--no-service-autorun",
            "--disable-sync",
        ]
        try:
            self.proc = subprocess.Popen(args, close_fds=True)
        except Exception:
            try:
                import webbrowser

                webbrowser.open(self.url)
                return True
            except Exception:
                return False
        if self.on_open:
            try:
                self.on_open(self.proc.pid)
            except Exception:
                pass
        threading.Thread(target=self._watch, daemon=True).start()
        return True

    def _watch(self):
        if not self.proc:
            return
        self.proc.wait()
        if self.on_close:
            self.on_close()

    def focus(self):
        if self.proc and self.proc.poll() is None:
            return False
        return self.open()

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


def make_tray(state: S.AppState, window: Window, on_quit):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return None

    def icon_image(recording: bool):
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        color = (239, 68, 68, 255) if recording else (52, 211, 153, 255)
        d.rounded_rectangle([2, 2, size - 3, size - 3], radius=16, fill=(16, 24, 22, 255))
        d.rounded_rectangle([2, 2, size - 3, size - 3], radius=16, outline=color, width=3)
        # микрофон
        d.rounded_rectangle([26, 14, 38, 36], radius=6, fill=color)
        d.arc([20, 26, 44, 48], start=0, end=180, fill=color, width=3)
        d.line([32, 44, 32, 52], fill=color, width=3)
        d.line([24, 52, 40, 52], fill=color, width=3)
        return img

    def show_window(icon=None, item=None):
        window.focus()

    def notify(text: str, title: str = "Стенограф"):
        try:
            icon.notify(text, title)
        except Exception:
            pass

    def toggle_dictation(icon=None, item=None):
        state.dictation.on_hotkey()

    def quit_app(icon=None, item=None):
        state.should_exit = True
        try:
            icon.stop()
        except Exception:
            pass
        window.close()
        on_quit()
    menu = pystray.Menu(
        pystray.MenuItem("Открыть Стенограф", show_window, default=True),
        pystray.MenuItem("Диктовка (старт/стоп)", toggle_dictation),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    )
    icon = pystray.Icon("Stenograf", icon_image(False), "Стенограф — диктовка готова", menu)

    def set_state(st: str):
        try:
            icon.icon = icon_image(st == "recording")
            icon.title = {
                "recording": "Стенограф — идёт запись…",
                "working": "Стенограф — распознавание…",
            }.get(st, "Стенограф — готово")
        except Exception:
            pass

    state.on_tray_state = set_state
    state.on_tray_notify = notify
    return icon


def single_instance_port() -> int | None:
    """Если приложение уже запущено — вернуть его порт."""
    f = C.data_dir() / "instance.json"
    if not f.exists():
        return None
    try:
        port = int(json.loads(f.read_text(encoding="utf-8"))["port"])
    except Exception:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.2) as r:
            if r.status == 200:
                return port
    except Exception:
        return None
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Стенограф")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--allow-multi", action="store_true")
    args = ap.parse_args(argv)

    setup_logging()

    if not args.allow_multi:
        existing = single_instance_port()
        if existing:
            url = f"http://127.0.0.1:{existing}/"
            if not args.no_window:
                Window(url).open()
            return 0

    state = S.AppState()
    httpd = S.make_server(state, args.port)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    (C.data_dir() / "instance.json").write_text(
        json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8"
    )

    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    state.start()

    window = Window(
        url,
        on_close=lambda: _on_window_close(state),
        on_open=lambda pid: setattr(state, "window_pid", pid),
    )
    state.window = window
    if not args.no_window:
        window.open()

    def shutdown():
        state.should_exit = True
        try:
            state.hotkey.stop()
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            (C.data_dir() / "instance.json").unlink(missing_ok=True)
        except Exception:
            pass

    tray = None if args.no_tray else make_tray(state, window, shutdown)
    state.has_tray = tray is not None

    if tray is not None:
        # сторож: выход может быть запрошен и из интерфейса, и из трея
        def watch_exit():
            while not state.should_exit:
                time.sleep(0.3)
            try:
                tray.stop()
            except Exception:
                pass
            window.close()

        threading.Thread(target=watch_exit, daemon=True, name="exit-watch").start()
        tray.run()
        shutdown()
        return 0

    while not state.should_exit:
        time.sleep(0.25)
    shutdown()
    return 0


def _on_window_close(state: S.AppState):
    # окно закрыто: наше приложение больше не может быть в фокусе
    state.window_pid = None
    state.dictation.ui_focused = False
    if state.should_exit:
        return
    if not state.cfg.get("minimize_to_tray", True) or not state.has_tray:
        # трея нет — закрываемся полностью, чтобы не оставлять невидимый процесс
        state.should_exit = True
        return
    if state.on_tray_state:
        try:
            state.on_tray_state(state.dictation.state)
        except Exception:
            pass
    if state.on_tray_notify and not state._close_notified:
        state._close_notified = True
        try:
            state.on_tray_notify(
                "Программа работает в фоне: значок в области уведомлений "
                "(стрелка ^ рядом с часами). Диктовка по Ctrl+Space работает."
            )
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
