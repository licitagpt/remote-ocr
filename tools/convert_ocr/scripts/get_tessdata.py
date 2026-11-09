"""Baixa os dados de idioma do Tesseract (tessdata_fast) para usar o backend `tesseract` do motor pymupdf4llm.

    python scripts/get_tessdata.py [--dir DIR] [--langs por,eng,osd]

Depois: `--tessdata DIR --ocr-backend tesseract`.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--dir", default="tessdata", help="pasta de destino (default: ./tessdata)")
    ap.add_argument("--langs", default="por,eng,osd", help="idiomas separados por virgula")
    args = ap.parse_args()
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    for lang in [l.strip() for l in args.langs.split(",") if l.strip()]:
        dest = out / f"{lang}.traineddata"
        if dest.is_file() and dest.stat().st_size > 0:
            print(f"{dest}: ja existe")
            continue
        url = BASE.format(lang=lang)
        print(f"baixando {url} -> {dest}")
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as e:  # noqa: BLE001
            print(f"  falhou: {e}", file=sys.stderr)
            return 1
    print(f"pronto. Use TESSDATA={out.resolve()} (ou TESSDATA_PREFIX) e --ocr-backend tesseract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
