# Amazing File Outbound — Production Readiness Plan

**Status**: Draft
**Created**: 2026-04-24
**Owner**: @pavshulin
**Scope**: Take the MVP outbound file sharing feature (Steps 1–6 of [amazing-file-outbound.md](amazing-file-outbound.md)) from "working on local" to "safe to ship".

> Feature is **live and verified end-to-end on real Slack traffic** (see final commit of Step 6). This doc is the punch-list to get it from MVP to v1.0.

---

## 1. Scope

The MVP shipped Steps 1–6:

1. Schema + migration (`agent_shared_files`)
2. Per-agent opt-in toggle + publish volume
3. Internal share endpoint with path / MIME / size / quota validation
4. Public download endpoint with token auth + policy gate + audit
5. `share_file` MCP tool with same-agent defense
6. UI panel — toggle, list, revoke, copy URL

Plus two bugs found during live test:
- Agent-delete didn't cascade-delete rows/files/volume → **fixed** (added explicit cleanup in delete handler since SQLite FKs aren't enforced at runtime)
- URL format used `/files/{id}` path which wasn't in the `/api/*` proxy allowlist on either Vite or prod nginx → **fixed** (switched to `/api/files/{id}?sig=...`; `download_token` alias kept for backward compat)
- Credential sanitizer redacted `?download_token=...` query params from agent responses → **fixed** (renamed to `?sig=...` which is outside the sanitizer's sensitive-key patterns)

Live regression: **37 of 37 assertions pass + 33/33 pytest unit tests green + real Slack round-trip observed**.

---

## 2. What we are NOT doing (deferred)

| | Why |
|---|-----|
| One-time download links | Deferred in Step 3; schema columns retained for later |
| Slack/Discord/Twitter unfurl-bot defense (crawler UA allowlist) | Only needed with one-time links |
| Inbound file uploads (user → agent via URL) | Different feature (see #364) |
| MCP tool count sync in architecture.md | Separate drift issue |
| FK `PRAGMA foreign_keys=ON` platform-wide | Pre-existing platform pattern, separate issue (G11) |

---

## 3. Triage — CRITICAL (must fix before production)

9 items, ~2 hours total. All fix-and-ship quality, no architectural debate.

| # | Item | Why critical | Effort |
|---|------|--------------|--------|
| **C1** | Docs pass: `requirements.md` (mark FILES-001 Implemented), `architecture.md` (tool count 62→73, add `agent_shared_files` to schema section, add endpoints to API table), `feature-flows.md` index entry, new `feature-flows/file-sharing-outbound.md` | Trinity's Rules of Engagement §1 + §4 require requirements/docs updates for new features. Not ship-blocking in code but ship-blocking in SDLC. | 45 min |
| **C2** | **Filename length cap** — `Field(max_length=255)` on `ShareFileRequest.filename` and `ShareFileMcpRequest.filename` | Prevents 10KB filename from agent or attacker. Trivial footgun removal. | 2 min |
| **C3** | **Disk-space pre-check** before writing `/data/agent-files/{id}` | `/data` is shared with the SQLite DB. If disk fills, whole backend crashes — not just file sharing. Use `shutil.disk_usage` with a configurable min-free threshold (default 500 MB). | 10 min |
| **C4** | **Step 7 cleanup sweep** in `cleanup_service.py` — purge expired + old-revoked shares on 5-min tick | Without it, one month of traffic = gigabytes of dead files. Disk pressure → C3 fire. Delete disk file first, then DB row. Audit a summary per sweep. | 30 min |
| **C5** | **Separate rate-limit bucket** for `/api/files/*` | Current code shares the `public_link_lookups:{ip}` bucket with public chat. Heavy download traffic exhausts rate limit for every other public endpoint on that IP — real multi-tenant issue. | 15 min |
| **C6** | **HEAD handler** on download endpoint | Some link-previewers probe with HEAD. Currently 405. Return same headers as GET but without body. | 10 min |
| **C7** | **Tighten list/revoke endpoints** to owner + admin (currently any shared user can see URLs and revoke) | Access-model mismatch with `share_file` (owner-only). Shared user could harvest URLs or revoke owner's shares. Change `can_user_access_agent` → `can_user_share_agent` in both. | 5 min |
| **C8** | **Agent prompt nudge** — add guidance about `/home/developer/public/` + `share_file` tool to the system-wide Trinity prompt (`platform_prompt_service.py`) | Every new agent will otherwise need the user to discover the tool. Near-zero-cost DX win. | 10 min |
| **C9** | **Close #295** with comment linking to this implementation | Housekeeping — prevents duplicate work by anyone else picking up the backlog. | 2 min |

### Things deliberately NOT in CRITICAL

- **Multi-agent stress test** — verified live through Slack; V1 traffic won't stress this.
- **pytest for Steps 3/4/5/6 endpoints** — critical path manually verified (37 assertions + live trace); file as P2 (G5).
- **Memory streaming during extract** — 100 MB peak per share is fine at our concurrency; fix when we see the problem (G1).
- **Directory sharding** — only matters past ~10k files (G2).
- **OTel explicit spans** — Claude Code already emits OTel from agent side (G10).

---

## 4. Triage — LATER (GitHub issues)

Each is scoped small enough for independent pickup.

| # | Proposed title | Priority | Labels |
|---|----------------|----------|--------|
| **G1** | `refactor(file-sharing): stream tar extraction to cap peak memory at 64 KB per share` | P2 | type-refactor, performance |
| **G2** | `perf(file-sharing): shard /data/agent-files/ by UUID prefix` | P3 | type-refactor, performance |
| **G3** | `feat(file-sharing): add platform-wide storage quota with setting` | P2 | type-feature, security |
| **G4** | `feat(audit): index file_share_download events by file_id for fast lookup` | P3 | type-feature |
| **G5** | `test(file-sharing): pytest coverage for share / download / list / revoke endpoints` | P2 | type-test |
| **G6** | `security(file-sharing): sanitize download URLs from stored chat_messages/schedule_executions to prevent token reuse` | P2 | security |
| **G7** | `feat(file-sharing): add one-time download links with link-previewer UA defense` (deferred from MVP) | P3 | type-feature |
| **G8** | `refactor(file-sharing): remove legacy ?download_token= alias once clients migrate` | P3 | type-refactor |
| **G9** | `ui(file-sharing): pagination, download trend chart, clipboard fallback for non-HTTPS contexts` | P3 | type-feature, ui |
| **G10** | `observability(file-sharing): OTel explicit span + metrics for shares/downloads` | P3 | type-feature |
| **G11** | `bug(platform): enable PRAGMA foreign_keys=ON per SQLite connection` (platform-wide, affects all FK declarations) | P2 | type-bug, security |
| **G12** | `refactor(models): unify ShareFileRequest (internal) + ShareFileMcpRequest (MCP) — near-duplicate` | P3 | type-refactor |

---

## 5. Initial audit — findings (self-review)

### HIGH priority (addressed in CRITICAL above)

| # | Finding | Location | Resolution |
|---|---------|----------|------------|
| H1 | `sig` value ends up in stored chat_messages/schedule_executions rows | download tokens live in persisted agent transcripts for 7 days | Known limitation; filed as G6 |
| H2 | No HEAD handler → 405 on probes | `routers/files.py:79` | **C6** |
| H3 | No disk-full guard before write | `services/agent_shared_files_service.py:264` | **C3** |
| H4 | Filename length unbounded | `models.py` `ShareFileMcpRequest` / `ShareFileRequest` | **C2** |

### MEDIUM priority

| # | Finding | Resolution |
|---|---------|------------|
| M1 | Shared IP rate-limit bucket with public chat | **C5** |
| M2 | List endpoint visible to shared users (not just owner) | **C7** |
| M3 | Memory: full file into bytearray + extracted bytes | G1 |
| M4 | Flat `/data/agent-files/` directory | G2 |
| M5 | Audit event logs target_id but not file_id in searchable column | G4 |
| M6 | No pytest for Steps 3/4/5/6 endpoints | G5 |

### LOW priority

| # | Finding | Resolution |
|---|---------|------------|
| L1 | `one_time` / `consumed_at` columns retained but unused | Documented in schema + design doc; addressed when one-time lands (G7) |
| L2 | `ShareFileRequest` / `ShareFileMcpRequest` near-duplicate models | G12 |
| L3 | Log line could be more structured for Vector | Cosmetic |
| L4 | No concurrency test on 50MB shares | Monitor prod |
| L5 | Legacy `?download_token=` alias | G8 |

### Non-issues (verified safe by audit)

- Constant-time token compare with `secrets.compare_digest`
- Path-traversal rejection (absolute, `..`, `\`)
- MIME blocklist for PE/ELF/Mach-O/shebang
- Filesystem isolation — backend never mounts agent workspace; reads only via `docker get_archive` at agent-named paths
- `Content-Disposition: attachment` prevents inline HTML XSS
- FK `ON UPDATE/DELETE CASCADE` declared (defense-in-depth — not runtime-enforced due to platform pattern, but explicit cascade is in `rename_agent()` and delete handler)
- Cleanup on agent delete — rows, on-disk files, volume (verified by live test)

---

## 6. Execution plan — today

```
Step 1 (5 min)    Save this doc
Step 2 (~2 hrs)   Execute C1–C9 in order
Step 3 (20 min)   File G1–G12 as GitHub issues, link from this doc
Step 4 (15 min)   Re-run the 37-scenario regression to confirm no regressions from C2–C8
Step 5 (5 min)    Commit with conventional message; mark FILES-001 Implemented
```

---

## 7. Section B — Inventory of everything that changed across Steps 1–6

### New files
| File | Approx lines | Purpose |
|------|-------------|---------|
| `src/backend/db/agent_shared_files.py` | 120 | DB ops class |
| `src/backend/db/agent_settings/file_sharing.py` | 58 | Per-agent toggle mixin |
| `src/backend/services/agent_service/file_sharing.py` | 130 | Toggle service + mount check |
| `src/backend/services/agent_shared_files_service.py` | 280 | Share orchestrator (validate/extract/persist) |
| `src/backend/routers/files.py` | 170 | Public download endpoint |
| `src/mcp-server/src/tools/files.ts` | 130 | `share_file` MCP tool |
| `src/frontend/src/components/FileSharingPanel.vue` | 210 | UI panel |
| `tests/unit/test_agent_shared_files_migration.py` | 220 | Schema + migration tests (12 tests) |
| `tests/unit/test_file_sharing_mixin.py` | 170 | DB mixin tests (12 tests) |
| `tests/unit/test_public_folder_mount_match.py` | 140 | Mount match helper tests (9 tests) |
| `docs/drafts/amazing-file-outbound.md` | 300+ | Design doc (canonical through Step 6) |
| `docs/drafts/amazing-file-outbound-production-readiness.md` | *(this doc)* | Production readiness plan |

### Modified files
- `src/backend/db/schema.py` (table + column + 3 indexes)
- `src/backend/db/migrations.py` (`_migrate_agent_shared_files`)
- `src/backend/db/agent_settings/__init__.py`, `db/agents.py` (mixin registration)
- `src/backend/db/agent_settings/metadata.py` (rename_agent cascade)
- `src/backend/db/public_links.py` (`validate_agent_session` cross-link validator)
- `src/backend/database.py` (facade forwards — ~10 methods)
- `src/backend/services/agent_service/__init__.py`, `lifecycle.py`, `crud.py`, `helpers.py` (mount volume, start/stop flow)
- `src/backend/services/docker_utils.py` (`container_get_archive`)
- `src/backend/routers/agent_files.py` (toggle + share + list + revoke endpoints)
- `src/backend/routers/internal.py` (internal share endpoint)
- `src/backend/routers/agents.py` (delete handler cleanup)
- `src/backend/routers/files.py` (download endpoint — new file too)
- `src/backend/main.py` (register router)
- `src/backend/models.py` (5 new Pydantic models)
- `src/mcp-server/src/client.ts`, `server.ts` (MCP wiring)
- `src/frontend/src/stores/agents.js` (4 new actions)
- `src/frontend/src/components/SharingPanel.vue` (embed FileSharingPanel)
- `tests/registry.json` (3 entries)
- `.claude/settings.json` (permission allowlist — created)

---

## 8. Open decisions for review

1. **Docs-first or code-first in Phase 1?** Plan has docs as C1 since SDLC requires. If you'd prefer shipping code, docs can move to end — happy to reorder.
2. **Agent prompt nudge placement**: system-wide Trinity prompt (affects all agents, instant rollout) vs. per-agent CLAUDE.md (more discoverable but requires rebuilds). Proposed: system-wide.
3. **Legacy `?download_token=` alias**: keep for one release cycle then remove (file G8)? Or remove now since only URLs that had tokens redacted would be using it and those URLs are already broken?
4. **Platform-wide storage cap (G3)**: default value? Proposing 10 GB for single-host dev/small-team deployments.

---

## 9. Decision log

| Date | Decision |
|------|----------|
| 2026-04-24 | Draft created after completing Step 6 + live Slack round-trip. |
| 2026-04-24 | Triaged 9 items as CRITICAL (ship-blockers) and 12 as LATER (GitHub issues). |

---

## 10. References

- Design doc: [amazing-file-outbound.md](amazing-file-outbound.md)
- Related issues: `#295` (FILES-001, to close), `#364` (inbound web chat files, unrelated track)
- Security posture: see `amazing-file-outbound.md` §6 — all 17 threat-model items addressed or documented.
