"""Supabase persistence for transcripts via the PostgREST API.

Uses the service_role key server-side. The table lives in
``supabase/migrations/20260809_archive.sql``.
"""

import os
from typing import Any

import httpx

_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class ArchiveError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Archive error {status}: {message}")
        self.status = status
        self.message = message


def _configured() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise ArchiveError(503, "SUPABASE_URL / SUPABASE_SERVICE_KEY not configured")
    return url, key


def _headers() -> dict:
    _, key = _configured()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _request(method: str, path: str, **kwargs) -> list[dict]:
    url, _ = _configured()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, f"{url}/rest/v1{path}", headers=_headers(), **kwargs)
    if resp.status_code >= 400:
        raise ArchiveError(resp.status_code, resp.text[:300])
    return resp.json()


async def upsert_transcript(row: dict) -> dict:
    """Insert or update (by external_key) a transcript row; return the saved row."""
    rows = await _request(
        "POST",
        "/transcripts",
        json=[row],
        params={"on_conflict": "external_key"},
    )
    return rows[0] if rows else row


async def list_transcripts(limit: int = 100) -> list[dict]:
    return await _request(
        "GET",
        "/transcripts",
        params={
            "select": "id,source_type,source_url,title,creator,duration_seconds,language,created_at,updated_at",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )


async def get_transcript(archive_id: str) -> dict | None:
    rows = await _request(
        "GET",
        "/transcripts",
        params={"select": "*", "id": f"eq.{archive_id}"},
    )
    return rows[0] if rows else None


async def update_speaker_mapping(archive_id: str, mapping: dict[str, Any]) -> dict | None:
    rows = await _request(
        "PATCH",
        f"/transcripts?id=eq.{archive_id}",
        json={"speaker_mapping": mapping},
    )
    return rows[0] if rows else None
