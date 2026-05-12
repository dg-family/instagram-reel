#!/usr/bin/env python3
"""將 static/index.html 複本中的 __INSTAGRAM_REEL_API_BASE__ 換成實際 API 根網址（無結尾斜線）。"""
from __future__ import annotations

import pathlib
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: inject_api_base.py <檔案.html> [API_BASE_URL]", file=sys.stderr)
        sys.exit(1)
    path = pathlib.Path(sys.argv[1])
    api = (sys.argv[2] if len(sys.argv) > 2 else "").strip().rstrip("/")
    text = path.read_text(encoding="utf-8")
    if "__INSTAGRAM_REEL_API_BASE__" not in text:
        print("錯誤：檔案中找不到 __INSTAGRAM_REEL_API_BASE__", file=sys.stderr)
        sys.exit(1)
    path.write_text(text.replace("__INSTAGRAM_REEL_API_BASE__", api), encoding="utf-8")


if __name__ == "__main__":
    main()
