"""Speaker-wise transcript tool.

A small FastAPI service that accepts a YouTube URL or an uploaded mp3/mp4 and
returns a speaker-labelled transcript via AssemblyAI. No files are stored on the
server: uploads stream through to AssemblyAI's temp storage and the transcript
is deleted on request.
"""

import asyncio
import base64
import os
import tempfile
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import assembly

# Cap at 2 GB: below AssemblyAI's 2.2 GB /upload ceiling while staying safely
# under Render free-tier demand. Files stream to a temp file, never to RAM.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
SUPPORTED_EXT = {
    # Audio (AssemblyAI supports these natively)
    ".3ga", ".8svx", ".aac", ".ac3", ".aif", ".aiff", ".alac", ".amr", ".ape",
    ".au", ".dss", ".flac", ".flv", ".m4a", ".m4b", ".m4p", ".m4r", ".mp3",
    ".mpga", ".ogg", ".oga", ".mogg", ".opus", ".qcp", ".tta", ".voc", ".wav",
    ".wma", ".wv",
    # Video (audio track is extracted server-side)
    ".webm", ".mts", ".m2ts", ".ts", ".mov", ".mp2", ".mp4", ".m4v", ".mxf",
}

app = FastAPI(title="Speaker Transcript", version="0.1.0")


class UrlRequest(BaseModel):
    url: str = Field(..., description="YouTube (or other yt-dlp supported) URL")
    language_code: str | None = None
    speakers_expected: int | None = Field(
        default=None, ge=2, le=10, description="Optional hint for diarization"
    )


async def _submit(audio_path: str, req: UrlRequest) -> dict:
    try:
        transcript_id = await assembly.submit_audio(
            audio_path,
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
        return await _submit(audio_path, req)
    finally:
        Path(audio_path).unlink(missing_ok=True)


@app.post("/api/transcribe/upload", status_code=202)
async def transcribe_upload(
    file: UploadFile = File(...),
    language_code: str | None = None,
    speakers_expected: int | None = None,
) -> dict:
    """Transcribe an uploaded audio/video file, streamed to a temp file (never RAM)."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise HTTPException(
            415, f"Unsupported file type '{ext or 'unknown'}'. Use one of: {sorted(SUPPORTED_EXT)}"
        )

    tmp = tempfile.NamedTemporaryFile(
        prefix="st_upload_", suffix=ext or ".bin", delete=False
    )
    tmp_path = tmp.name
    try:
        size = 0
        while chunk := await file.read(1024 * 1024):  # 1 MB chunks
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    413, f"File exceeds {MAX_UPLOAD_BYTES // (1024 ** 3)} GB limit"
                )
            tmp.write(chunk)
        tmp.close()

        req = UrlRequest(
            url="upload",
            language_code=language_code,
            speakers_expected=speakers_expected,
        )
        return await _submit(tmp_path, req)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


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
    """Download just the audio track of a YouTube URL to a temp dir, return its path.

    Uses cookies from ``YTDLP_COOKIES`` (base64-encoded Netscape-cookie file) if
    present, which bypasses YouTube's "confirm you're not a bot" check on
    datacenter IPs.
    """
    tmp_dir = tempfile.mkdtemp(prefix="st_")
    ydl_opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp_dir, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    cookies_b64 = os.environ.get("YTDLP_COOKIES")
    if cookies_b64:
        cookie_path = os.path.join(tmp_dir, "cookies.txt")
        Path(cookie_path).write_bytes(base64.b64decode(cookies_b64))
        ydl_opts["cookiefile"] = cookie_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    candidates = sorted(Path(tmp_dir).glob("audio.mp3"))
    if not candidates:
        raise RuntimeError("yt-dlp produced no audio files")
    return str(candidates[0])


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
