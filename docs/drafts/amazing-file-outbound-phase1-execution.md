# Amazing File Outbound — Phase 1 Execution Checklist

**Date**: 2026-04-24
**Branch**: `feature/295-files-outbound-sharing`
**Parent commit**: `e47c688 feat(files): FILES-001 outbound file sharing MVP (Steps 1-6)`
**Source plan**: [amazing-file-outbound-production-readiness.md](amazing-file-outbound-production-readiness.md) §3 CRITICAL

This is the narrow "what to execute right now" checklist. Each item is fix-and-ship quality; no architectural debate.

## Execution order (~2 hours total)

- [ ] **C1 — Docs pass** (~45 min)
  - `docs/memory/requirements.md` — mark FILES-001 Implemented with date + summary
  - `docs/memory/architecture.md` — bump MCP tool count (62 → 73+), add `agent_shared_files` to schema section, add the 4 new endpoints to API table
  - `docs/memory/feature-flows.md` — add index entry
  - `docs/memory/feature-flows/file-sharing-outbound.md` — **new** full feature-flow doc (UI → store → router → service → DB → download)

- [ ] **C2 — Filename length cap** (~2 min)
  - `src/backend/models.py` — `Field(max_length=255)` on `ShareFileRequest.filename` and `ShareFileMcpRequest.filename`

- [ ] **C3 — Disk-space pre-check** (~10 min)
  - `src/backend/services/agent_shared_files_service.py` — `shutil.disk_usage` check before `open().write()`; reject with 507 Insufficient Storage if free space below threshold (default 500 MB configurable)

- [ ] **C4 — Cleanup sweep (Step 7)** (~30 min)
  - `src/backend/db/agent_shared_files.py` — new method `delete_expired_and_revoked() -> list[stored_filename]` (returns disk paths to unlink)
  - `src/backend/database.py` — facade forward
  - `src/backend/services/cleanup_service.py` — call on existing 5-min tick; unlink disk files; log summary per sweep

- [ ] **C5 — Separate rate-limit bucket** (~15 min)
  - `src/backend/routers/files.py` — replace `check_public_link_rate_limit` with new `check_file_download_rate_limit` helper using key `file_downloads:{ip}`
  - Declared in same module or a small helpers file; same limits (30/min) but separate bucket so download traffic doesn't starve public chat's rate-limit quota

- [ ] **C6 — HEAD handler** (~10 min)
  - `src/backend/routers/files.py` — add `@router.head("/{file_id}")` that returns same headers as GET but no body; reuse the same auth + expiration + revocation checks

- [ ] **C7 — Tighten list/revoke to owner+admin** (~5 min)
  - `src/backend/routers/agent_files.py` — `GET /api/agents/{name}/shared-files` change `can_user_access_agent` → `can_user_share_agent`
  - `DELETE .../shared-files/{file_id}` already uses `can_user_share_agent` — verify and no-op if so
  - No API shape change; only access gate tightening

- [ ] **C8 — Agent prompt nudge** (~10 min)
  - Target file: `src/backend/services/platform_prompt_service.py` (or the system-wide prompt file — confirm during execution)
  - Add 2 lines: "When sharing files with users, write the file to `/home/developer/public/` and call the `share_file` MCP tool to get a download URL. Return the URL as-is to the user."

- [ ] **C9 — Close #295** (~2 min)
  - `gh issue comment 295` with a link to this feature-flow doc + the merged PR
  - `gh issue close 295`

## Pre-execution step (required by user request)

- [ ] Read updated project docs (requirements.md, architecture.md, feature-flows.md) via `read-docs` skill — main's recent commits touched these, need fresh context before touching them in C1.

## Post-execution verification

- [ ] All edited Python files parse (`ast.parse`)
- [ ] `pytest tests/unit/test_agent_shared_files_migration.py tests/unit/test_file_sharing_mixin.py tests/unit/test_public_folder_mount_match.py` still green (33/33)
- [ ] Backend + MCP + frontend still healthy after restart
- [ ] Repeat the 37-scenario live regression from the earlier thorough audit
- [ ] Verify agent filesystem delete + cleanup sweep both purge disk files
- [ ] Verify HEAD returns same headers as GET with empty body
- [ ] Verify shared user gets 403 on list/revoke

## Commit strategy

One commit per C-item, conventional message. Squash at PR-time. Branch stays `feature/295-files-outbound-sharing`. No push to remote until all 9 done + verified.

## Out-of-scope (filed as GitHub issues after Phase 1)

See [amazing-file-outbound-production-readiness.md](amazing-file-outbound-production-readiness.md) §4 for the full 12-item LATER list (G1–G12).
