"""Сборка портативной версии Стенограф (PyInstaller).

Результат: dist/Stenograf/  — папка, которую можно скопировать на любой ПК
и запустить Stenograf.exe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
TARGET = DIST / "Stenograf"
ICON = ROOT / "assets" / "icon.ico"


def make_icon():
    """Рисует иконку приложения в assets/icon.ico."""
    from PIL import Image, ImageDraw

    ICON.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, size - 7, size - 7], radius=58, fill=(9, 20, 18, 255))
    d.rounded_rectangle([6, 6, size - 7, size - 7], radius=58,
                        outline=(52, 211, 153, 255), width=6)
    green = (52, 211, 153, 255)
    d.rounded_rectangle([104, 54, 152, 146], radius=24, fill=green)
    d.arc([78, 96, 178, 186], start=0, end=180, fill=green, width=13)
    d.line([128, 186, 128, 206], fill=green, width=13)
    d.line([102, 206, 154, 206], fill=green, width=13)

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ICON, format="ICO", sizes=sizes)
    print("иконка:", ICON)


def copy_assets():
    TARGET.mkdir(parents=True, exist_ok=True)

    models_src = ROOT / "models" / "gigaam-v3-punct"
    models_dst = TARGET / "models" / "gigaam-v3-punct"
    if models_src.exists():
        print("копирую модель…")
        shutil.copytree(models_src, models_dst, dirs_exist_ok=True)
    else:
        print("ВНИМАНИЕ: модель не найдена в", models_src)

    tools_src = ROOT / "tools"
    tools_dst = TARGET / "tools"
    if tools_src.exists():
        print("копирую ffmpeg…")
        shutil.copytree(tools_src, tools_dst, dirs_exist_ok=True)

    for name in ("README.md", "LICENSE-MODEL.txt"):
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, TARGET / name)

    docs_src = ROOT / "docs"
    if docs_src.exists():
        shutil.copytree(docs_src, TARGET / "docs", dirs_exist_ok=True)

    launcher = TARGET / "Запустить Стенограф.bat"
    launcher.write_text(
        "@echo off\r\n"
        'cd /d "%~dp0"\r\n'
        'if exist "Stenograf.exe" (\r\n'
        '  start "" "Stenograf.exe"\r\n'
        ") else (\r\n"
        "  echo Stenograf.exe не найден рядом с этим файлом.\r\n"
        "  pause\r\n"
        ")\r\n",
        encoding="utf-8",
    )


def _rm(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except Exception:
        return False


def merge_tree(src: Path, dst: Path):
    """Сливает сборку в целевую папку, не трогая data/ и устаревшие файлы внутри _internal."""
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)

    fresh = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    internal = dst / "_internal"
    if internal.exists():
        for p in internal.rglob("*"):
            if p.is_file() and p.relative_to(dst) not in fresh:
                try:
                    p.unlink()
                except Exception:
                    pass


def build():
    make_icon()
    if BUILD.exists():
        _rm(BUILD)

    # сохраняем пользовательские данные (история, настройки, профиль окна)
    backup = None
    data_dir = TARGET / "data"
    if data_dir.exists():
        backup = ROOT / "_data_backup"
        _rm(backup)
        try:
            shutil.move(str(data_dir), str(backup))
            print("данные сохранены во временную папку")
        except Exception:
            backup = None

    # если старая папка занята запущенным приложением — собираем в стейдж и сливаем
    stage = None
    if TARGET.exists() and not _rm(TARGET):
        stage = DIST / "_stage"
        _rm(stage)
        print("папка dist/Stenograf занята — сборка через временный каталог")
    distpath = stage if stage else DIST

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "Stenograf",
        "--distpath", str(distpath),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--icon", str(ICON),
        "--add-data", f"{APP / 'web'}{os.pathsep}web",
        "--collect-all", "sherpa_onnx",
        "--collect-all", "sounddevice",
        "--hidden-import", "pystray._win32",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        "--paths", str(APP),
        str(APP / "main.py"),
    ]
    print("PyInstaller:", " ".join(cmd[:8]), "…")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("СБОРКА НЕ УДАЛАСЬ, код", r.returncode)
        return r.returncode

    if stage:
        print("переношу собранные файлы…")
        TARGET.mkdir(parents=True, exist_ok=True)
        merge_tree(stage / "Stenograf", TARGET)
        _rm(stage)

    if (TARGET / "Stenograf.exe").exists():
        shutil.copy2(ROOT / "assets" / "icon.ico", TARGET / "icon.ico")

    copy_assets()

    if backup and backup.exists():
        target_data = TARGET / "data"
        _rm(target_data)
        try:
            shutil.move(str(backup), str(target_data))
            print("данные возвращены на место")
        except Exception as exc:
            print("не удалось вернуть данные:", exc)

    size = sum(f.stat().st_size for f in TARGET.rglob("*") if f.is_file()) / 1_048_576
    print(f"\nГотово: {TARGET}  ({size:.0f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
