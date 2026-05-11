#!/usr/bin/env python3
"""
將影片（或 Instagram Reel）轉錄、翻成中文後，用語音合成覆蓋成「中文發音」影片。

依賴：ffmpeg、（選用 URL 時）yt-dlp。中文語音使用免費的 Microsoft Edge TTS（edge-tts）。

範例：
  python dub_video_chinese.py --video ./clip.mp4 --out ./clip_zh_tw.mp4 --traditional
  python dub_video_chinese.py --url "https://www.instagram.com/reel/xxx/" --out out.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import edge_tts

from translate_instagram_video import (
    download_instagram,
    require_cmd,
    to_chinese,
    transcribe,
)


def ffprobe_duration_seconds(path: Path) -> float:
    require_cmd("ffprobe")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        sys.exit(r.returncode)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        print(f"錯誤：無法解析影片長度：{path}", file=sys.stderr)
        sys.exit(1)


def split_tts_chunks(text: str, max_chars: int = 2800) -> list[str]:
    """Edge 單次請求過長可能失敗，切成多段再拼接音訊；盡量在標點處切開。"""
    t = text.strip()
    if not t:
        return []
    if len(t) <= max_chars:
        return [t]
    chunks: list[str] = []
    start = 0
    while start < len(t):
        end = min(start + max_chars, len(t))
        if end < len(t):
            window = t[start:end]
            cut = -1
            for sep in ("\n", "。", "！", "？", "；", "，", " "):
                idx = window.rfind(sep)
                if idx != -1 and idx > int(max_chars * 0.25):
                    cut = idx + 1
                    break
            if cut != -1:
                end = start + cut
        piece = t[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end if end > start else min(start + max_chars, len(t))
    return chunks


async def _synthesize_chunk(text: str, voice: str, rate: str, out_path: Path) -> None:
    comm = edge_tts.Communicate(text, voice=voice, rate=rate)
    await comm.save(str(out_path))


async def synthesize_chinese_speech(
    text: str,
    voice: str,
    rate: str,
    out_mp3: Path,
    work_dir: Path,
) -> None:
    chunks = split_tts_chunks(text)
    if not chunks:
        print("錯誤：沒有可合成的中文內容。", file=sys.stderr)
        sys.exit(1)
    paths: list[Path] = []
    for i, ch in enumerate(chunks):
        p = work_dir / f"tts_part_{i:04d}.mp3"
        await _synthesize_chunk(ch, voice=voice, rate=rate, out_path=p)
        paths.append(p)
    if len(paths) == 1:
        shutil.copy(paths[0], out_mp3)
        return
    list_file = work_dir / "concat_list.txt"
    lines = []
    for p in paths:
        # concat demuxer 需要跳脫單引號
        safe = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_file.write_text("\n".join(lines), encoding="utf-8")
    require_cmd("ffmpeg")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(out_mp3),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        sys.exit(r.returncode)


def align_audio_to_video_duration(speech_mp3: Path, video_duration: float, out_audio: Path) -> None:
    require_cmd("ffmpeg")
    ad = ffprobe_duration_seconds(speech_mp3)
    vd = video_duration
    if vd <= 0:
        print("錯誤：影片長度無效。", file=sys.stderr)
        sys.exit(1)
    # 容許極小誤差，避免無限 padding
    eps = 0.08
    if ad + eps < vd:
        pad = vd - ad
        filt = f"apad=pad_dur={pad:.3f}"
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(speech_mp3),
            "-af",
            filt,
            "-t",
            f"{vd:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(out_audio),
        ]
    elif ad > vd + eps:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(speech_mp3),
            "-af",
            f"atrim=0:{vd:.3f},asetpts=PTS-STARTPTS",
            "-t",
            f"{vd:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(out_audio),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(speech_mp3),
            "-t",
            f"{vd:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(out_audio),
        ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        sys.exit(r.returncode)


def mux_video_audio(video: Path, audio_aac: Path, out_mp4: Path) -> None:
    require_cmd("ffmpeg")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio_aac),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(out_mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        sys.exit(r.returncode)


def default_voice(traditional: bool) -> str:
    return "zh-TW-HsiaoChenNeural" if traditional else "zh-CN-XiaoxiaoNeural"


async def _run_dub(args: argparse.Namespace) -> None:
    tmp_dl: tempfile.TemporaryDirectory[str] | None = None
    tmp_work = tempfile.TemporaryDirectory(prefix="dub_work_")
    work = Path(tmp_work.name)
    try:
        if args.url:
            tmp_dl = tempfile.TemporaryDirectory(prefix="ig_dl_")
            media = download_instagram(args.url.strip(), Path(tmp_dl.name))
        else:
            media = args.video.expanduser().resolve()
            if not media.is_file():
                print(f"錯誤：找不到影片：{media}", file=sys.stderr)
                sys.exit(1)

        vd = ffprobe_duration_seconds(media)
        print(f"影片長度約 {vd:.1f} 秒。正在轉錄…", flush=True)
        transcript = transcribe(
            media,
            model_size=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
        )
        print("正在翻譯成中文…", flush=True)
        zh = to_chinese(transcript, traditional=args.traditional)
        if not zh.strip():
            print("錯誤：譯文為空，無法配音。", file=sys.stderr)
            sys.exit(1)

        voice = args.voice or default_voice(args.traditional)
        speech_mp3 = work / "speech.mp3"
        aligned = work / "aligned.m4a"
        print(f"正在合成中文語音（{voice}）…", flush=True)
        await synthesize_chinese_speech(zh, voice, args.rate, speech_mp3, work)
        print("正在對齊音訊長度並與影片合併…", flush=True)
        align_audio_to_video_duration(speech_mp3, vd, aligned)

        out = args.out.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        mux_video_audio(media, aligned, out)
        print(f"完成：{out}", flush=True)

        if args.save_script:
            args.save_script.parent.mkdir(parents=True, exist_ok=True)
            args.save_script.write_text(
                "【轉錄】\n" + transcript + "\n\n【中文配音稿】\n" + zh,
                encoding="utf-8",
            )
            print(f"字幕／稿：{args.save_script}", flush=True)
    finally:
        tmp_work.cleanup()
        if tmp_dl is not None:
            tmp_dl.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="影片 → 中文語音配音（覆蓋原音軌）")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="Instagram 公開網址")
    g.add_argument("--video", type=Path, help="本機影片檔")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="輸出 MP4 路徑",
    )
    parser.add_argument(
        "--traditional",
        action="store_true",
        help="繁體中文稿與預設台灣語音；不加則簡體＋大陸預設語音",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help="Edge TTS voice id，例如 zh-TW-YunJheNeural、zh-CN-YunxiNeural",
    )
    parser.add_argument(
        "--rate",
        default="+0%",
        help="語速，例如 +10%%、-5%%（預設 +0%%）",
    )
    parser.add_argument("--model", default="base", help="Whisper 模型大小")
    parser.add_argument("--device", default="cpu", help="cpu 或 cuda")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--language", default=None, help="強制來源語言，如 en")
    parser.add_argument(
        "--save-script",
        type=Path,
        default=None,
        help="另存轉錄與中文稿為 UTF-8 文字檔",
    )
    args = parser.parse_args()
    asyncio.run(_run_dub(args))


if __name__ == "__main__":
    main()
