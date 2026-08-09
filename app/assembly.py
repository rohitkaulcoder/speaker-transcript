"""AssemblyAI interactions built on the official ``assemblyai`` SDK.

Flow: upload+submit (non-blocking) -> poll via ``get_by_id`` -> delete on request.
All calls run in a threadpool executor so FastAPI's event loop stays free.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import assemblyai as aai

_threadpool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="assemblyai")


class AssemblyError(RuntimeError):
    """Raised when AssemblyAI rejects a request or a transcript errors out."""

    def __init__(self, status: int, message: str, *, transcript_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.transcript_id = transcript_id


def _ensure_settings() -> None:
    """Configure the SDK once per process from the environment."""

    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise AssemblyError(401, "ASSEMBLYAI_API_KEY is not set on the server")
    aai.settings.api_key = key
    aai.settings.base_url = os.environ.get(
        "ASSEMBLYAI_BASE_URL", "https://api.assemblyai.com"
    )


def _make_config(language_code: str | None, speakers_expected: int | None) -> aai.TranscriptionConfig:
    return aai.TranscriptionConfig(
        speech_models=["universal-3-5-pro", "universal-2"],
        speaker_labels=True,
        speakers_expected=speakers_expected,
        language_code=language_code,
    )


async def _run(fn, *args):
    """Run a blocking SDK call off the event loop."""

    return await asyncio.get_running_loop().run_in_executor(_threadpool, fn, *args)


async def submit_audio(audio_path: str, *, language_code: str | None = None,
                       speakers_expected: int | None = None) -> str:
    """Upload + submit a transcription job; return the transcript id (queued).

    ``audio_path`` is a local file path — the SDK streams it to AssemblyAI in
    25 MB chunks, so large uploads never need to fit in RAM.
    """

    _ensure_settings()
    config = _make_config(language_code, speakers_expected)

    def _submit() -> aai.Transcript:
        transcriber = aai.Transcriber()
        t = transcriber.submit(audio_path, config=config)
        if t.status == aai.TranscriptStatus.error:
            raise AssemblyError(0, t.error or "transcription failed")
        return t

    try:
        transcript = await _run(_submit)
    except AssemblyError:
        raise
    except Exception as exc:  # SDK raises a mix of request/connection errors
        raise AssemblyError(502, f"Failed to submit transcription: {exc}") from exc
    return transcript.id


async def get_transcript(transcript_id: str) -> dict:
    """Fetch current transcript state; completed records include speaker utterances."""

    _ensure_settings()

    def _fetch() -> aai.Transcript:
        return aai.Transcript.get_by_id(transcript_id)

    try:
        t = await _run(_fetch)
    except Exception as exc:
        raise AssemblyError(404, f"Transcript lookup failed: {exc}") from exc

    payload = {"status": t.status, "id": t.id}
    if t.status == aai.TranscriptStatus.completed:
        payload["utterances"] = [
            {
                "speaker": u.speaker,
                "text": u.text,
                "start": u.start,
                "end": u.end,
            }
            for u in (t.utterances or [])
        ]
    elif t.status == aai.TranscriptStatus.error:
        raise AssemblyError(422, t.error or "transcription failed",
                            transcript_id=transcript_id)
    return payload


async def delete_transcript(transcript_id: str) -> None:
    """Remove the transcript (and its source audio) from AssemblyAI."""

    _ensure_settings()

    def _delete() -> None:
        aai.Transcript.delete_by_id(transcript_id)

    try:
        await _run(_delete)
    except Exception as exc:
        raise AssemblyError(404, f"Transcript delete failed: {exc}") from exc
