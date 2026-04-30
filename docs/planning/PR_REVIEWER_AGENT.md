# PR Reviewer Agent — Planning Doc

**Status**: Draft — pending review
**Date**: 2026-04-22
**Goal**: Autonomous agent that watches a configurable set of GitHub repos, runs `/review` on every new PR, and posts the resulting review as a PR comment. Safety goal: the agent has **near-zero** direct ability to mutate any repo; all side effects go through a narrowly-scoped deterministic Python CLI.

---

## 1. Trust Boundary

| Layer | Can do | Cannot do |
|-------|--------|-----------|
| **Deterministic CLI** (`pr-reviewer`) | Read PR list, fetch diff, post issue comment, update local state DB | Close/merge PRs, push code, create branches, approve/request-changes, edit files, run arbitrary `gh` |
| **Agent (Claude)** | Call `pr-reviewer` subcommands on an allow-list, read fetched diffs, invoke the `/review` skill, write review markdown to a sandbox path | Talk to GitHub directly, run raw `gh`/`git`, use a PAT, see credential values |

Concretely: the **GitHub PAT never lands in the agent's environment**. It lives in the CLI process's env, invoked as an out-of-process subprocess by the agent, and the CLI enforces the allow-list at its entry point. If the agent is ever jailbroken to try `gh pr merge` or `git push`, it has no token and no tool to do so.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ Trinity Agent Container                                               │
│                                                                        │
│   ┌─────────────────────────┐                                         │
│   │ pr-reviewer daemon      │  ◄── sleep(interval) loop, no Claude   │
│   │ - scan every N minutes  │                                         │
│   │ - if empty: sleep again │                                         │
│   │ - if work: POST /api/chat to localhost Claude                     │
│   └──────────┬──────────────┘                                         │
│              │ (only when PRs found)                                   │
│              ▼                                                         │
│   ┌────────────────────────┐   subprocess   ┌──────────────────────┐ │
│   │ Claude Code (agent)    │ ───────────►   │ pr-reviewer CLI      │ │
│   │ - /review skill        │                │ (same binary)        │ │
│   │ - no PAT in env        │ ◄───stdout─── │ - GITHUB_TOKEN in env │ │
│   └────────────────────────┘                └──────────┬───────────┘ │
│                                                        │              │
│                                  ┌─────────────────────┼──────────┐   │
│                                  │                     ▼          │   │
│                                  │  SQLite state     GitHub API   │   │
│                                  │  (reviewed_prs)   (fine-grained│   │
│                                  │                    PAT, RW on  │   │
│                                  │                    PRs only)   │   │
│                                  └────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

### Trigger model — deterministic, zero-token-on-empty

Trinity's cron fires a chat turn, which costs Claude tokens on every poll even when nothing to do. To avoid that, polling lives **outside** Claude: a sibling daemon in the same container runs the scan. Claude is only woken when there is real work.

**Daemon loop** (in-container, no Claude invocations):
1. `pr-reviewer daemon --interval 900` starts at container boot, alongside `agent-server.py`.
2. Every 15 min: `pr-reviewer scan` → list of new PRs since last run.
3. **If empty** → sleep, no token cost, nothing happens.
4. **If non-empty** → `POST http://localhost:8000/api/chat` with a single batch message: `"Review the following PRs: abilityai/trinity#371, abilityai/abilities#42"`.

**Claude loop** (only fires when daemon hands work off):
1. Agent receives the batch prompt via local chat.
2. For each PR: `pr-reviewer fetch <repo>#<num>` → CLI writes `diff.md` + `meta.json` under `~/work/<repo>/<num>/`.
3. Agent reads those files, invokes `/review`, writes `review.md`.
4. Agent calls `pr-reviewer post <repo>#<num> --file review.md` → CLI posts the comment and marks `state.db`.
5. Agent reports per-PR status and exits the chat turn.

**Why not Trinity scheduler directly?** Scheduler messages are chat messages — every fire is a Claude invocation. For a 15-min poll across quiet repos, that's ~96 empty turns/day × ~500 tokens = ~48k wasted tokens/day. The daemon sidesteps this by keeping polling deterministic and Python-only.

**Fallback option** if we cannot add a daemon to the base image: use the Trinity scheduler with a minimal prompt (`"run pr-reviewer scan; if empty, reply DONE"`) — still costs tokens per poll but kept small. Not recommended unless image changes are blocked.

---

## 3. The Deterministic CLI — `pr-reviewer`

Single Python module, shipped with the agent. Uses `PyGithub` (or raw `httpx` against GitHub REST). Subcommands are the **only** way side effects happen.

| Subcommand | Purpose | Writes? |
|------------|---------|---------|
| `scan` | List PRs needing review across configured repos. Returns JSON `[{repo, number, head_sha, title, author, url}, ...]`. | Reads only |
| `fetch <repo>#<number>` | Download diff + metadata into `work/<repo>/<number>/{diff.md, meta.json}`. | Local filesystem only |
| `post <repo>#<number> --file <path>` | Post the file contents as a single PR issue comment. Prepends a bot-identity header. Refuses if PR already has a bot comment for current `head_sha`. | GitHub issue comment + `state.db` |
| `status` | Show recently reviewed PRs (from `state.db`). | Reads only |
| `config validate` | Lint the YAML config. | Reads only |
| `daemon --interval <sec>` | Long-running loop: `scan` → if work, wake Claude via localhost `/api/chat`, else sleep. Never calls GitHub writes itself — only triggers the Claude loop. Started at container boot. | Local chat HTTP only |

**Hard-coded restrictions inside the CLI** (not configurable, so the agent cannot loosen them):
- Only `POST /repos/{owner}/{repo}/issues/{number}/comments` is reachable for writes.
- No `PATCH`, no `MERGE`, no reviews API (`/pulls/{n}/reviews`), no branch/ref writes.
- Comment body capped (e.g. 60 KB) — hard rejection over that.
- Repo allow-list from config; any `repo` arg outside the list → error.
- Rate limit: max **N comments per hour per repo** (configurable, default 6) — CLI sleeps/fails rather than exceeding.
- Dry-run mode (`--dry-run` or `DRY_RUN=1`) that logs but never calls GitHub — default on for first deploy.

---

## 4. Config Shape

`~/work/config.yaml` in the agent workspace:

```yaml
repos:
  - owner: abilityai
    repo: trinity
    filters:
      draft: false
      labels_any: []           # empty = all
      labels_none: [skip-bot-review]
      authors_none: [dependabot[bot]]
  - owner: abilityai
    repo: abilities

policy:
  review_on: new_pr            # new_pr | new_pr_and_push
  max_comments_per_hour: 6
  comment_header: "🤖 **Trinity PR Reviewer**"
  dry_run: true                # flip to false after a week of dry-run output
```

---

## 5. State

Local SQLite at `~/.pr-reviewer/state.db`, owned by the CLI:

```sql
CREATE TABLE reviewed_prs (
    repo          TEXT NOT NULL,
    pr_number     INTEGER NOT NULL,
    head_sha      TEXT NOT NULL,
    reviewed_at   TEXT NOT NULL,
    comment_url   TEXT,
    comment_body_sha TEXT,      -- for idempotency / detect drift
    PRIMARY KEY (repo, pr_number, head_sha)
);
CREATE TABLE rate_limit (
    repo       TEXT NOT NULL,
    posted_at  TEXT NOT NULL
);
```

This means **re-review** behavior is deterministic: a PR with a new head SHA reopens for review iff `policy.review_on == new_pr_and_push`, otherwise stays silent.

---

## 6. PAT Scoping

One fine-grained PAT issued by the bot's GitHub account:
- **Repositories**: explicit allow-list (same as config)
- **Permissions**: `pull_requests: read & write`, `contents: read`, `metadata: read` — **nothing else** (no workflows, no actions, no admin)
- Stored via Trinity's CRED-002 `.env` injection as `GITHUB_TOKEN`
- `.env` file readable only by the CLI process? In practice the agent container can `cat .env`, so this is belt-and-braces: the CLI's allow-list is the real gate, the narrow PAT is defense-in-depth.

---

## 7. Trinity Platform Integration

| Concern | How |
|---------|-----|
| **Deployment** | Custom agent template with `pr-reviewer` CLI pre-installed; create via `POST /api/agents` or Trinity UI. |
| **Credentials** | `GITHUB_TOKEN` (fine-grained PAT) injected via `POST /api/agents/{name}/credentials/inject`. No other secrets needed. |
| **Scheduling** | **No Trinity cron schedule.** `pr-reviewer daemon --interval 900` runs in-container next to `agent-server.py`, backgrounded from `~/.trinity/setup.sh` (which Trinity's `startup.sh` already invokes on every boot — no base-image change needed). The daemon does the polling; Claude is only invoked when the daemon POSTs work to localhost `/api/chat`. Zero Claude tokens burned on empty polls. |
| **Read-only mode** | Turn on `PUT /api/agents/{name}/read-only` — blocks accidental edits to source files; `work/` stays writable because read-only only gates source paths. |
| **Autonomy** | Enabled; no human in loop for routine reviews. |
| **Audit trail** | Each CLI invocation logs to stdout → Vector → `agents.json`. Every GitHub write the CLI makes is recorded with PR id + comment URL. Optionally emit Trinity events via MCP `emit_event` for a dashboard. |
| **Kill switch** | Flip `dry_run: true` in config OR disable the schedule via `PUT /api/agents/{name}/autonomy`. Either stops posts immediately. |

---

## 8. Agent System Prompt (sketch)

```
You are the PR Reviewer agent. You review GitHub pull requests using the
/review skill and post the result as a comment.

You have exactly ONE tool for GitHub interaction: the `pr-reviewer` CLI.
You MUST NOT use `gh`, `git push`, `curl`, or any other network tool
against GitHub. You do not have a GitHub token and these calls will fail.

Loop per invocation:
  1. Run `pr-reviewer scan`. Parse JSON.
  2. For each entry: `pr-reviewer fetch <repo>#<num>`, then read the
     diff, run /review, write review.md, then
     `pr-reviewer post <repo>#<num> --file review.md`.
  3. If the CLI rejects a post (rate limit, duplicate, size), skip and
     continue. Do not retry aggressively.
  4. Report per-PR status at the end.

Never modify files outside `~/work/`. Never open shells against the
GitHub API directly.
```

---

## 9. V1 Scope

In scope:
- Single top-level PR comment with the review markdown.
- Polling-based discovery via `scan` (no webhooks yet).
- SQLite state, dry-run default, rate limit, repo allow-list.
- One bot identity (one PAT), multi-repo.

Out of scope (V2+):
- **Per-line code comments** via `/pulls/{n}/reviews` — needs another CLI subcommand with its own allow-list. Adds complexity; defer until V1 is stable.
- **Webhook-driven** (GitHub App or Cloudflare webhook → Trinity) for sub-minute latency. Polling is fine for V1.
- **Learning from reactions** (👎 on reviews → tune prompt).
- **PR approve/request-changes** — explicitly never, to preserve the "comment only" trust boundary.

---

## 10. Security Review Checklist (pre-deploy)

- [ ] PAT is fine-grained, scoped to configured repos only, `pull_requests:write` only
- [ ] CLI allow-list tested: attempting `pr-reviewer post` with a repo not in config fails closed
- [ ] CLI size-cap tested: 100 KB body rejected
- [ ] CLI rate-limit tested: 7th comment in an hour blocked
- [ ] Duplicate-comment guard tested: same head_sha twice → CLI refuses
- [ ] Dry-run default verified: fresh deploy posts nothing to GitHub
- [ ] Agent read-only mode enabled
- [ ] Audit log confirms all write attempts (successful and refused)

---

## 11. Open Questions

1. ~~Daemon launch mechanism~~ — **resolved**. No base-image change needed. `docker/base-image/startup.sh` already sources `/home/developer/.trinity/setup.sh` on every boot (used for restoring user packages). The template for this agent ships a `.trinity/setup.sh` that backgrounds the daemon:

    ```bash
    nohup python3 /home/developer/bin/pr-reviewer daemon --interval 900 \
        > /home/developer/logs/daemon.log 2>&1 &
    ```

    Same `&`-backgrounding pattern Trinity already uses for `agent-server.py`. Daemon restarts with the container. Crash recovery: a `while true; do ...; done` wrapper or systemd-style restart policy in the daemon itself.
2. **Which review skill exactly** — the existing `/review` in `.claude/skills/review` (pre-landing PR review) applies to Trinity's own branch. For external repos we won't have a `main` to diff against in the agent container. Likely need a variant that works off the PR diff payload directly. Is this a new skill or a flag on the existing one?
3. **One PAT across repos or per-repo PATs?** One is simpler; per-repo is tighter blast radius if one repo's scope changes.
4. **Re-review on new pushes?** Default to single-review-per-PR (cheaper, less noise). Override per-repo in config if desired.
5. **Review latency target?** Polling every 15 min is cheap. Sub-5-min needs webhooks → adds Cloudflare Tunnel + HTTP endpoint to the agent. Defer?
6. **Handling draft PRs** — default skip, configurable?
7. **Bot identity** — dedicated GitHub user (recommended, clean audit trail) or run as a human account?

---

## 12. Delivery Plan

| Step | Artifact | Est. effort |
|------|----------|-------------|
| 1 | `pr-reviewer` CLI (Python, PyGithub, subcommands + allow-list + state DB + tests) | 1 day |
| 2 | Agent template (CLAUDE.md + system prompt + schedule + config) | 0.5 day |
| 3 | Fine-grained PAT issuance, credential injection | 0.5 day |
| 4 | Dry-run soak on abilityai/trinity + abilityai/abilities (observe, do not post) | 2–3 days |
| 5 | Flip `dry_run: false`, monitor first 20 reviews for quality + rate-limit behavior | ongoing |
| 6 | Follow-up issue for V2 (per-line comments, webhooks) | later |

---

## 13. Failure Modes & Responses

| Failure | Detection | Response |
|---------|-----------|----------|
| PAT revoked / expired | CLI 401 from GitHub | Agent reports; ops rotates PAT via credential injection endpoint |
| Agent posts spam / low-quality reviews | Human reviewer 👎 / issue report | Flip `dry_run: true` via config update + agent chat; disable schedule |
| Rate limit hit | CLI refuses | Already the intended behavior — safe |
| CLI bug writes wrong repo | Should be impossible (allow-list) — unit-tested | CLI tests gate the release |
| Agent tries direct `gh`/`git push` | No token → fails; logged to Vector | Audit log review, tune system prompt |

---

## 14. Related

- Trinity credential injection: `docs/memory/architecture.md` §Credentials (CRED-002)
- Scheduling: `docs/memory/architecture.md` §Background Services
- Existing review skill: `.claude/skills/review/` (needs variant for external-repo diffs — see Open Q #1)
- GitHub fine-grained PATs: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens
