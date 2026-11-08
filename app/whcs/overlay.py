"""Плавающий индикатор записи: маленькая панель поверх всех окон.

Рисуется через GDI+ в 32-битный DIB и выводится слоистым окном (per-pixel alpha),
поэтому углы скруглены и панель прозрачна. Окно не забирает фокус и «прозрачно»
для мыши, так что не мешает работать в целевом приложении.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import threading

IS_WINDOWS = os.name == "nt"

W_BASE, H_BASE = 268, 86

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01
AC_SRC_OVER = 0x00
WM_TIMER = 0x0113
WM_APP = 0x8000


# ------------------------------------------------------------------ структуры

class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int),
        ("SuppressExternalCodecs", ctypes.c_int),
    ]


class _RectF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float),
                ("W", ctypes.c_float), ("H", ctypes.c_float)]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _Blend(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


# ---------------------------------------------------------------- прототипы

if IS_WINDOWS:
    _u = ctypes.WinDLL("user32", use_last_error=True)
    _gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _HGDIOBJ = ctypes.c_void_p

    _u.CreateWindowExW.restype = wt.HWND
    _u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
    _u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_ssize_t]
    _u.DefWindowProcW.restype = ctypes.c_ssize_t
    _u.RegisterClassW.argtypes = [ctypes.c_void_p]
    _u.RegisterClassW.restype = wt.ATOM
    _u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _u.ShowWindow.restype = wt.BOOL
    _u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
    _u.SetWindowPos.restype = wt.BOOL
    _u.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
    _u.MonitorFromPoint.argtypes = [_Point, wt.DWORD]
    _u.MonitorFromPoint.restype = wt.HANDLE
    _u.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(_MONITORINFO)]
    _u.GetMonitorInfoW.restype = wt.BOOL
    _u.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(_Point),
                                       ctypes.POINTER(_Size), wt.HDC, ctypes.POINTER(_Point),
                                       wt.DWORD, ctypes.POINTER(_Blend), wt.DWORD]
    _u.UpdateLayeredWindow.restype = wt.BOOL
    _u.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    _u.SetTimer.restype = ctypes.c_size_t
    _u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    _u.GetMessageW.restype = wt.BOOL
    _u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    _u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    _u.GetDC.argtypes = [wt.HWND]
    _u.GetDC.restype = wt.HDC
    _u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _u.IsWindowVisible.argtypes = [wt.HWND]
    _u.PostQuitMessage.argtypes = [ctypes.c_int]

    _gdi.CreateCompatibleDC.argtypes = [wt.HDC]
    _gdi.CreateCompatibleDC.restype = wt.HDC
    _gdi.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                      ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    _gdi.CreateDIBSection.restype = wt.HANDLE
    _gdi.SelectObject.argtypes = [wt.HDC, _HGDIOBJ]
    _gdi.SelectObject.restype = _HGDIOBJ
    _gdi.GetDeviceCaps.argtypes = [wt.HDC, ctypes.c_int]
    _gdi.GetDeviceCaps.restype = ctypes.c_int


def _argb(a: int, r: int, g: int, b: int) -> int:
    return ((a & 0xFF) << 24) | ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | (b & 0xFF)


class GdiPlus:
    """Минимальная обёртка над gdiplus.dll."""

    def __init__(self):
        self.ok = False
        if not IS_WINDOWS:
            return
        try:
            self.dll = ctypes.WinDLL("gdiplus")
            self._startup()
            self.ok = True
        except Exception:
            self.ok = False

    def _p(self, name, argtypes, restype=ctypes.c_int):
        fn = getattr(self.dll, name)
        fn.argtypes = argtypes
        fn.restype = restype
        return fn

    def _startup(self):
        inp = _GdiplusStartupInput()
        inp.GdiplusVersion = 1
        self.token = ctypes.c_void_p()
        self._p("GdiplusStartup", [ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(_GdiplusStartupInput),
                                   ctypes.c_void_p])(
            ctypes.byref(self.token), ctypes.byref(inp), None)
        vp = ctypes.c_void_p
        f = ctypes.c_float
        self.create_from_hdc = self._p("GdipCreateFromHDC", [wt.HDC, ctypes.POINTER(vp)])
        self.delete_graphics = self._p("GdipDeleteGraphics", [vp])
        self.set_compositing = self._p("GdipSetCompositingMode", [vp, ctypes.c_int])
        self.create_fill = self._p("GdipCreateSolidFill", [ctypes.c_uint32, ctypes.POINTER(vp)])
        self.fill_rect = self._p("GdipFillRectangle", [vp, vp, f, f, f, f])
        self.fill_ellipse = self._p("GdipFillEllipse", [vp, vp, f, f, f, f])
        self.create_path = self._p("GdipCreatePath", [ctypes.c_int, ctypes.POINTER(vp)])
        self.add_arc = self._p("GdipAddPathArc", [vp, f, f, f, f, f, f])
        self.close_figure = self._p("GdipClosePathFigure", [vp])
        self.fill_path = self._p("GdipFillPath", [vp, vp, vp])
        self.delete_path = self._p("GdipDeletePath", [vp])
        self.delete_brush = self._p("GdipDeleteBrush", [vp])
        # шрифт создаётся не по имени строкой, а по объекту семейства
        self.create_family = self._p("GdipCreateFontFamilyFromName",
                                     [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(vp)])
        self.create_font = self._p("GdipCreateFont", [vp, f, ctypes.c_int,
                                                      ctypes.c_int, ctypes.POINTER(vp)])
        self.delete_font = self._p("GdipDeleteFont", [vp])
        self._families: dict[str, ctypes.c_void_p] = {}
        self.draw_string = self._p("GdipDrawString", [vp, ctypes.c_wchar_p, ctypes.c_int, vp,
                                                      ctypes.POINTER(_RectF), vp, vp])
        self.set_text_hint = self._p("GdipSetTextRenderingHint", [vp, ctypes.c_int])

    def graphics(self, hdc):
        g = ctypes.c_void_p()
        self.create_from_hdc(hdc, ctypes.byref(g))
        return g

    def brush(self, color: int):
        b = ctypes.c_void_p()
        self.create_fill(ctypes.c_uint32(color), ctypes.byref(b))
        return b

    def rounded_path(self, x, y, w, h, r):
        p = ctypes.c_void_p()
        self.create_path(0, ctypes.byref(p))
        d = r * 2
        self.add_arc(p, x, y, d, d, 180, 90)
        self.add_arc(p, x + w - d, y, d, d, 270, 90)
        self.add_arc(p, x + w - d, y + h - d, d, d, 0, 90)
        self.add_arc(p, x, y + h - d, d, d, 90, 90)
        self.close_figure(p)
        return p

    def family(self, name: str):
        fam = self._families.get(name)
        if fam is None:
            fam = ctypes.c_void_p()
            self.create_family(ctypes.c_wchar_p(name), None, ctypes.byref(fam))
            if not fam.value:  # запасной вариант, если семейства нет в системе
                self.create_family(ctypes.c_wchar_p("Segoe UI"), None, ctypes.byref(fam))
            self._families[name] = fam
        return fam

    def font(self, family, px, style=0):
        f = ctypes.c_void_p()
        self.create_font(self.family(family), ctypes.c_float(px), style, 2, ctypes.byref(f))
        return f


class HUD(threading.Thread):
    """Индикатор записи. Сам решает, показываться ли, по внешнему состоянию."""

    def __init__(self, get_state):
        super().__init__(daemon=True, name="hud")
        self.get_state = get_state          # () -> (state, level, seconds, error)
        self._ready = threading.Event()
        self._hwnd = None
        self._visible = False
        self._gp = None
        self._scale = 1.0
        self._phase = 0.0
        self.started_ok = False
        self.error: str | None = None

    def wait_ready(self, timeout=3.0) -> bool:
        return self._ready.wait(timeout)

    # -- поток
    def run(self):
        if not IS_WINDOWS:
            self._ready.set()
            return
        try:
            self._gp = GdiPlus()
            if not self._gp.ok:
                self.error = "GDI+ недоступен"
                return
            self._create_window()
            self.started_ok = True
            self._ready.set()
            _u.SetTimer(self._hwnd, 1, 33, None)
            msg = wt.MSG()
            while True:
                ret = _u.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == WM_TIMER:
                    try:
                        self._tick()
                    except Exception as exc:  # не роняем поток из-за одного кадра
                        self.error = str(exc)
                else:
                    _u.TranslateMessage(ctypes.byref(msg))
                    _u.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.error = str(exc)
            self._ready.set()

    # -- окно
    def _create_window(self):
        _u.SetProcessDPIAware()
        dc = _u.GetDC(None)
        dpi = _gdi.GetDeviceCaps(dc, 88) or 96
        _u.ReleaseDC(None, dc)
        self._scale = max(1.0, dpi / 96.0)

        hinst = _k.GetModuleHandleW(None)
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_ssize_t)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
            ]

        @WNDPROC
        def proc(hwnd, msg, wp, lp):
            return _u.DefWindowProcW(hwnd, msg, wp, lp)

        self._proc_ref = proc
        wc = WNDCLASS()
        wc.lpfnWndProc = proc
        wc.hInstance = hinst
        wc.lpszClassName = "Stenograf_HUD"
        atom = _u.RegisterClassW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() not in (0, 1410):
            raise OSError(f"RegisterClassW: {ctypes.get_last_error()}")

        self._w = int(W_BASE * self._scale)
        self._h = int(H_BASE * self._scale)
        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self._hwnd = _u.CreateWindowExW(ex, "Stenograf_HUD", "Stenograf", WS_POPUP,
                                        0, 0, self._w, self._h, None, None, hinst, None)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW: {ctypes.get_last_error()}")

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self._w
        bmi.bmiHeader.biHeight = -self._h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        self._hdc_screen = _u.GetDC(None)
        self._hdc = _gdi.CreateCompatibleDC(self._hdc_screen)
        self._bits = ctypes.c_void_p()
        self._hbmp = _gdi.CreateDIBSection(self._hdc, ctypes.byref(bmi), 0,
                                           ctypes.byref(self._bits), None, 0)
        if not self._hbmp:
            raise OSError(f"CreateDIBSection: {ctypes.get_last_error()}")
        _gdi.SelectObject(self._hdc, self._hbmp)

    # -- кадр
    def _tick(self):
        try:
            state, level, seconds, _err = self.get_state()
        except Exception:
            state, level, seconds = "idle", 0.0, 0.0
        want = state in ("recording", "working")
        if not want:
            if self._visible:
                _u.ShowWindow(self._hwnd, 0)
                self._visible = False
            return
        if not self._visible:
            self._place()
            _u.ShowWindow(self._hwnd, 4)  # SW_SHOWNOACTIVATE
            self._visible = True
        self._phase += 0.033
        self._paint(state, level, seconds)
        self._blit()

    def _place(self):
        pt = _Point()
        _u.GetCursorPos(ctypes.byref(pt))
        hmon = _u.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        x, y = 60, 60
        if _u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            work = mi.rcWork
            x = work.left + (work.right - work.left - self._w) // 2
            # сверху по центру: не перекрывает поле ввода, в которое идёт диктовка
            y = work.top + int(28 * self._scale)
        _u.SetWindowPos(self._hwnd, -1, x, y, self._w, self._h, 0x0010 | 0x0040)

    def _paint(self, state: str, level: float, seconds: float):
        gp = self._gp
        g = gp.graphics(self._hdc)
        s = self._scale
        W, H = self._w, self._h
        gp.set_compositing(g, 1)  # SourceCopy
        clear = gp.brush(_argb(0, 0, 0, 0))
        gp.fill_rect(g, clear, 0, 0, W, H)
        gp.delete_brush(clear)
        gp.set_compositing(g, 0)  # SourceOver
        gp.set_text_hint(g, 4)    # AntiAliasGridFit

        recording = state == "recording"

        panel = gp.rounded_path(1, 1, W - 2, H - 2, 17 * s)
        rim = gp.brush(_argb(46, 255, 255, 255))
        gp.fill_path(g, rim, panel)
        gp.delete_brush(rim)
        gp.delete_path(panel)

        inner = gp.rounded_path(2, 2, W - 4, H - 4, 16 * s)
        bg = gp.brush(_argb(244, 13, 20, 19))
        gp.fill_path(g, bg, inner)
        gp.delete_brush(bg)
        gp.delete_path(inner)

        pad = 20 * s
        cy = H / 2

        if recording:
            pulse = 0.5 + 0.5 * math.sin(self._phase * 2 * math.pi / 1.15)
            hr = (12 + 8 * pulse) * s
            halo = gp.brush(_argb(int(55 + 60 * pulse), 248, 113, 113))
            gp.fill_ellipse(g, halo, pad + 7 * s - hr, cy - hr, hr * 2, hr * 2)
            gp.delete_brush(halo)
            dot = gp.brush(_argb(255, 248, 113, 113))
            gp.fill_ellipse(g, dot, pad - 7 * s, cy - 7 * s, 14 * s, 14 * s)
            gp.delete_brush(dot)
        else:
            sp = gp.brush(_argb(255, 52, 211, 153))
            gp.fill_ellipse(g, sp, pad - 7 * s, cy - 7 * s, 14 * s, 14 * s)
            gp.delete_brush(sp)

        x0 = pad + 28 * s
        if not recording:
            big = gp.font("Segoe UI Semibold", 14.5 * s)
            gb = gp.brush(_argb(255, 238, 246, 243))
            dots = "." * (1 + int(self._phase * 2) % 3)
            gp.draw_string(g, "Распознаём" + dots, -1, big, ctypes.byref(
                _RectF(x0, cy - 11 * s, 175 * s, 24 * s)), None, gb)
            gp.delete_brush(gb)
            gp.delete_font(big)
            gp.delete_graphics(g)
            return

        bars, gap, bw = 16, 4 * s, 4 * s
        max_h = 32 * s
        t = self._phase * 3.4
        for i in range(bars):
            shape = (abs(math.sin(t + i * 0.62)) * 0.6
                     + abs(math.sin(t * 0.53 + i * 1.7)) * 0.4)
            # логарифмическая шкала: тихая речь тоже видна
            norm = min(1.0, (max(0.0, level) * 9.0) ** 0.6)
            amp = norm * (0.28 + 0.72 * shape)
            bh = max(5 * s, amp * max_h)
            alpha = 225 if bh > 9 * s else 120
            br = gp.brush(_argb(alpha, 52, 211, 153))
            gp.fill_rect(g, br, x0 + i * (bw + gap), cy - bh / 2, bw, bh)
            gp.delete_brush(br)

        tx = x0 + bars * (bw + gap) + 14 * s
        m, sec = int(seconds // 60), int(seconds % 60)
        big = gp.font("Segoe UI Semibold", 15 * s)
        gb = gp.brush(_argb(255, 238, 246, 243))
        gp.draw_string(g, f"{m}:{sec:02d}", -1, big, ctypes.byref(
            _RectF(tx, cy - 21 * s, 60 * s, 22 * s)), None, gb)
        gp.delete_brush(gb)
        gp.delete_font(big)
        small = gp.font("Segoe UI", 11.5 * s)
        gs = gp.brush(_argb(185, 156, 174, 170))
        gp.draw_string(g, "Запись", -1, small, ctypes.byref(
            _RectF(tx, cy + 2 * s, 120 * s, 18 * s)), None, gs)
        gp.delete_brush(gs)
        gp.delete_font(small)

        gp.delete_graphics(g)

    def _blit(self):
        top = _Point(0, 0)
        size = _Size(self._w, self._h)
        blend = _Blend(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        src = _Point(0, 0)
        _u.UpdateLayeredWindow(self._hwnd, self._hdc_screen, None, ctypes.byref(size),
                               self._hdc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)

    def hide(self):
        if self._hwnd:
            _u.ShowWindow(self._hwnd, 0)
            self._visible = False
