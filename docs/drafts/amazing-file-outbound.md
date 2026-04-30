# Amazing File Outbound — Design Context

**Status**: Draft — MVP-in-progress
**Created**: 2026-04-21
**Owner**: @pavshulin
**Scope**: Outbound file sharing (agent → user) via Trinity-hosted public URL

> This is the running context doc for the feature. MVP defaults are locked (see §4). Open questions are tracked in §9 and resolved as we build/test. Update this file as decisions change.

---

## 1. Origin & Motivation

Agents generate artifacts (CSVs, reports, images, PDFs, exports, code bundles) but today have no first-class way to hand them to a user. Current workarounds:

- Fenced code blocks in chat → ugly, truncated, not downloadable.
- `#222` inbound Slack files (user → agent) — already shipped, wrong direction.
- `#282` outbound Slack-only (extract code block, upload to Slack via V2 API) — only handles code blocks, only Slack, size-limited.
- Base64 data URIs in chat — fragile, terrible UX.

The gap — **generic outbound file delivery that works across Slack, Telegram, public chat, and email** — was scoped as `#295` (FILES-001: Platform file storage + one-time download links) but never started.

### The proposal (user, 2026-04-21)

> Build file sharing on top of the public-link mechanism we already have. The agent writes a file to a volume, Trinity mints a URL (same style as `/chat/{token}`), and later we wire that URL into Slack so the agent can "post a file" in Slack by simply posting the URL.

Direction: **outbound-only first** (agent → user). Inbound (user → agent uploads via a URL) is explicitly out of scope for Phase 1.

---

## 2. What Already Exists (reuse surface)

| Capability | Where | Reusable for this feature? |
|------------|-------|---------------------------|
| Token-scoped public URLs | `agent_public_links` + `routers/public_links.py` + `routers/public.py` | ✅ Exact pattern to clone |
| `secrets.token_urlsafe(24)` token generator | `routers/public_links.py:66` | ✅ |
| External URL building | `_build_external_url()` in `routers/public_links.py` | ✅ |
| IP rate limiting with trusted-proxy handling | `check_public_link_rate_limit()` in `routers/public.py` | ✅ |
| Per-agent Docker volumes with ownership fix | `agent_shared_folder_config` + `services/agent_service/crud.py` (alpine chown 1000:1000) | ✅ Mirror this pattern for publish volume |
| Container recreation on volume change | `check_shared_folder_mounts_match()` + `recreate_container_with_updated_config()` | ✅ |
| Unified channel access gate (require_email / open_access) | `routers/public.py`, `message_router.py` (#311) | ✅ Gate downloads the same way |
| MCP tool scaffolding | `src/mcp-server/src/tools/*.ts` | ✅ |
| Platform audit trail | `PlatformAuditService` (SEC-001) | ✅ Emit `FILE_SHARE_CREATE`, `FILE_SHARE_DOWNLOAD` |
| Slack adapter send path | `slack_adapter.send_response()` | ✅ Phase 2 hook point |

**Bottom line**: every primitive we need is already in the codebase. This feature is mostly wiring.

---

## 3. Related Issues

| # | State | Relationship |
|---|-------|--------------|
| **#295** | OPEN P1 | FILES-001. Same goal, different implementation. This doc effectively supersedes it. **Action**: post a comment on #295 linking to this draft when Phase 1 lands; update #295's body to reference the implementation path, then close when MVP ships. |
| **#354** | CLOSED 2026-04-16 | File upload for external channels — Telegram Phase 1 shipped; Slack + public-link inbound parts dropped without successor issue. Tangential (inbound). |
| **#364** | OPEN P2 | Web chat file upload (inbound). Orthogonal. |
| **#222** | CLOSED 2026-03-31 | Slack inbound file sharing. Orthogonal. |
| **#282** | CLOSED 2026-04-08 | Slack outbound via code-block extraction + native Slack upload. Phase 3 question: retire for files > threshold, keep for small code blocks. |
| **#237** | CLOSED | Slack file download bug fix. Context. |

### 3a. #295 cross-reference — what we adopt, what we change

| Aspect | #295 | This MVP | Decision |
|--------|------|----------|----------|
| MCP tool shape | `upload_file(file_path, filename, content_type?, expires_in?, one_time?)` | `share_file(filename, display_name?, expires_in?, one_time?)` | **Adopt** `expires_in` and `one_time` from #295 (see §5.5). |
| Storage layout | Flat `/data/files/{uuid}` | Per-agent `/data/agent-public/{name}/` | **Diverge** — per-agent is better for quotas, cleanup, and tenant isolation; mirrors shared-folders. |
| Token | HMAC-SHA256 signed, stateless | Random `token_urlsafe(32)` stored in DB | **Diverge** — DB tokens make revocation trivial and match public-chat precedent; revisit if download QPS is ever a concern. |
| Default lifecycle | One-time, 24h | Reusable, 7d (agent can opt into one-time per call) | **Diverge** — user preference; one-time available via flag. |
| Access model | "Token is the auth" (anonymous) | Token + inherit agent channel-access policy (`require_email` / `open_access`) | **Diverge (stricter)** — if owner email-gated the agent, file URLs run the same gate; no side door. Behavior identical to #295 when `open_access=true`. |
| File size cap | 100 MB | 50 MB (setting-configurable) | Minor. |
| Per-agent quota | Not specified | 500 MB (setting-configurable) | **Added.** |
| Listing / delete endpoints | Yes | Yes | Aligned. |
| Executable blocklist, streamed, outside web root | Yes | Yes | Aligned. |

---

## 4. Locked MVP Defaults

| Question | Decision | Rationale |
|----------|----------|-----------|
| **Q1** Which link? | Inherit behavior of existing public link (`agent_public_links`). Access policy (`require_email`, `open_access`) is read from agent ownership, same source of truth as public chat. | User answered 2026-04-21: "yes inherit behaviour". |
| **Q2** Who creates? | Outbound only (agent → user). Inbound deferred. | User answered 2026-04-21. |
| **Q3** Storage | Per-agent Docker-managed volume `agent-{name}-public` mounted **only** into the agent at `/home/developer/public/` (same pattern as `shared-folders`). On `share_file`, backend uses Docker SDK `get_archive()` to stream the named file out, extracts it, and stores at `/data/agent-files/{file_id}` under the existing `trinity-data` mount. Download endpoint serves from `/data/agent-files/{file_id}`. | **Zero docker-compose changes in dev or prod.** Backend never sees the agent's filesystem directly — only files the agent explicitly names via MCP (tightest blast radius). Matches existing `get_archive` usage in credential_service, agent_files, audit MCP tool. |
| **Q4** Link lifecycle | Reusable (not one-time). 7-day expiration after last download. Manually revocable from UI. | Matches user mental model of "here's a link to my report"; one-time surprises users and conflicts with Slack unfurl races. |
| **Q5** Size limits | 50 MB per file; 500 MB per agent total; oldest auto-expires first on quota breach. | Fits typical report/export sizes, well under Slack's 1 GB workspace ceiling. |
| **Q6** Email gating | Inherit agent's channel-access policy. If `require_email=true`, download requires a valid `session_token` (same one the public chat uses). If `open_access=true`, anonymous allowed. | One source of truth; no side-door around owner's policy. |
| **Q7** Slack integration | Phase 2 only. MVP ships without Slack-specific handling. Agents return the URL as plain text in chat; Slack's native unfurl will preview it. | Don't block MVP on channel-specific work; decide replace-vs-complement of #282 later based on testing. |

---

## 5. Architecture (MVP)

### 5.1 Data flow (happy path)

```
Agent container                Backend                          User
---------------                -------                          ----
1. Agent writes file to
   /home/developer/public/report.csv
   (Docker-managed volume
    agent-{name}-public, mounted
    ONLY into the agent)

2. Agent calls MCP tool
   share_file("report.csv")
        │
        ▼
   MCP server → POST /api/internal/agent-files/share
   (Header: X-Internal-Secret, Agent-scoped MCP key identifies agent)
        │
        ▼
3.                            Backend validates:
                              - Path relative, no traversal, under /home/developer/public/
                              - docker.containers.get(agent-{name}).get_archive(path)
                              - Streams tar, extracts single file to /data/agent-files/{file_id}
                              - Size ≤ 50 MB (checked as bytes stream)
                              - Agent quota ≤ 500 MB
                              - python-magic MIME detection on extracted bytes
                              - Not in blocklist (PE/ELF/Mach-O)

                              Inserts into agent_shared_files
                              (file_id = uuid, download_token = token_urlsafe(32))

                              Returns {file_id, url, expires_at}
        ▼
4. Agent includes URL in
   its response to user:
   "Here's your report: https://trinity.example.com/files/{file_id}?t={token}"

                                                                 5. User clicks URL
                                                                        │
                                                                        ▼
                              GET /api/files/{file_id}?t={token}
                              - Validate token (constant-time compare)
                              - Check expiration / revocation / consumption
                              - Check agent's access policy:
                                • open_access=true → allow
                                • require_email=true → check session_token query param
                                  against public_link_verifications
                                • else → allow (owner's link is assumed public by default)
                              - Rate limit by IP
                              - Stream /data/agent-files/{file_id} with:
                                • Content-Disposition: attachment; filename="..."
                                • X-Content-Type-Options: nosniff
                                • Content-Type: {server-detected MIME}
                              - Emit FILE_SHARE_DOWNLOAD audit event
                              - Update last_downloaded_at, download_count
                                                                        ▼
                                                                 User saves file
```

### 5.2 Components to add

| Layer | File | Notes |
|-------|------|-------|
| Schema | `src/backend/db/schema.py` | New `agent_shared_files` table + indexes |
| Migration | `src/backend/db/migrations.py` | Versioned migration |
| DB ops | `src/backend/db/agent_shared_files.py` | `AgentSharedFilesOperations` class (Invariant #2) |
| Service | `src/backend/services/agent_shared_files_service.py` | Path validation, MIME detection, quota enforcement |
| Internal router (agent-auth) | `src/backend/routers/internal.py` (extend) | `POST /api/internal/agent-files/share` |
| Public router (token-auth) | `src/backend/routers/files.py` (new) | `GET /api/files/{id}` with streaming response |
| Admin router (JWT) | `src/backend/routers/agent_files.py` (extend) | `GET /api/agents/{name}/shared-files`, `DELETE /api/agents/{name}/shared-files/{id}` |
| MCP tool | `src/mcp-server/src/tools/agents.ts` (extend) | `share_file` tool |
| Volume mgmt | `src/backend/services/agent_service/crud.py` + `lifecycle.py` | Create/attach Docker-managed volume `agent-{name}-public` on agent start when feature is enabled (mirrors `shared-folders` expose pattern). Backend does NOT mount this volume. |
| Settings toggle | `agent_ownership` column `file_sharing_enabled` (default 0) | Owner opts in per-agent |
| UI | `src/frontend/src/components/SharingPanel.vue` or new `FileSharingPanel.vue` | List/revoke shared files, toggle feature on/off |
| Cleanup | `src/backend/services/cleanup_service.py` | Add sweep for expired rows + disk deletion |

### 5.3 URL structure

```
External:  https://{public_chat_url}/files/{file_id}?t={download_token}
                                                    &s={session_token}   ← only when agent requires email
Internal:  http://localhost:8000/api/files/{file_id}?t={download_token}
```

Under `/files/` (frontend route proxying to backend) — **not** under `/api/agents/{name}/...` — because users don't have an agent name in their hand. Matches public chat's `/chat/{token}` pattern.

### 5.4 DB schema (draft)

```sql
CREATE TABLE IF NOT EXISTS agent_shared_files (
    id              TEXT PRIMARY KEY,              -- uuid
    agent_name      TEXT NOT NULL,
    filename        TEXT NOT NULL,                 -- original display name for download
    stored_filename TEXT NOT NULL,                 -- UUID filename under /data/agent-files/
    size_bytes      INTEGER NOT NULL,
    mime_type       TEXT,                          -- python-magic detected
    download_token  TEXT UNIQUE NOT NULL,          -- secrets.token_urlsafe(32)
    created_by      TEXT NOT NULL,                 -- agent_name (or user_id if admin created)
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,                 -- created_at + expires_in (default 7d)
    revoked_at      TEXT,                          -- set if manually revoked
    one_time        INTEGER DEFAULT 0,             -- 1 = invalidate after first download
    consumed_at     TEXT,                          -- set on first successful GET when one_time=1
    download_count  INTEGER DEFAULT 0,
    last_downloaded_at TEXT,
    FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
        ON DELETE CASCADE ON UPDATE CASCADE
);
-- ON UPDATE CASCADE is belt-and-suspenders: rename_agent() in db/agent_settings/metadata.py
-- also explicitly UPDATEs this table (same pattern as the other 16 tables it cascades through).

CREATE INDEX idx_agent_files_agent ON agent_shared_files(agent_name);
CREATE INDEX idx_agent_files_token ON agent_shared_files(download_token);
CREATE INDEX idx_agent_files_expires ON agent_shared_files(expires_at) WHERE revoked_at IS NULL;
```

Plus one column on `agent_ownership`:

```sql
ALTER TABLE agent_ownership ADD COLUMN file_sharing_enabled INTEGER DEFAULT 0;
```

### 5.5 MCP tool contract

```typescript
share_file({
    filename: string,       // Relative path in /home/developer/public/
    display_name?: string,  // Override download filename (default: basename(filename))
    expires_in?: number,    // Seconds until expiration. Default 604800 (7d). Min 60s, max 604800s.
}): {
    file_id: string,
    url: string,
    expires_at: string,
    size_bytes: number,
    mime_type: string,
}
```

> **Deferred (2026-04-24)**: `one_time: boolean` is omitted from the MVP MCP tool + request/response and from the download endpoint's consumption path. The schema columns (`one_time`, `consumed_at`) are retained so the feature can be added back without a migration. See §9 OQ9 and the Decision Log.

Errors: `FILE_NOT_FOUND`, `PATH_TRAVERSAL`, `SIZE_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `FEATURE_DISABLED`, `MIME_BLOCKED`, `INVALID_EXPIRATION`.

---

## 6. Security Review (OWASP-targeted)

| # | Threat | Mitigation in MVP |
|---|--------|-------------------|
| S1 | Path traversal — agent tries `share_file("../.env")` | Reject absolute paths. Normalize and require the resolved path to begin with `/home/developer/public/`. Reject any path containing `..` segments after normalization. `get_archive` call happens inside the agent container's own context — even if path validation slipped, the agent can only name files it already has read access to, not backend files. |
| S2 | Credential leak via backend reach | Backend never mounts the agent workspace; it extracts only the single file the agent explicitly names via MCP. The extracted file is the only artifact the backend ever touches. |
| S3 | Predictable tokens | `secrets.token_urlsafe(32)` (192-bit entropy). Constant-time compare on download. |
| S4 | Link leaked to public archives (Slack indexers, bots) | 7-day expiration; revocable; audit log of every download with IP + UA. |
| S5 | Slack unfurl pre-consumes `one_time=true` link | N/A for MVP (one-time deferred). When re-added: detect `User-Agent: Slackbot-LinkExpanding` (and equivalents for other crawlers) and **return the file without marking `consumed_at`**. Only non-crawler GETs consume. |
| S6 | XSS via agent-uploaded HTML served inline | Force `Content-Disposition: attachment`; `X-Content-Type-Options: nosniff`; CSP `default-src 'none'`; consider serving from cookieless subdomain (post-MVP). |
| S7 | Filename header injection (CRLF) | Strip control chars; RFC 6266 quoting; fallback `file-{id}.bin` if sanitization fails. |
| S8 | MIME spoofing | python-magic server-side detection; reject PE/ELF/Mach-O; serve with detected MIME, not agent-claimed. |
| S9 | Storage DoS (runaway agent fills disk) | Per-file 50 MB cap, per-agent 500 MB quota, global cap via setting. Oldest expires first on quota breach. Cleanup every 60s. |
| S10 | Enumeration / brute force | 192-bit tokens (infeasible). IP rate limit reuses `check_public_link_rate_limit` (30/min). |
| S11 | Cross-tenant download | `file_id` alone addresses files; agent_name is looked up from DB row, never accepted from URL. |
| S12 | SSRF via URL-based file sharing | Not a vector — agent writes to local volume only. No URL-fetch code path. |
| S13 | Audit gaps | `FILE_SHARE_CREATE` (agent, file_id, size, MIME, filename) + `FILE_SHARE_DOWNLOAD` (IP, UA, success). |
| S14 | Access-policy bypass (require_email agent, file URL skips it) | Download endpoint runs the same gate helpers as `/api/public/chat/{token}`. Unified gate. |
| S15 | Agent impersonation via MCP | `share_file` takes no `agent_name` param; backend reads it from the MCP key's agent scope. Agent A cannot share from Agent B's volume. |
| S16 | Log leakage of content | Only metadata logged (file_id, size, MIME, requester); never content. |
| S17 | One-time race (two GETs arriving simultaneously on `one_time=true` link) | N/A for MVP (one-time deferred). When re-added: atomic `UPDATE agent_shared_files SET consumed_at=? WHERE id=? AND consumed_at IS NULL RETURNING id` — first winner streams, losers 410 Gone. |

### Architectural invariants checked

- ✅ **#1 Three-layer backend**: router → service → db operations
- ✅ **#2 Class-per-domain DB ops**: new `AgentSharedFilesOperations`
- ✅ **#3 Schema in `db/schema.py`, versioned migration**: yes
- ✅ **#4 Router registration order**: new `/api/files/{id}` is top-level, no conflict with `/api/agents/{name}/...`
- ✅ **#8 Auth pattern**: internal endpoint uses `X-Internal-Secret` + MCP agent scope; admin endpoints use `Depends(get_current_user)`; public download is token-scoped (matches public chat precedent)
- ✅ **#11 Docker as source of truth**: metadata in SQLite, Docker volume is storage only
- ✅ **#13 MCP/backend/agent three surfaces**: MCP TypeScript + backend router both added
- ✅ **#14 Pydantic models centralized**: add `ShareFileRequest`, `ShareFileResponse` to `models.py`

---

## 7. Phased Rollout

### Phase 1 — MVP (this iteration)

Ordered by dependency. Each step is independently testable before moving to the next.

#### Step 1 — Schema + migration (~30 min)

**Add**:
- `agent_shared_files` table (see §5.4) in `src/backend/db/schema.py` (add to `TABLES` dict; index declarations into the indexes list).
- `agent_ownership.file_sharing_enabled INTEGER DEFAULT 0` column.
- Versioned migration in `src/backend/db/migrations.py` (next migration number after current head).

**Verify locally**:
```bash
./scripts/deploy/start.sh                        # or: docker compose restart backend
docker compose logs backend | grep -i migration  # confirms migration ran
sqlite3 ~/trinity-data/trinity.db ".schema agent_shared_files"
sqlite3 ~/trinity-data/trinity.db "SELECT file_sharing_enabled FROM agent_ownership LIMIT 1"
```

**Gate**: schema present, `file_sharing_enabled` column default 0 on existing agents. No runtime impact.

---

#### Step 2 — Publish volume + opt-in toggle (~1.5 h)

**Add**:
- `PUT /api/agents/{name}/file-sharing` — body `{enabled: bool}`. Owner-only (use `can_user_share_agent`). Writes `agent_ownership.file_sharing_enabled`, returns `{restart_required: true}`.
- `GET /api/agents/{name}/file-sharing` — returns `{enabled, volume_attached, file_count, total_bytes, quota_bytes}`.
- Extend `services/agent_service/crud.py` and `lifecycle.py`: when `file_sharing_enabled=1`, create Docker-managed volume `agent-{name}-public` and mount only into the agent at `/home/developer/public/` (rw). Apply alpine chown-1000 fix. **No backend-side mount.**
- Extend `check_shared_folder_mounts_match()` (or add `check_public_folder_mount()`) so container recreates on toggle.
- Ensure `agents.py` DELETE handler removes the `agent-{name}-public` volume alongside `agent-{name}-shared`.

**Verify locally** (no compose rebuild needed — backend code changes auto-reload):
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token -d "username=admin&password=$ADMIN_PASSWORD" | jq -r .access_token)
curl -s -X POST http://localhost:8000/api/agents -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"filetest","template":"local:default"}'

curl -s -X PUT http://localhost:8000/api/agents/filetest/file-sharing \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"enabled": true}'

curl -s -X POST http://localhost:8000/api/agents/filetest/stop -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://localhost:8000/api/agents/filetest/start -H "Authorization: Bearer $TOKEN"

docker volume ls | grep agent-filetest-public                             # volume exists
docker exec agent-filetest ls -la /home/developer/public                  # mounted rw, owned by 1000
docker exec agent-filetest sh -c 'echo hi > /home/developer/public/x.txt' # writable by agent
# Backend MUST NOT have /data/agent-public/ — this is intentional:
docker exec trinity-backend ls /data/agent-public 2>&1                    # expect: No such file or directory
```

**Gate**: owner can toggle, agent has `/home/developer/public/` (writable), backend has **no direct view** of it.

---

#### Step 3 — Backend `share` endpoint (~2 h)

**Add**:
- `src/backend/db/agent_shared_files.py` — `AgentSharedFilesOperations` class (Invariant #2): `create`, `get_by_id`, `get_by_token`, `list_by_agent`, `mark_downloaded`, `consume_one_time_atomic`, `revoke`, `delete_expired`, `total_bytes_for_agent`.
- `src/backend/services/agent_shared_files_service.py`:
  - `validate_publish_path(filename)` — reject absolute paths; normalize; reject `..` segments after normalization; final form must start with `/home/developer/public/` when combined.
  - `extract_from_agent(agent_name, filename) -> bytes` — uses Docker SDK `get_archive` to pull the tar, extracts a single regular file (rejects symlinks/dirs/devices), caps bytes read at 50 MB + 1 to detect oversize early.
  - `detect_mime(bytes) -> str` — python-magic (already installed via #354).
  - `check_mime_blocklist(mime)` — reject PE/ELF/Mach-O/shell scripts.
  - `enforce_quota(agent_name, new_bytes)` — sum `size_bytes` where `revoked_at IS NULL AND (consumed_at IS NULL OR expires_at > now())`.
  - `create_share(agent_name, filename, display_name, expires_in, one_time)` — orchestrator: validate path → extract → MIME detect → quota → write to `/data/agent-files/{file_id}` → DB insert → return `(file_id, url, expires_at, size_bytes, mime_type, one_time)`.
- `src/backend/routers/internal.py` — add `POST /api/internal/agent-files/share` gated by `X-Internal-Secret` + agent-scoped MCP key (agent_name from context; body contains filename/options only).
- Pydantic `ShareFileRequest` / `ShareFileResponse` in `src/backend/models.py`.

**Verify locally**:
```bash
# Drop a file in the agent's publish volume
docker exec agent-filetest sh -c 'printf "a,b\n1,2\n" > /home/developer/public/test.csv'

# Agent-scoped call (X-Internal-Secret + agent MCP key)
AGENT_KEY=$(docker exec agent-filetest printenv TRINITY_MCP_API_KEY)
INTERNAL_SECRET=$(docker compose exec backend printenv INTERNAL_API_SECRET | tr -d '\r')
curl -s -X POST http://localhost:8000/api/internal/agent-files/share \
  -H "X-Internal-Secret: $INTERNAL_SECRET" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"filename":"test.csv","expires_in":604800,"one_time":false}'
# → {"file_id":"...","url":"http://localhost/files/.../?t=...","expires_at":"...",...}

# Abuse cases
curl ... -d '{"filename":"/etc/passwd"}'           # 400 PATH_TRAVERSAL (absolute)
curl ... -d '{"filename":"../.env"}'               # 400 PATH_TRAVERSAL (escape)
curl ... -d '{"filename":"nonexistent.csv"}'       # 404 FILE_NOT_FOUND
ln -s /etc/passwd /home/developer/public/link.txt  # via docker exec
curl ... -d '{"filename":"link.txt"}'              # 400 NOT_REGULAR_FILE

# Quota test
docker exec agent-filetest sh -c 'dd if=/dev/zero of=/home/developer/public/big.bin bs=1M count=51 2>/dev/null'
curl ... -d '{"filename":"big.bin"}'               # 413 SIZE_LIMIT_EXCEEDED

# Verify DB + on-disk artifact
sqlite3 ~/trinity-data/trinity.db "SELECT id,filename,size_bytes,mime_type,one_time FROM agent_shared_files"
docker compose exec backend ls -la /data/agent-files/
```

**Gate**: can register a valid file → get URL; all abuse cases return correct HTTP code.

---

#### Step 4 — Public download endpoint (~1.5 h)

**Add**:
- `src/backend/routers/files.py` — new top-level router. `GET /api/files/{file_id}` with `?t={token}&s={session_token}` query.
- Constant-time token compare (`secrets.compare_digest`).
- Policy gate: if agent has `require_email=1`, require `session_token` matching a valid row in `public_link_verifications` (reuse `_agent_requires_email` helper from `routers/public.py`).
- `StreamingResponse` with chunked read from `/data/agent-files/{stored_filename}`.
- Headers: `Content-Disposition: attachment; filename="sanitized"`, `X-Content-Type-Options: nosniff`, `Content-Type: {detected_mime}`, `Cache-Control: private, no-store`.
- Atomic consume for `one_time=true` via DB RETURNING.
- Rate-limit by IP (reuse `check_public_link_rate_limit`).
- Emit `FILE_SHARE_DOWNLOAD` audit event.
- Register router in `main.py` **before** any `/api/agents/{name}` catch-alls (Invariant #4).

**Verify locally**:
```bash
# Happy path — open URL from Step 3 in browser: should download test.csv
# Or via curl
curl -v "http://localhost:8000/api/files/<FILE_ID>?t=<TOKEN>" -o /tmp/downloaded.csv
diff /tmp/downloaded.csv <(docker exec agent-filetest cat /home/developer/public/test.csv)

# Wrong token → 404
curl -v "http://localhost:8000/api/files/<FILE_ID>?t=WRONG"

# Revoked → 410
sqlite3 ~/trinity-data/trinity.db "UPDATE agent_shared_files SET revoked_at=datetime('now') WHERE id='<FILE_ID>'"
curl -v "http://localhost:8000/api/files/<FILE_ID>?t=<TOKEN>"

# Expired → 410
sqlite3 ~/trinity-data/trinity.db "UPDATE agent_shared_files SET expires_at=datetime('now','-1 hour') WHERE id='<FILE_ID2>'"
curl -v "http://localhost:8000/api/files/<FILE_ID2>?t=<TOKEN>"

# One-time consumed twice → second request 410
curl "http://localhost:8000/api/files/<ONE_TIME_ID>?t=<TOKEN>"   # 200
curl "http://localhost:8000/api/files/<ONE_TIME_ID>?t=<TOKEN>"   # 410

# Slackbot unfurl simulation (should NOT consume)
curl -H "User-Agent: Slackbot-LinkExpanding 1.0" "http://localhost:8000/api/files/<ONE_TIME_ID>?t=<TOKEN>"
sqlite3 ~/trinity-data/trinity.db "SELECT consumed_at FROM agent_shared_files WHERE id='<ONE_TIME_ID>'"
# → still NULL; real GET after still works

# Email-gated agent (if agent has require_email=1): download without session → 403
# With valid session token from public-chat verify flow → 200

# Audit
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/audit-log?event_type=file_share" | jq
```

**Gate**: all HTTP codes correct, byte-identical download, headers set, audit rows written.

---

#### Step 5 — MCP `share_file` tool (~1 h)

**Add**:
- `src/mcp-server/src/tools/agents.ts` — new `share_file` tool. Wraps `POST /api/internal/agent-files/share`.
- Schema matches §5.5. Pulls `agent_name` from `McpAuthContext.agentName` (enforce agent-scoped key).
- Rebuild MCP server.

**Verify locally**:
```bash
docker compose build mcp-server && docker compose up -d mcp-server
# From a running agent's Claude Code session (SSH or terminal tab):
claude -p 'Call the share_file MCP tool with filename="test.csv" and return the URL'
# Or test via MCP HTTP directly:
AGENT_KEY=$(docker exec agent-filetest printenv TRINITY_MCP_API_KEY)
curl -s http://localhost:8080/mcp/tools/call \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"share_file","arguments":{"filename":"test.csv"}}'
```

**Gate**: MCP tool returns valid URL; cross-agent attempt (Agent A key with Agent B filename) is refused by internal endpoint.

---

#### Step 6 — UI: toggle + file list (~3 h)

**Add**:
- `src/frontend/src/components/FileSharingPanel.vue` — toggle, quota bar, table of active shared files (filename, size, created, expires, download count, Copy URL, Revoke).
- Embed in `SharingPanel.vue` (next to Public Links section) when `agent.can_share`.
- `stores/agents.js` — `getFileSharing`, `updateFileSharing`, `listSharedFiles`, `revokeSharedFile`.

**Verify locally** (browser):
- Navigate to `http://localhost/agents/filetest` → Sharing tab → "File Sharing" section appears.
- Toggle on, restart banner shows, restart agent.
- Have agent create a file + share via MCP (reuse Step 5 session).
- File appears in UI; copy URL, download works.
- Click Revoke; file row marked revoked; URL returns 410.

**Gate**: owner can drive the full lifecycle from UI without touching CLI.

---

#### Step 7 — Cleanup task (~45 min)

**Add**:
- Extend `src/backend/services/cleanup_service.py`: new sweep `_cleanup_expired_shared_files()` on the existing 5-min tick.
  - Delete disk file at `/data/agent-files/{stored_filename}` when `expires_at < now` OR `(one_time=1 AND consumed_at IS NOT NULL)` OR `revoked_at IS NOT NULL AND revoked_at < now - 24h`.
  - Then `DELETE FROM agent_shared_files WHERE id=?`.
  - Emit audit event per cleanup batch.

**Verify locally**:
```bash
# Age a row
sqlite3 ~/trinity-data/trinity.db "UPDATE agent_shared_files SET expires_at=datetime('now','-1 day') WHERE id='<FILE_ID>'"

# Trigger cleanup manually (admin endpoint already exists)
curl -s -X POST http://localhost:8000/api/monitoring/cleanup-trigger -H "Authorization: Bearer $TOKEN"

# Verify row gone + on-disk artifact gone
sqlite3 ~/trinity-data/trinity.db "SELECT id FROM agent_shared_files WHERE id='<FILE_ID>'"   # empty
docker compose exec backend ls /data/agent-files/<STORED_FILENAME>                            # no such file
```

**Gate**: expired/consumed/revoked files are swept within one cleanup cycle.

---

### Phase 2 — Slack integration

### Phase 2 — Slack integration
- Slack adapter: when agent response body contains a Trinity file URL, optionally enrich the Slack message (e.g., explicit "📎 Download: <url>")
- Decision: retire `#282` code-block extraction for files > N KB, or keep both (decide after testing).

### Phase 1 — effort summary

| Step | Rough effort |
|------|--------------|
| 1. Schema + migration | 30 min |
| 2. Volume + toggle | 1.5 h |
| 3. Backend share endpoint | 2 h |
| 4. Public download endpoint | 1.5 h |
| 5. MCP `share_file` tool | 1 h |
| 6. UI | 3 h |
| 7. Cleanup task | 45 min |
| **Total** | **~10 h** across 1–2 sessions |

### Phase 3 — Other channels
- Telegram: same URL-in-text pattern (Telegram unfurls too)
- Public chat: render download chip instead of raw URL
- Email (future)

### Phase 4 — Extensions (backlog)
- One-time download mode
- Password-protected links
- Per-file access policy override (instead of agent-wide)
- Cookieless subdomain for XSS defense-in-depth
- User-initiated inbound (combine with #364)
- Retire/unify with #295

---

## 8. Testing Plan (local)

Golden path:
1. Create agent with `file_sharing_enabled=true`.
2. Have agent write `/home/developer/public/test.csv`.
3. Agent calls `share_file("test.csv")` via MCP.
4. Receive URL; open in browser; verify download + correct filename + correct MIME.
5. Re-download; verify counter increments.
6. Revoke via UI; verify 410 Gone.
7. Wait past expiration (or manually age); verify cleanup removes disk + row.

Abuse paths:
- `share_file("../../etc/passwd")` → rejected
- `share_file("/absolute/path")` → rejected
- `share_file("../.env")` → rejected (and volume isolation means even if it got through, the path isn't reachable)
- 51 MB file → rejected on size cap
- 11 × 50 MB files → 11th rejected on quota
- File containing PE/ELF header → rejected on MIME
- Direct GET with wrong token → 404 (no enumeration signal)
- Direct GET with expired token → 410
- Agent with `require_email=true`, GET without session token → 403

Cross-agent:
- Agent A cannot call `share_file` with a path that resolves into agent B's volume (validated by MCP scope + backend path check).

UI:
- Owner sees list of shared files, can revoke, sees per-file download count.

---

## 9. Open Questions (tracked for resolution during/after MVP)

| # | Question | Status |
|---|----------|--------|
| OQ1 | Should we retire `#282` (Slack code-block → native Slack upload) once URL-posting works, or keep both paths? | Defer to Phase 2. |
| OQ2 | Do we want a cookieless subdomain for download serving (defense-in-depth against HTML XSS)? | Defer; non-blocker for MVP. |
| OQ3 | Should revoking the agent's public chat link cascade-revoke file links, or are they independent? | **Independent** — file-sharing is a separate per-agent toggle. Revisit if coupling is ever requested. |
| OQ4 | How does `trinity-system` agent use this (if at all)? | Defer. |
| OQ5 | Quota failure UX — silent oldest-eviction or hard error to agent? | MVP: **hard error**, agent decides what to do. |
| OQ6 | Is 7 days the right default expiration, or per-agent-configurable? | MVP: 7 days hardcoded; add setting in Phase 4 if requested. |
| OQ7 | Do we need a download-page interstitial (filename, size, "click to download") for better UX + Slack-unfurl behavior? | Defer; direct stream for MVP. |
| OQ8 | Should we support overwriting an existing share (stable URL that gets new content)? | No for MVP — each `share_file` mints a new URL. Agents can revoke the old one explicitly. |
| OQ9 | One-time download links | **Deferred** (2026-04-24). Schema columns (`one_time`, `consumed_at`) retained; API / service / docs surface removed. Re-add when we have a use case driving it (e.g. sharing sensitive artifacts). |

---

## 10. Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-21 | Build atop public-link pattern, not a standalone platform-files service. | User preference; reuses hardened primitives; inherits access-control model. |
| 2026-04-21 | Outbound-only for Phase 1. | User scope-cut; inbound via #364 later. |
| 2026-04-21 | Per-agent publish Docker volume bind-mounted to backend. | Eliminates `docker cp` cost; filesystem-isolates from agent secrets. |
| 2026-04-23 | **Revised**: per-agent Docker-managed publish volume, **not** bind-mounted to backend. Backend uses Docker SDK `get_archive` on `share_file` to extract and store at `/data/agent-files/{id}` under the existing `trinity-data` mount. | Zero docker-compose changes in dev or prod (no compose drift, no ops-side filesystem prep). Backend only touches files the agent explicitly names via MCP — tightest blast radius. Trade-off: ~<1s extraction cost per share, acceptable at 50 MB cap. |
| 2026-04-23 | Use `agent_name` as the identifier (matches every other table); FK declared with `ON DELETE CASCADE ON UPDATE CASCADE`; also added explicit `UPDATE agent_shared_files` line to `rename_agent()` in `db/agent_settings/metadata.py`. | Consistency with 16 existing tables. Surrogate ID would have made this one table the outlier and added a JOIN to every list/filter query. `ON UPDATE CASCADE` + explicit `rename_agent` update is defense-in-depth so a future maintainer can't break it. |
| 2026-04-24 | One-time download links deferred. Stripped `one_time` / `consumed_at` from the MCP tool contract, request/response models, service params, and DB `create()`. Schema columns retained. Step 4 removes `consume_one_time_atomic` DB method and `is_crawler_user_agent` helper from scope. | Simplifies the first working slice; no real use case on the table today. Bringing it back later is a ~40-line change because the columns are already there. |
| 2026-04-21 | Reusable 7-day links, not one-time. | User mental model; avoids Slack unfurl race. |
| 2026-04-21 | Email-gating inherits agent's channel policy. | Single source of truth. |
| 2026-04-21 | Defer Slack-specific integration to Phase 2. | Don't block MVP on channel bikeshedding. |

---

## 11. References

- #295 — FILES-001 (original request, to be superseded)
- #354 — Inbound file upload (closed partially)
- `docs/memory/feature-flows/public-agent-links.md` — URL + access-policy pattern we're cloning
- `docs/memory/feature-flows/agent-shared-folders.md` — Docker volume pattern we're cloning
- `docs/memory/feature-flows/unified-channel-access-control.md` — Gating model
- `docs/memory/architecture.md` — Architectural invariants checked
