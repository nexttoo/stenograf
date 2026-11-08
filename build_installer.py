"""Сборка установщика Стенограф (Inno Setup).

1) готовит чистый payload: копия dist/Stenograf без пользовательских данных;
2) компилирует installer/Stenograf.iss в dist/Stenograf-Setup-<версия>.exe
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "Stenograf"
INSTALLER = ROOT / "installer"
PAYLOAD = INSTALLER / "payload"
SCRIPT = INSTALLER / "Stenograf.iss"
VERSION = "1.0.1"

ISCC_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
]


def find_iscc() -> Path | None:
    for p in ISCC_CANDIDATES:
        if p.exists():
            return p
    # на всякий случай — поиск в профиле
    for p in Path.home().rglob("ISCC.exe"):
        return p
    return None


def prepare_payload():
    if not DIST.exists():
        print("Нет dist/Stenograf — сначала запустите build.py")
        return False
    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD, ignore_errors=True)
    print("готовлю payload (без папки data)…")
    cmd = [
        "robocopy", str(DIST), str(PAYLOAD),
        "/E", "/XD", "data", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode >= 8:
        print("robocopy error:", r.returncode, r.stdout.decode("cp866", "replace")[:400])
        return False
    size = sum(f.stat().st_size for f in PAYLOAD.rglob("*") if f.is_file()) / 1_048_576
    print(f"payload: {size:.0f} МБ")
    return True


def compile_installer() -> int:
    iscc = find_iscc()
    if not iscc:
        print("ISCC.exe не найден. Установите Inno Setup 6 или укажите путь.")
        return 1
    print("компилирую установщик (несколько минут)…")
    r = subprocess.run([str(iscc), str(SCRIPT)], cwd=str(INSTALLER))
    if r.returncode != 0:
        print("компиляция не удалась, код", r.returncode)
        return r.returncode
    out = ROOT / "dist" / f"Stenograf-Setup-{VERSION}.exe"
    if out.exists():
        print(f"\nГотово: {out}  ({out.stat().st_size / 1_048_576:.0f} МБ)")
    return 0


if __name__ == "__main__":
    if not prepare_payload():
        sys.exit(1)
    sys.exit(compile_installer())
