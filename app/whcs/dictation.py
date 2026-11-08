"""Диктовка: глобальная горячая клавиша, запись, вставка текста в активное окно."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

from . import audio, asr

IS_WINDOWS = os.name == "nt"


def _log(message: str):
    """Короткий журнал диктовки — попадает в data/stenograf.log."""
    try:
        print(f"[dictation] {message}", file=sys.stderr, flush=True)
    except Exception:
        pass

# ------------------------------------------------------------ константы

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_APP_REBIND = 0x0400 + 17
WM_QUIT = 0x0012

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
VK_CONTROL = 0x11
VK_V = 0x56

MODS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}

KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "`": 0xC0, "minus": 0xBD, "equal": 0xBB,
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEYS[_c] = ord(_c.upper())
for _d in "0123456789":
    KEYS[_d] = ord(_d)


# --------------------------------------------------------------- WinAPI

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
    user32.RegisterHotKey.restype = wt.BOOL
    user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wt.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = wt.BOOL
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    user32.PeekMessageW.restype = wt.BOOL
    user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostThreadMessageW.restype = wt.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.OpenClipboard.restype = wt.BOOL
    user32.CloseClipboard.restype = wt.BOOL
    user32.EmptyClipboard.restype = wt.BOOL
    user32.GetClipboardData.argtypes = [wt.UINT]
    user32.GetClipboardData.restype = wt.HANDLE
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    user32.SetClipboardData.restype = wt.HANDLE

    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wt.HANDLE
    kernel32.GlobalLock.argtypes = [wt.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wt.HANDLE]
    kernel32.GlobalUnlock.restype = wt.BOOL
    kernel32.GlobalFree.argtypes = [wt.HANDLE]
    kernel32.GlobalFree.restype = wt.HANDLE

    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wt.UINT
else:  # pragma: no cover
    user32 = None
    kernel32 = None


def combo_to_hotkey(hotkey: dict):
    mods = 0
    for m in hotkey.get("mods") or []:
        mods |= MODS.get(str(m).lower(), 0)
    key = KEYS.get(str(hotkey.get("key") or "").lower())
    if key is None:
        return None, None
    return mods | MOD_NOREPEAT, key


def describe_hotkey(hotkey: dict) -> str:
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    mods = [names.get(str(m).lower(), str(m)) for m in (hotkey.get("mods") or [])]
    raw = str(hotkey.get("key") or "")
    key = raw.upper()
    if raw.lower() == "space":
        key = "Space"
    return " + ".join(mods + [key]) if mods else key


# --------------------------------------------------------------- ввод


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send_inputs(inputs: list) -> None:
    if not IS_WINDOWS or not inputs:
        return
    arr = (_INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), ctypes.byref(arr), ctypes.sizeof(_INPUT))


def _key(vk: int, up: bool = False) -> _INPUT:
    return _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                                     time=0, dwExtraInfo=0)),
    )


def _unicode_char(ch: str) -> list:
    out = []
    raw = ch.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        code = int.from_bytes(raw[i:i + 2], "little")
        out.append(_INPUT(
            type=INPUT_KEYBOARD,
            u=_INPUTUNION(ki=_KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE,
                                         time=0, dwExtraInfo=0)),
        ))
    return out


def get_clipboard_text() -> str | None:
    if not IS_WINDOWS:
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not IS_WINDOWS:
        return False
    data = (text + "\0").encode("utf-16-le")
    opened = False
    for _ in range(15):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.03)
    if not opened:
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


def paste_from_clipboard() -> bool:
    if not IS_WINDOWS:
        return False
    _send_inputs([_key(VK_CONTROL), _key(VK_V), _key(VK_V, True), _key(VK_CONTROL, True)])
    return True


def type_text(text: str) -> bool:
    if not IS_WINDOWS:
        return False
    batch: list = []
    for ch in text:
        if ch == "\n":
            batch.extend([_key(0x0D), _key(0x0D, True)])
        elif ch == "\t":
            batch.extend([_key(0x09), _key(0x09, True)])
        else:
            batch.extend(_unicode_char(ch))
        if len(batch) >= 96:
            _send_inputs(batch)
            batch = []
            time.sleep(0.004)
    if batch:
        _send_inputs(batch)
    return True


def insert_text(text: str, cfg: dict) -> str:
    """Вставляет текст в активное окно. Возвращает выполненный способ."""
    if not text:
        return "none"
    mode = cfg.get("insert_mode") or "clipboard"

    if mode == "type":
        type_text(text)
        return "type"

    if mode == "none":
        set_clipboard_text(text)
        return "clipboard"

    previous = get_clipboard_text() if cfg.get("restore_clipboard", True) else None
    if not set_clipboard_text(text):
        type_text(text)
        return "type"
    time.sleep(0.05)
    paste_from_clipboard()

    if cfg.get("restore_clipboard", True) and not cfg.get("keep_in_clipboard"):
        delay = max(0, int(cfg.get("restore_delay_ms") or 350)) / 1000.0
        time.sleep(delay)
        if previous is not None:
            set_clipboard_text(previous)
    return "clipboard"


# ---------------------------------------------------------- горячая клавиша

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
VK_MODS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
VK_MODS_R = {"ctrl": 0xA2, "alt": 0xA4, "shift": 0xA0, "win": 0x5C}


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


if IS_WINDOWS:
    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
    user32.SetWindowsHookExW.restype = wt.HANDLE
    user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]


class KeyboardHook(threading.Thread):
    """Низкоуровневый хук: ловит сочетание даже там, где RegisterHotKey бессилен.

    Сработавшее сочетание «проглатывается», чтобы не печатать символ в чужом окне.
    """

    def __init__(self, on_down, on_up=None):
        super().__init__(daemon=True, name="kbdhook")
        self.on_down = on_down
        self.on_up = on_up
        self._mods: list[str] = ["ctrl"]
        self._key: int = 0x20
        self._lock = threading.RLock()
        self._pressed: set[int] = set()
        self._active = False
        self._hook = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self.registered = False
        self.error: str | None = None
        self.paused = False
        self._proc_ref = None

    # -- конфигурация
    def bind(self, hotkey: dict) -> bool:
        mods, key = combo_to_hotkey(hotkey)
        with self._lock:
            self._mods = [str(m).lower() for m in (hotkey.get("mods") or [])]
            self._key = int(key or 0)
        return self.registered

    @property
    def hotkey_repr(self) -> str:
        return f"{self._mods}+{self._key:#x}"

    # -- хук
    def run(self):
        if not IS_WINDOWS:
            return
        self._thread_id = int(kernel32.GetCurrentThreadId())
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
        self._proc_ref = HOOKPROC(self._handle)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_ref, None, 0)
        self.registered = bool(self._hook)
        if not self.registered:
            self.error = f"не удалось установить перехват клавиатуры (код {ctypes.get_last_error()})"
        self._ready.set()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break

    def wait_ready(self, timeout: float = 3.0) -> bool:
        return self._ready.wait(timeout)

    def _mods_held(self) -> bool:
        for name in self._mods:
            vk = VK_MODS.get(name, 0)
            vk_r = VK_MODS_R.get(name, 0)
            held = (vk and vk in self._pressed) or (vk_r and vk_r in self._pressed)
            if not held and vk:
                held = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if not held:
                return False
        return True

    def _handle(self, nCode, wParam, lParam):
        try:
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)
                mod_vks = set()
                for name in VK_MODS:
                    mod_vks.add(VK_MODS[name])
                    mod_vks.add(VK_MODS_R[name])

                if is_down:
                    self._pressed.add(vk)
                elif is_up:
                    self._pressed.discard(vk)

                with self._lock:
                    key, paused = self._key, self.paused
                if not key or paused:
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)

                if is_down and vk == key and self._mods_held() and not self._active:
                    self._active = True
                    if self.on_down:
                        threading.Thread(target=self.on_down, daemon=True).start()
                    return 1  # проглотить

                if self._active and (
                    (is_up and vk == key) or (is_up and vk in mod_vks)
                ):
                    self._active = False
                    if self.on_up:
                        threading.Thread(target=self.on_up, daemon=True).start()
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def stop(self):
        try:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


def keys_down(mod_names: list[str], vk_key: int) -> bool:
    if not IS_WINDOWS:
        return False
    for m in mod_names:
        vk = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}.get(str(m).lower())
        if vk and not (user32.GetAsyncKeyState(vk) & 0x8000):
            return False
    return bool(user32.GetAsyncKeyState(vk_key) & 0x8000)


def foreground_pid() -> int:
    if not IS_WINDOWS:
        return 0
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


# ---------------------------------------------------------------- диктовка


class Dictation:
    """Горячая клавиша -> запись -> распознавание -> вставка."""

    IDLE, RECORDING, WORKING = "idle", "recording", "working"

    def __init__(self, engine: asr.Engine, get_cfg, on_done=None, is_ui_foreground=None):
        self.engine = engine
        self.get_cfg = get_cfg
        self.on_done = on_done
        self.is_ui_foreground = is_ui_foreground
        self.recorder = audio.Recorder()
        self.state = self.IDLE
        self.error: str | None = None
        self.last_text = ""
        self.last_result: dict | None = None
        self.ui_focused = False
        self._lock = threading.RLock()
        self._source = "hotkey"
        self.started_at = 0.0
        self.on_state = None

    # -- состояние
    def ui_active(self) -> bool:
        """Наше окно сейчас в фокусе? Основной признак — реальное активное окно,
        флаг из браузера используется только как резерв."""
        check = self.is_ui_foreground
        if callable(check):
            try:
                return bool(check())
            except Exception:
                pass
        return self.ui_focused

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "level": round(min(1.0, float(self.recorder.level) * 7), 3),
            "recording": self.state == self.RECORDING,
            "seconds": round(time.time() - self.started_at, 1) if self.state == self.RECORDING else 0,
            "last_text": self.last_text,
            "error": self.error,
            "last_result": self.last_result,
            "ui_focused": self.ui_focused,
            "ui_active": self.ui_active(),
        }

    def _set(self, state: str):
        self.state = state
        if self.on_state:
            try:
                self.on_state(state)
            except Exception:
                pass

    # -- сценарий
    def on_key_down(self):
        cfg = self.get_cfg()
        mode = cfg.get("mode") or "hold"
        if mode == "toggle":
            if self.state == self.RECORDING:
                self.stop("hotkey")
            elif self.state == self.IDLE:
                self.start("hotkey")
            return
        # режим «зажми и говори»
        if self.state == self.IDLE:
            self.start("hotkey")

    def on_key_up(self):
        cfg = self.get_cfg()
        if (cfg.get("mode") or "hold") != "hold":
            return
        if self.state == self.RECORDING and self._source == "hotkey":
            self.stop("hotkey")

    def start(self, source: str = "hotkey") -> bool:
        cfg = self.get_cfg()
        with self._lock:
            if self.state != self.IDLE:
                return False
            self.error = None
            if not self.recorder.start(cfg):
                self.error = self.recorder.error or "Микрофон недоступен"
                self._set(self.IDLE)
                if cfg.get("sound_cues"):
                    audio.beep("error")
                return False
            self._source = source
            self.started_at = time.time()
            self._set(self.RECORDING)
            if cfg.get("sound_cues"):
                audio.beep("start")
            _log(f"старт записи ({source})")
            return True

    def stop(self, source: str = "hotkey") -> None:
        cfg = self.get_cfg()
        with self._lock:
            if self.state != self.RECORDING:
                return
            samples = self.recorder.stop()
            self._set(self.WORKING)
            if cfg.get("sound_cues"):
                audio.beep("stop")

        dur = len(samples) / 16000.0
        if dur < 0.18:
            self.error = "Слишком короткая запись — удерживайте чуть дольше."
            self.last_result = None
            self._set(self.IDLE)
            return

        def work():
            try:
                text = asr.clean_text(self.engine.transcribe(samples, int(cfg.get("num_threads") or 4)))
                self.last_text = text
                inserted = "none"
                if text:
                    if source == "hotkey" and not self.ui_active():
                        inserted = insert_text(text, cfg)
                    else:
                        set_clipboard_text(text)
                        inserted = "clipboard-ui"
                self.last_result = {
                    "text": text,
                    "duration": round(dur, 2),
                    "inserted": inserted,
                    "source": source,
                }
                if not text:
                    peak = float(abs(samples).max()) if len(samples) else 0.0
                    if peak < 1e-4:
                        self.error = (
                            "Микрофон молчит: нет сигнала. Проверьте кнопку Mute на устройстве "
                            "и уровень записи в параметрах Windows."
                        )
                    else:
                        self.error = "Речь не распознана. Говорите ближе к микрофону."
                    if cfg.get("sound_cues"):
                        audio.beep("error")
                _log(
                    f"стоп: {dur:.1f} c, символов {len(text)}, вставка '{inserted}', "
                    f"окно в фокусе: {self.ui_active()}"
                    + (f", ошибка: {self.error}" if self.error else "")
                )
                if self.on_done:
                    self.on_done(self.last_result)
            except Exception as exc:  # pragma: no cover
                self.error = f"Ошибка распознавания: {exc}"
                self.last_result = None
            finally:
                self._set(self.IDLE)

        threading.Thread(target=work, daemon=True).start()
