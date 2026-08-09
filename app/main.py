"""Speaker-wise transcript tool.

A small FastAPI service that accepts a YouTube URL or an uploaded mp3/mp4 and
returns a speaker-labelled transcript via AssemblyAI. No files are stored on the
server: uploads stream through to AssemblyAI's temp storage and the transcript
is deleted on request.
"""

import asyncio
import os
import tempfile
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import assembly

MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB safety net
SUPPORTED_EXT = {".mp3", ".mp4", ".m4a", ".wav", ".webm", ".ogg", ".flac", ".aac"}

app = FastAPI(title="Speaker Transcript", version="0.1.0")


class UrlRequest(BaseModel):
    url: str = Field(..., description="YouTube (or other yt-dlp supported) URL")
    language_code: str | None = None
    speakers_expected: int | None = Field(
        default=None, ge=2, le=10, description="Optional hint for diarization"
    )


async def _submit(audio: bytes, req: UrlRequest) -> dict:
    try:
        transcript_id = await assembly.submit_audio(
            audio,
            language_code=req.language_code,
            speakers_expected=req.speakers_expected,
        )
    except assembly.AssemblyError as exc:
        raise HTTPException(502, f"Submit failed: {exc.message}") from exc
    return {"transcript_id": transcript_id, "status": "queued"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/api/transcribe/url", status_code=202)
async def transcribe_url(req: UrlRequest) -> dict:
    """Download audio from a YouTube URL, transcribe it, return a transcript id."""
    if not req.url.strip():
        raise HTTPException(400, "URL is required")

    try:
        loop = asyncio.get_running_loop()
        audio_path = await loop.run_in_executor(
            None, _download_audio, req.url.strip()
        )
    except Exception as exc:  # yt-dlp raises many error types
        raise HTTPException(422, f"Failed to download audio: {exc}") from exc

    try:
        audio = Path(audio_path).read_bytes()
    finally:
        Path(audio_path).unlink(missing_ok=True)

    return await _submit(audio, req)


@app.post("/api/transcribe/upload", status_code=202)
async def transcribe_upload(
    file: UploadFile = File(...),
    language_code: str | None = None,
    speakers_expected: int | None = None,
) -> dict:
    """Transcribe an uploaded mp3/mp4. The file is streamed straight to AssemblyAI."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise HTTPException(
            415, f"Unsupported file type '{ext or 'unknown'}'. Use one of: {sorted(SUPPORTED_EXT)}"
        )

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")

    req = UrlRequest(
        url="upload",
        language_code=language_code,
        speakers_expected=speakers_expected,
    )
    return await _submit(content, req)


@app.get("/api/transcript/{transcript_id}")
async def transcript_status(transcript_id: str) -> dict:
    """Poll status. Returns speaker-wise utterances once completed."""
    try:
        data = await assembly.get_transcript(transcript_id)
    except assembly.AssemblyError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return data


@app.delete("/api/transcript/{transcript_id}", status_code=204)
async def transcript_delete(transcript_id: str) -> Response:
    """Delete the transcript once the caller is done (auto-cleans AssemblyAI's copy)."""
    try:
        await assembly.delete_transcript(transcript_id)
    except assembly.AssemblyError:
        raise HTTPException(404, "transcript not found")
    return Response(status_code=204)


def _download_audio(url: str) -> str:
    """Download just the audio track of a YouTube URL to a temp dir, return its path."""
    tmp_dir = tempfile.mkdtemp(prefix="st_")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp_dir, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    candidates = sorted(Path(tmp_dir).glob("audio.mp3"))
    if not candidates:
        raise RuntimeError("yt-dlp produced no audio files")
    return str(candidates[0])


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
