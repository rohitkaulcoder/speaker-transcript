"""Speaker-wise transcript tool.

A small FastAPI service that accepts a YouTube URL or an uploaded mp3/mp4 and
returns a speaker-labelled transcript via AssemblyAI. No files are stored on the
server: uploads stream through to AssemblyAI's temp storage and the transcript
is deleted on request.
"""

import asyncio
import base64
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("speaker_transcript")

import yt_dlp
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import archive, assembly

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

# In-memory job registry for async YouTube downloads. No persistence on purpose:
# Render restarts clear it, and clients re-submit if they lose it mid-flight.
_URL_JOBS: dict[str, dict] = {}
_JOB_TTL = timedelta(hours=1)


def _cleanup_jobs() -> None:
    cutoff = datetime.utcnow() - _JOB_TTL
    for jid in list(_URL_JOBS):
        if _URL_JOBS[jid].get("created_at", datetime.utcnow()) < cutoff:
            _URL_JOBS.pop(jid, None)


def _job(job_id: str) -> dict:
    _cleanup_jobs()
    job = _URL_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired")
    return job


class UrlRequest(BaseModel):
    url: str = Field(..., description="YouTube (or other yt-dlp supported) URL")
    language_code: str | None = None
    speakers_expected: int | None = Field(
        default=None, ge=2, le=10, description="Optional hint for diarization"
    )
    speaker_type: str | None = Field(
        default=None, pattern="^(name|role)$",
        description="AssemblyAI Speaker Identification: 'name' or 'role'"
    )
    speakers: list[dict] | None = Field(
        default=None, description="e.g. [{'role': 'Host'}, {'role': 'Guest'}] or [{'name': ...}, ...]"
    )


async def _submit(audio_path: str, req: UrlRequest) -> dict:
    try:
        transcript_id = await assembly.submit_audio(
            audio_path,
            language_code=req.language_code,
            speakers_expected=req.speakers_expected,
            speaker_type=req.speaker_type,
            speakers=req.speakers,
        )
    except assembly.AssemblyError as exc:
        raise HTTPException(502, f"Submit failed: {exc.message}") from exc
    return {"transcript_id": transcript_id, "status": "queued"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/api/transcribe/url", status_code=202)
async def transcribe_url(req: UrlRequest) -> dict:
    """Download audio from a YouTube URL in the background; return a job id.

    Downloading the m4a audio can take ~30-60 s on free-tier CPU, so we hand
    back a job id immediately and let the client poll /api/job/{id}.
    """
    if not req.url.strip():
        raise HTTPException(400, "URL is required")

    job_id = uuid.uuid4().hex
    _URL_JOBS[job_id] = {
        "status": "downloading",
        "transcript_id": None,
        "error": None,
        "created_at": datetime.utcnow(),
    }
    asyncio.create_task(_process_url_job(job_id, req))
    return {"job_id": job_id, "status": "downloading"}


async def _process_url_job(job_id: str, req: UrlRequest) -> None:
    job = _URL_JOBS[job_id]
    try:
        loop = asyncio.get_running_loop()
        audio_path, meta = await loop.run_in_executor(
            None, _download_audio, req.url.strip()
        )
        job["meta"] = meta
    except Exception as exc:  # yt-dlp raises many error types
        job["error"] = f"Failed to download audio: {exc}"
        job["status"] = "error"
        logger.warning("job %s download failed: %s", job_id, exc)
        return

    try:
        result = await _submit(audio_path, req)
        job["transcript_id"] = result["transcript_id"]
        job["status"] = "transcribing"
    except Exception as exc:
        job["error"] = str(exc)
        job["status"] = "error"
        logger.warning("job %s submit failed: %s", job_id, exc)
    finally:
        Path(audio_path).unlink(missing_ok=True)


@app.get("/api/job/{job_id}")
async def job_status(job_id: str) -> dict:
    """Poll for a URL-job. Returns transcript_id once download+submit is done."""
    job = _job(job_id)
    payload = {"job_id": job_id, "status": job["status"]}
    if job["transcript_id"]:
        payload["transcript_id"] = job["transcript_id"]
    if job.get("meta"):
        payload["meta"] = job["meta"]
    if job["error"]:
        payload["error"] = job["error"]
    return payload


@app.post("/api/transcribe/upload", status_code=202)
async def transcribe_upload(
    file: UploadFile = File(...),
    language_code: str | None = None,
    speakers_expected: int | None = None,
    speaker_type: str | None = Form(None),
    speakers: str | None = Form(None),
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

        speakers_parsed = None
        if speakers:
            try:
                speakers_parsed = json.loads(speakers)
            except ValueError as exc:
                raise HTTPException(422, "speakers must be valid JSON") from exc

        req = UrlRequest(
            url="upload",
            language_code=language_code,
            speakers_expected=speakers_expected,
            speaker_type=speaker_type,
            speakers=speakers_parsed,
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


class ArchiveSaveRequest(BaseModel):
    source_type: str = Field(..., pattern="^(youtube|upload)$")
    source_url: str = ""
    external_key: str = Field(..., description="Dedupe key: youtube video id, or upload hash")
    title: str | None = None
    creator: str | None = None
    duration_seconds: int | None = None
    language: str | None = None
    transcript_id: str | None = Field(
        default=None, description="AssemblyAI transcript id (if not supplying utterances)"
    )
    utterances: list[dict] | None = Field(default=None)
    speaker_mapping: dict | None = Field(default=None)


@app.post("/api/archive", status_code=201)
async def archive_save(req: ArchiveSaveRequest) -> dict:
    """Persist a completed transcript to Supabase (upsert by external_key).

    Either provide ``transcript_id`` (server fetches from AssemblyAI, must be
    completed) or pass ``utterances`` directly.
    """
    utterances = req.utterances
    speaker_mapping = req.speaker_mapping

    if req.transcript_id and utterances is None:
        try:
            data = await assembly.get_transcript(req.transcript_id)
        except assembly.AssemblyError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        if data.get("status") != "completed":
            raise HTTPException(409, "transcript not completed yet")
        utterances = data["utterances"]
        speaker_mapping = data.get("speaker_mapping")

    if not utterances:
        raise HTTPException(422, "no utterances to save")

    row = {
        "external_key": req.external_key,
        "source_type": req.source_type,
        "source_url": req.source_url,
        "title": req.title,
        "creator": req.creator,
        "duration_seconds": req.duration_seconds,
        "language": req.language,
        "utterances": utterances,
        "speaker_mapping": speaker_mapping,
    }
    try:
        saved = await archive.upsert_transcript(row)
    except archive.ArchiveError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"archive_id": saved["id"]}


@app.get("/api/archive")
async def archive_list(limit: int = 100) -> dict:
    """List archived transcripts, newest first."""
    try:
        rows = await archive.list_transcripts(limit=min(limit, 200))
    except archive.ArchiveError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"rows": rows}


@app.get("/api/archive/{archive_id}")
async def archive_get(archive_id: str) -> dict:
    try:
        row = await archive.get_transcript(archive_id)
    except archive.ArchiveError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    if not row:
        raise HTTPException(404, "archived transcript not found")
    return row


@app.patch("/api/archive/{archive_id}")
async def archive_patch(archive_id: str, updates: dict) -> dict:
    """Overwrite speaker_mapping (and optionally label names) in the archive."""
    allowed = {k: updates[k] for k in ("speaker_mapping",) if k in updates}
    if not allowed:
        raise HTTPException(422, "no supported fields provided")
    try:
        row = await archive.update_speaker_mapping(archive_id, allowed["speaker_mapping"])
    except archive.ArchiveError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    if not row:
        raise HTTPException(404, "archived transcript not found")
    return row


def _download_audio(url: str) -> tuple[str, dict]:
    """Download audio (native m4a, no transcode); return (path, metadata).

    Uses cookies from ``YTDLP_COOKIES`` if present, which bypasses YouTube's
    "confirm you're not a bot" check on datacenter IPs. The metadata dict holds
    title/creator/duration for archival.
    """
    tmp_dir = tempfile.mkdtemp(prefix="st_")
    ydl_opts: dict = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": os.path.join(tmp_dir, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    cookies_b64 = os.environ.get("YTDLP_COOKIES")
    if cookies_b64:
        cookie_path = os.path.join(tmp_dir, "cookies.txt")
        Path(cookie_path).write_bytes(base64.b64decode(cookies_b64))
        ydl_opts["cookiefile"] = cookie_path

    # YouTube's JS n-challenge: solve it using the JS runtime yt-dlp auto-picks
    # (deno preferred, node fallback — both shipped in the Docker image) plus
    # the community challenge solver, so format selection gets actual media
    # formats instead of images. Allow override for debugging.
    runtime = os.environ.get("YTDLP_JSC")
    ydl_opts["remote_components"] = ("ejs:github",)
    if runtime:
        ydl_opts["extractor_args"] = {"youtube": {"jsc": [runtime]}}

    info = None
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    meta = {
        "title": (info or {}).get("title"),
        "creator": (info or {}).get("channel") or (info or {}).get("uploader"),
        "duration_seconds": (info or {}).get("duration"),
    }
    candidates = sorted(p for p in Path(tmp_dir).iterdir() if p.name.startswith("audio."))
    if not candidates:
        raise RuntimeError("yt-dlp produced no audio files")
    return str(candidates[0]), meta


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
