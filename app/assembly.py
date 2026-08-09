"""Thin async client for the AssemblyAI REST API.

Covers exactly what the tool needs: file upload, submit a transcription with
speaker diarization, poll status, and delete a transcript once done.
"""

import os

import httpx

BASE_URL = "https://api.assemblyai.com/v2"

# 25 MB is AssemblyAI's recommended chunk for uploads; sends stay single-shot here.
UPLOAD_CHUNK_SIZE = 25 * 1024 * 1024

# Timeout generous for a potentially large upload over a home connection.
_UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=30.0)
_API_TIMEOUT = httpx.Timeout(60.0, connect=30.0)


class AssemblyError(RuntimeError):
    """Raised when AssemblyAI returns a non-2xx response."""

    def __init__(self, status: int, message: str):
        super().__init__(f"AssemblyAI error {status}: {message}")
        self.status = status
        self.message = message


def _headers() -> dict:
    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise AssemblyError(401, "ASSEMBLYAI_API_KEY is not set on the server")
    return {"Authorization": key}


async def upload(audio: bytes) -> str:
    """Upload raw bytes to AssemblyAI's temp storage, return the audio_url."""
    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        resp = await client.post(
            f"{BASE_URL}/upload",
            headers={**_headers(), "Content-Type": "application/octet-stream"},
            content=audio,
        )
    if resp.status_code != 200:
        raise AssemblyError(resp.status_code, resp.text)
    return resp.json()["upload_url"]


async def create_transcript(
    audio_url: str,
    *,
    language_code: str | None = None,
    speakers_expected: int | None = None,
) -> str:
    """Submit a transcription with speaker diarization, return transcript id."""
    body: dict = {
        "audio_url": audio_url,
        "speaker_labels": True,
        "speaker_has_silence_threshold": True,
    }
    if language_code:
        body["language_code"] = language_code
    if speakers_expected:
        body["speakers_expected"] = speakers_expected

    async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
        resp = await client.post(
            f"{BASE_URL}/transcript", headers=_headers(), json=body
        )
    if resp.status_code not in (200, 201):
        raise AssemblyError(resp.status_code, resp.text)
    data = resp.json()
    if data.get("status") == "error":
        raise AssemblyError(resp.status_code, data.get("error", "unknown error"))
    return data["id"]


async def get_transcript(transcript_id: str) -> dict:
    """Return the raw transcript payload; status is queued/processing/completed/error."""
    async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
        resp = await client.get(
            f"{BASE_URL}/transcript/{transcript_id}", headers=_headers()
        )
    if resp.status_code != 200:
        raise AssemblyError(resp.status_code, resp.text)
    return resp.json()


async def delete_transcript(transcript_id: str) -> None:
    """Delete the transcript (and its uploaded source) once a caller is done with it."""
    async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
        await client.delete(
            f"{BASE_URL}/transcript/{transcript_id}", headers=_headers()
        )
