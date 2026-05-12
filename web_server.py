#!/usr/bin/env python3
"""
本機網頁服務：輸入 Instagram 網址 → 下載 → 轉錄 → 分段翻譯 → WebVTT 中文字幕 + 原影片串流播放。

啟動：
  cd <專案目錄> && source .venv/bin/activate
  pip install -r requirements.txt   # 需 yt-dlp、ffmpeg 在 PATH
  python web_server.py

環境變數（選用）：
  CORS_ORIGINS   逗號分隔的來源網址，預設含 GitHub Pages 與本機。
  EMBEDDED_API_BASE  若 HTML 託管在別處，可設為本 API 根網址（通常不必設，Fly 同源即可）。

瀏覽 http://127.0.0.1:8765/
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from translate_instagram_video import (
    download_instagram,
    segments_to_webvtt,
    transcribe_segments,
    translate_segments_to_chinese,
)

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

app = FastAPI(title="Instagram 翻譯播放")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# 逗號分隔；GitHub Pages 前端呼叫 Fly API 時必須把來源網址列在這裡
_default_cors = "https://dg-family.github.io,http://127.0.0.1:8765,http://localhost:8765"
_cors_raw = os.environ.get("CORS_ORIGINS", _default_cors).strip()
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Accept-Ranges", "Content-Length", "Content-Range"],
    )

_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# 暫存目錄上限與存活時間（避免硬碟塞滿）
_MAX_JOBS = 12
_JOB_TTL_SEC = 3600


def _instagram_url_ok(url: str) -> bool:
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        return False
    try:
        from urllib.parse import urlparse

        p = urlparse(u)
        h = (p.hostname or "").lower()
        return h in ("www.instagram.com", "instagram.com", "m.instagram.com")
    except Exception:
        return False


def _purge_old_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        to_del: list[str] = []
        for jid, meta in _jobs.items():
            if now - float(meta.get("created", 0)) > _JOB_TTL_SEC:
                to_del.append(jid)
        for jid in to_del:
            meta = _jobs.pop(jid, None)
            if meta and meta.get("work_dir"):
                wd = Path(meta["work_dir"])
                if wd.is_dir():
                    shutil.rmtree(wd, ignore_errors=True)
        # 數量上限：刪最舊的已完成／失敗
        while len(_jobs) > _MAX_JOBS:
            oldest = None
            oldest_t = None
            for jid, meta in _jobs.items():
                st = meta.get("status")
                if st not in ("completed", "failed"):
                    continue
                c = float(meta.get("created", 0))
                if oldest_t is None or c < oldest_t:
                    oldest_t = c
                    oldest = jid
            if oldest is None:
                break
            meta = _jobs.pop(oldest, None)
            if meta and meta.get("work_dir"):
                shutil.rmtree(Path(meta["work_dir"]), ignore_errors=True)


def _run_job(job_id: str, url: str, traditional: bool) -> None:
    work_root = Path(os.environ.get("TMPDIR", "/tmp")) / "ig_translate_web"
    work_root.mkdir(parents=True, exist_ok=True)
    work_dir = work_root / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    def upd(**kwargs: object) -> None:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update(kwargs)

    try:
        upd(status="processing", stage="download", error=None, work_dir=str(work_dir))
        video_path = download_instagram(url, work_dir)
        # 固定檔名方便串流
        canonical = work_dir / "source.mp4"
        if video_path.resolve() != canonical.resolve():
            if canonical.exists():
                canonical.unlink()
            shutil.move(str(video_path), str(canonical))

        upd(stage="transcribe")
        segments = transcribe_segments(
            canonical,
            model_size=WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE,
            language=None,
        )
        if not segments:
            raise RuntimeError("無法從影片辨識出語音內容（轉錄為空）。")

        upd(stage="translate")
        zh_segments = translate_segments_to_chinese(segments, traditional=traditional)
        vtt = segments_to_webvtt(zh_segments)
        vtt_path = work_dir / "subtitles.vtt"
        vtt_path.write_text(vtt, encoding="utf-8")

        upd(
            status="completed",
            stage="done",
            video_file=str(canonical),
            vtt_file=str(vtt_path),
        )
    except Exception as e:
        upd(status="failed", stage="error", error=str(e))
        shutil.rmtree(work_dir, ignore_errors=True)
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["work_dir"] = None
                _jobs[job_id]["video_file"] = None
                _jobs[job_id]["vtt_file"] = None


class CreateJobRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=2048)
    traditional: bool = True


def _index_html() -> str:
    p = STATIC_DIR / "index.html"
    if not p.is_file():
        raise HTTPException(status_code=500, detail="缺少 static/index.html")
    html = p.read_text(encoding="utf-8")
    # 部署在 Fly 同網域時留空；若要把 HTML 託管在別處可設 EMBEDDED_API_BASE
    embedded = os.environ.get("EMBEDDED_API_BASE", "").strip().rstrip("/")
    return html.replace("__INSTAGRAM_REEL_API_BASE__", embedded)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _index_html()


@app.get("/app", response_class=HTMLResponse)
def app_page() -> str:
    """與首頁相同，方便 GitHub Pages 使用 /app 路徑。"""
    return _index_html()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/jobs")
def create_job(body: CreateJobRequest) -> dict[str, object]:
    url = body.url.strip()
    if not _instagram_url_ok(url):
        raise HTTPException(
            status_code=400,
            detail="僅支援 Instagram 的 https 連結（instagram.com）。",
        )
    _purge_old_jobs()
    job_id = uuid.uuid4().hex[:16]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued",
            "stage": "queued",
            "created": time.time(),
            "error": None,
            "video_file": None,
            "vtt_file": None,
            "work_dir": None,
        }
    t = threading.Thread(
        target=_run_job,
        args=(job_id, url, body.traditional),
        daemon=True,
    )
    t.start()
    return {
        "job_id": job_id,
        "poll_url": f"/api/jobs/{job_id}",
        "video_url": f"/api/jobs/{job_id}/video",
        "subtitles_url": f"/api/jobs/{job_id}/subtitles.vtt",
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, object]:
    with _jobs_lock:
        meta = _jobs.get(job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="找不到此工作。")
    out: dict[str, object] = {
        "status": meta.get("status"),
        "stage": meta.get("stage"),
        "error": meta.get("error"),
    }
    if meta.get("status") == "completed":
        out["video_url"] = f"/api/jobs/{job_id}/video"
        out["subtitles_url"] = f"/api/jobs/{job_id}/subtitles.vtt"
    return out


@app.get("/api/jobs/{job_id}/video")
def job_video(job_id: str) -> FileResponse:
    with _jobs_lock:
        meta = _jobs.get(job_id)
    if not meta or meta.get("status") != "completed":
        raise HTTPException(status_code=404, detail="影片尚未就緒。")
    path = meta.get("video_file")
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="影片檔不存在。")
    p = Path(path)
    suf = p.suffix.lower()
    if suf in (".mp4", ".m4v"):
        mt = "video/mp4"
    elif suf == ".webm":
        mt = "video/webm"
    else:
        mt = "application/octet-stream"
    return FileResponse(
        str(p),
        media_type=mt,
        filename="instagram_clip" + suf,
    )


@app.get("/api/jobs/{job_id}/subtitles.vtt")
def job_subtitles(job_id: str) -> PlainTextResponse:
    with _jobs_lock:
        meta = _jobs.get(job_id)
    if not meta or meta.get("status") != "completed":
        raise HTTPException(status_code=404, detail="字幕尚未就緒。")
    path = meta.get("vtt_file")
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="字幕檔不存在。")
    text = Path(path).read_text(encoding="utf-8")
    return PlainTextResponse(
        content=text,
        media_type="text/vtt; charset=utf-8",
    )


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
