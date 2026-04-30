"""
Public download endpoint for outbound agent file sharing (FILES-001 Step 4).

Resolves the token-scoped URLs minted by POST /api/internal/agent-files/share.
Unauthenticated — the 192-bit `sig` token IS the auth credential, minted at
share time and known only to the recipient.

Error matrix:
- 404 — file_id does not exist
- 401 — download_token missing or wrong (constant-time compare)
- 410 — revoked or expired
- 429 — IP rate limit
- 500 — storage file missing on disk
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from database import db
from services.agent_shared_files_service import STORAGE_ROOT
from services.platform_audit_service import AuditEventType, platform_audit_service
from routers.auth import get_redis_client
from routers.public import _get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])

_CHUNK_SIZE = 64 * 1024

# C5: File-download rate limit has its OWN bucket so heavy download
# traffic can't exhaust the shared public_link_lookups bucket used by
# /api/public/* endpoints. Same 60/min per IP default; configurable via
# redis key prefix.
_DOWNLOAD_RATE_LIMIT = 60         # requests per window per IP
_DOWNLOAD_RATE_WINDOW = 60        # window in seconds


def _check_file_download_rate_limit(client_ip: str) -> None:
    """
    Rate-limit GETs to /api/files/{id} per client IP.

    Fails open if Redis is unavailable (logs a warning) — same convention
    as public-link rate limiting. Uses a dedicated `file_downloads:{ip}`
    bucket so it can't starve other public endpoints.
    """
    r = get_redis_client()
    if r is None:
        logger.warning("File download rate limiting unavailable — Redis not connected")
        return
    key = f"file_downloads:{client_ip}"
    try:
        attempts = r.get(key)
        if attempts and int(attempts) >= _DOWNLOAD_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _DOWNLOAD_RATE_WINDOW)
        pipe.execute()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"File download rate limit check failed: {e}")


def _iter_file(path: str):
    """Stream a file in chunks without loading it into memory."""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


def _format_disposition(filename: str) -> str:
    """
    RFC 6266 Content-Disposition with UTF-8 fallback.
    Always `attachment` — never inline (defense against XSS via agent-uploaded HTML).
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    safe_ascii = ascii_name.replace('"', "").replace("\\", "")
    utf8_encoded = quote(filename, safe="")
    return f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{utf8_encoded}'


def _parse_expires(value: str) -> datetime:
    """Parse the ISO timestamp stored in expires_at, guaranteeing tz-aware."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _validate_download_request(
    file_id: str,
    request: Request,
    sig: Optional[str],
    download_token_alias: Optional[str],
) -> tuple:
    """
    Shared validation for GET and HEAD requests.

    Returns ``(row, storage_path, headers, mime_type, client_ip)`` on success.
    Raises HTTPException on any failure (401 / 404 / 410 / 500 / 429).
    """
    client_ip = _get_client_ip(request)
    _check_file_download_rate_limit(client_ip)  # 429 on limit (C5 — dedicated bucket)

    # Accept either `sig` (preferred) or `download_token` (legacy alias)
    token = sig or download_token_alias
    if not token:
        raise HTTPException(status_code=401, detail="sig required")

    row = db.get_agent_shared_file(file_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    # Constant-time compare to prevent timing oracles
    if not secrets.compare_digest(token, row["download_token"]):
        raise HTTPException(status_code=401, detail="invalid download_token")

    if row["revoked_at"]:
        raise HTTPException(status_code=410, detail="revoked")

    try:
        expires = _parse_expires(row["expires_at"])
    except ValueError:
        logger.error("[files] malformed expires_at on file_id=%s: %r", file_id, row.get("expires_at"))
        raise HTTPException(status_code=500, detail="storage error")
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=410, detail="expired")

    storage_path = os.path.join(STORAGE_ROOT, row["stored_filename"])
    if not os.path.exists(storage_path):
        logger.error(
            "[files] orphan DB row for file_id=%s — stored_filename=%s missing on disk",
            file_id, row["stored_filename"],
        )
        raise HTTPException(status_code=500, detail="storage error")

    headers = {
        "Content-Disposition": _format_disposition(row["filename"]),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Length": str(row["size_bytes"]),
    }
    mime_type = row["mime_type"] or "application/octet-stream"
    return row, storage_path, headers, mime_type, client_ip


@router.get("/{file_id}")
async def download_shared_file(
    file_id: str,
    request: Request,
    sig: Optional[str] = None,
    download_token: Optional[str] = None,
):
    """
    Serve a file previously registered via POST /api/internal/agent-files/share.

    Query parameters:
    - sig (required): 192-bit token minted at share time, sole auth credential.

    `download_token` is accepted as a legacy alias but deprecated —
    Trinity's credential sanitizer redacts `...TOKEN...=value` query
    pairs from agent responses, stripping the token in transit. New
    URLs emit `?sig=...`.
    """
    row, storage_path, headers, mime_type, client_ip = await _validate_download_request(
        file_id, request, sig, download_token,
    )
    agent_name = row["agent_name"]

    # Counters — best-effort
    try:
        db.mark_shared_file_downloaded(file_id)
    except Exception as e:  # pragma: no cover
        logger.warning("[files] failed to mark_downloaded for %s: %s", file_id, e)

    # Audit — best-effort
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="file_share_download",
            source="public",
            actor_ip=client_ip,
            target_type="agent",
            target_id=agent_name,
            details={
                "file_id": file_id,
                "filename": row["filename"],
                "size_bytes": row["size_bytes"],
                "mime_type": row["mime_type"],
                "user_agent": (request.headers.get("user-agent") or "")[:200],
            },
            endpoint=str(request.url.path),
        )
    except Exception as e:  # pragma: no cover
        logger.warning("[files] audit log failed for %s: %s", file_id, e)

    return StreamingResponse(
        _iter_file(storage_path),
        media_type=mime_type,
        headers=headers,
    )


@router.head("/{file_id}")
async def head_shared_file(
    file_id: str,
    request: Request,
    sig: Optional[str] = None,
    download_token: Optional[str] = None,
):
    """
    HEAD handler for link previewers / CDNs that probe before GET.

    Runs the same validation as GET (rate limit, token, expiry, revoke,
    storage presence) and returns the same headers — but no body, no
    download counter bump, no audit row. Follows RFC 7231 §4.3.2:
    HEAD is identical to GET except the server MUST NOT return a
    message-body.
    """
    _row, _storage_path, headers, mime_type, _client_ip = await _validate_download_request(
        file_id, request, sig, download_token,
    )
    return Response(status_code=200, headers=headers, media_type=mime_type)
