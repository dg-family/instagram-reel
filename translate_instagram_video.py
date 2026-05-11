#!/usr/bin/env python3
"""
從 Instagram 影片擷取語音、轉成文字，並翻譯成中文。

使用方式：
  1) 本機已有影片檔：
     python translate_instagram_video.py --video ./reel.mp4

  2) 公開 Instagram 網址（需已安裝 yt-dlp 與 ffmpeg）：
     python translate_instagram_video.py --url "https://www.instagram.com/reel/xxxxx/"

注意：
  - 僅處理你有權存取的內容；請遵守 Instagram 使用條款與著作權。
  - 私人帳號或需登入的內容，請自行下載成檔案後用 --video 指定。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from deep_translator import GoogleTranslator


def require_cmd(name: str) -> str:
    path = shutil.which(name)
    if not path:
        print(f"錯誤：找不到指令「{name}」。請先安裝後再試。", file=sys.stderr)
        sys.exit(1)
    return path


def download_instagram(url: str, out_dir: Path) -> Path:
    require_cmd("yt-dlp")
    require_cmd("ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "ig_video.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f",
        "bestvideo+bestaudio/best",
        "--merge-output-format",
        "mp4",
        "-o",
        pattern,
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        sys.exit(r.returncode)
    files = sorted(out_dir.glob("ig_video.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("錯誤：下載完成但找不到輸出檔案。", file=sys.stderr)
        sys.exit(1)
    return files[0]


def transcribe(
    media_path: Path,
    model_size: str,
    device: str,
    compute_type: str,
    language: str | None,
) -> str:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(media_path),
        language=language,
        vad_filter=True,
    )
    parts: list[str] = []
    for seg in segments:
        parts.append(seg.text.strip())
    text = " ".join(p for p in parts if p)
    if not text.strip():
        print(
            f"警告：轉錄結果為空（偵測語言：{info.language}，機率 {info.language_probability:.2f}）。",
            file=sys.stderr,
        )
    return text


def transcribe_segments(
    media_path: Path,
    model_size: str,
    device: str,
    compute_type: str,
    language: str | None,
) -> list[dict[str, Any]]:
    """回傳帶時間軸的逐段轉錄，供字幕或進階處理使用。"""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(media_path),
        language=language,
        vad_filter=True,
    )
    out: list[dict[str, Any]] = []
    for seg in segments:
        t = (seg.text or "").strip()
        if not t:
            continue
        out.append(
            {
                "start": float(seg.start),
                "end": float(seg.end),
                "text": t,
            }
        )
    if not out and info.language:
        print(
            f"警告：分段轉錄為空（偵測語言：{info.language}，機率 {info.language_probability:.2f}）。",
            file=sys.stderr,
        )
    return out


def translate_segments_to_chinese(
    segments: list[dict[str, Any]],
    traditional: bool,
    throttle_sec: float = 0.12,
) -> list[dict[str, Any]]:
    """逐段翻譯為中文，保留 start / end / text，並加上 text_zh。"""
    tgt = "zh-TW" if traditional else "zh-CN"
    translator = GoogleTranslator(source="auto", target=tgt)
    result: list[dict[str, Any]] = []
    for i, s in enumerate(segments):
        raw = (s.get("text") or "").strip()
        if not raw:
            zh = ""
        else:
            try:
                zh = translator.translate(raw)
            except Exception as e:
                print(f"警告：分段翻譯失敗（{e}），保留原文。", file=sys.stderr)
                zh = raw
        row = {**s, "text_zh": zh}
        result.append(row)
        if throttle_sec > 0 and i < len(segments) - 1:
            time.sleep(throttle_sec)
    return result


def segments_to_webvtt(segments: list[dict[str, Any]]) -> str:
    """將含 text_zh 與時間的段落轉成 WebVTT。"""

    def fmt_vtt(t: float) -> str:
        ms_total = max(0, int(round(t * 1000)))
        h, r = divmod(ms_total, 3600000)
        m, r = divmod(r, 60000)
        s, msf = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{msf:03d}"

    lines = ["WEBVTT", ""]
    cue = 0
    for s in segments:
        zh = (s.get("text_zh") or "").strip()
        if not zh:
            continue
        cue += 1
        st = float(s["start"])
        en = float(s["end"])
        if en <= st:
            en = st + 0.4
        esc = zh.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(str(cue))
        lines.append(f"{fmt_vtt(st)} --> {fmt_vtt(en)}")
        lines.append(esc)
        lines.append("")
    return "\n".join(lines)


def to_chinese(text: str, traditional: bool) -> str:
    if not text.strip():
        return ""
    tgt = "zh-TW" if traditional else "zh-CN"
    # GoogleTranslator 單次有長度限制，分段翻譯
    chunk_size = 4500
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    out: list[str] = []
    for c in chunks:
        out.append(GoogleTranslator(source="auto", target=tgt).translate(c))
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram 影片語音 → 中文翻譯")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="Instagram 貼文 / Reel 公開網址")
    g.add_argument("--video", type=Path, help="本機影片檔路徑")
    parser.add_argument(
        "--model",
        default="base",
        help="Whisper 模型：tiny, base, small, medium, large-v3 等（預設 base）",
    )
    parser.add_argument("--device", default="cpu", help="推理裝置：cpu 或 cuda")
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="cpu 建議 int8；若 device=cuda 可試 float16",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="強制來源語言代碼（如 en、ja），省略則自動偵測",
    )
    parser.add_argument(
        "--traditional",
        action="store_true",
        help="輸出繁體中文（zh-TW），預設為簡體（zh-CN）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="將原文與譯文寫入此檔案（UTF-8）",
    )
    args = parser.parse_args()

    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.url:
            tmp = tempfile.TemporaryDirectory(prefix="ig_dl_")
            media = download_instagram(args.url.strip(), Path(tmp.name))
        else:
            media = args.video.expanduser().resolve()
            if not media.is_file():
                print(f"錯誤：找不到影片檔：{media}", file=sys.stderr)
                sys.exit(1)

        print("正在轉錄（首次執行會下載 Whisper 模型）…", flush=True)
        transcript = transcribe(
            media,
            model_size=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
        )
        print("\n--- 轉錄原文 ---\n")
        print(transcript or "(無語音內容)")

        print("\n正在翻譯成中文…", flush=True)
        zh = to_chinese(transcript, traditional=args.traditional)
        print("\n--- 中文譯文 ---\n")
        print(zh or "(無譯文)")

        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                "【轉錄】\n" + (transcript or "") + "\n\n【中文】\n" + (zh or ""),
                encoding="utf-8",
            )
            print(f"\n已寫入：{args.out}", flush=True)
    finally:
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    main()
