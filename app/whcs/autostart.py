"""Автозапуск: запись в HKCU\\...\\Run со флагом --minimized.

Через реестр, а не ярлык в «Автозагрузке», по двум причинам:
- команду с параметром можно менять из настроек без пересоздания файла;
- Windows показывает такую запись в «Диспетчере задач → Автозагрузка»,
  где её легко отключить.

Старые ярлыки в папке «Автозагрузка» (версии 1.0.0–1.0.1) удаляются при старте.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Stenograf"
FLAG = "--minimized"

_LEGACY_NAMES = ("Стенограф.lnk", "Stenograf.lnk")


def is_supported() -> bool:
    """Управлять автозапуском имеет смысл только в собранной версии."""
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def exe_path() -> str | None:
    if not is_supported():
        return None
    return sys.executable


def command() -> str | None:
    exe = exe_path()
    if not exe:
        return None
    return f'"{exe}" {FLAG}'


def startup_folder() -> Path:
    base = os.environ.get("APPDATA", str(Path.home()))
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def remove_legacy_shortcuts() -> list[str]:
    """Убирает ярлыки прежних версий, чтобы программа не стартовала дважды."""
    removed: list[str] = []
    folder = startup_folder()
    for name in _LEGACY_NAMES:
        link = folder / name
        try:
            if link.exists():
                link.unlink()
                removed.append(name)
        except Exception:
            pass
    return removed


def is_enabled() -> bool:
    if not is_supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def enable() -> bool:
    cmd = command()
    if not cmd:
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
        return True
    except Exception:
        return False


def disable() -> bool:
    if not is_supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def ensure(cfg_enabled: bool) -> None:
    """Приводит систему в соответствие с настройкой (вызывается при старте)."""
    if not is_supported():
        return
    remove_legacy_shortcuts()
    if cfg_enabled and not is_enabled():
        enable()
    elif not cfg_enabled and is_enabled():
        disable()
