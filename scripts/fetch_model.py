"""Загрузка модели распознавания GigaAM v3 (punct) для Стенограф.

Модель распространяется по лицензии MIT (GigaChat Team) и не хранится
в репозитории, потому что весит 221 МБ.

Использование:
    python scripts/fetch_model.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

BASE = (
    "https://huggingface.co/csukuangfj/"
    "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16/resolve/main/"
)
FILES = ["encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt", "LICENSE"]
TARGET = Path(__file__).resolve().parent.parent / "models" / "gigaam-v3-punct"


def human(num: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024:
            return f"{num:.0f} {unit}"
        num /= 1024
    return f"{num:.1f} ТБ"


def download(name: str) -> bool:
    dest = TARGET / name
    if dest.exists() and dest.stat().st_size > 1024:
        print(f"  {name}: уже есть ({human(dest.stat().st_size)})")
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = BASE + name
    print(f"  {name}: загрузка…", end="", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r  {name}: {done * 100 // total}% ({human(done)} из {human(total)})",
                              end="", flush=True)
        tmp.replace(dest)
        print(f"\r  {name}: готово ({human(dest.stat().st_size)})            ")
        return True
    except Exception as exc:
        print(f"\r  {name}: ошибка — {exc}")
        tmp.unlink(missing_ok=True)
        return False


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    print(f"Каталог модели: {TARGET}")
    ok = all([download(name) for name in FILES])
    if not ok:
        print("\nНе удалось скачать модель. Проверьте интернет или скачайте файлы вручную:\n" + BASE)
        return 1
    print("\nГотово. Дальше: python app/main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
