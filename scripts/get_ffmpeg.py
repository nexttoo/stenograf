"""Загрузка ffmpeg для Стенограф.

Берём **LGPL**-сборку (BtbN): для декодирования аудио и видео этого достаточно,
и она не накладывает обязательств GPL на проект.

Использование:
    python scripts/get_ffmpeg.py

Если интернет недоступен — положите любой ffmpeg.exe в папку tools/ вручную.
"""
from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
       "ffmpeg-master-latest-win64-lgpl.zip")
TOOLS = Path(__file__).resolve().parent.parent / "tools"


def main() -> int:
    force = "--force" in sys.argv[1:]
    TOOLS.mkdir(parents=True, exist_ok=True)
    target = TOOLS / "ffmpeg.exe"
    if target.exists() and target.stat().st_size > 1_000_000 and not force:
        print(f"ffmpeg уже на месте: {target} "
              f"({target.stat().st_size / 1_048_576:.0f} МБ)")
        print("перекачать другую сборку: python scripts/get_ffmpeg.py --force")
        return 0

    print("загрузка ffmpeg (LGPL, ~167 МБ)…")
    try:
        with urllib.request.urlopen(URL, timeout=120) as resp:
            data = resp.read()
    except Exception as exc:
        print(f"не удалось скачать: {exc}")
        print("\nСкачайте вручную и положите ffmpeg.exe в папку tools/:\n" + URL)
        return 1

    print("распаковка…")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = next((n for n in zf.namelist()
                       if n.lower().endswith("/bin/ffmpeg.exe")), None)
        if not member:
            print("в архиве не найден bin/ffmpeg.exe")
            return 1
        with zf.open(member) as src, open(target, "wb") as dst:
            dst.write(src.read())
        license_member = next((n for n in zf.namelist()
                               if n.lower().endswith(("/license", "/license.txt"))), None)
        if license_member:
            with zf.open(license_member) as src:
                (TOOLS / "ffmpeg.LICENSE.txt").write_bytes(src.read())

    print(f"готово: {target} ({target.stat().st_size / 1_048_576:.0f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
