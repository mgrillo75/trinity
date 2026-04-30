# SUB-003: Automatic Subscription Switching on Rate Limit

> **Requirement ID**: SUB-003
> **Extends**: SUB-002 (Subscription Management)
> **Priority**: HIGH
> **Status**: ✅ Implemented (2026-03-21), updated 2026-04-25 (#441 — threshold 2 → 1, broadened to auth failures, default flipped to on)

---

## Problem

When an agent hits a Claude subscription usage limit ("out of extra usage"), all scheduled and interactive executions fail with HTTP 429 until the subscription resets (often hours/days away). The user must manually notice the error, go to Settings, and reassign a different subscription. This is disruptive for autonomous agents that run on schedules.

The same disruption happens on auth-class failures (401/403, expired OAuth token, low credit balance) — those signal the subscription itself is broken and need the same auto-recovery.

## Requirements

### Preconditions (ALL must be true for auto-switch to trigger)

1. **Subscription failure observed**: The agent has just received either a rate-limit (429) **or** an auth-class failure (401/403/credit balance/expired token) on its current subscription. (Pre-#441 this required 2 consecutive 429s — that gate is removed; the 2h skip-list on alternative selection is the only thrash guard now.)
2. **Multiple subscriptions available**: There are **≥2 subscription credentials** registered in the system
3. **Setting enabled**: A system-level setting **"Allow automatic subscription switching"** is checked. Default **ON** (opt-out) per #441 — operators who explicitly disable it keep their choice.

### Behavior

When all three preconditions are met:

1. **Select next subscription**: Pick an available subscription that is NOT the currently-assigned one. Selection strategy:
   - Prefer subscriptions with **fewer assigned agents** (load-balance)
   - Skip subscriptions that have **themselves been rate-limited recently** (within last 2 hours) to avoid cascading failures
   - If all alternatives are also rate-limited, do NOT switch — keep current and report the situation

2. **Switch subscription**: Call the existing `assign_subscription` flow (DB update + container restart with new `CLAUDE_CODE_OAUTH_TOKEN`)

3. **Log the switch**: Create a structured log entry and agent activity event:
   ```
   [SUB-003] Auto-switching agent "{agent_name}" from "{old_sub}" to "{new_sub}"
   after {a rate-limit error | an authentication failure}
   ```

4. **Notify**: Send a notification (via existing notification system) to the agent owner so they're aware of the automatic switch

5. **Retry the failed execution**: After the switch, automatically retry the execution that triggered the switch (once only — no retry loops)

### Rate-Limit Tracking

- Track per-subscription rate-limit timestamps in the database (new table or column)
- Fields: `subscription_id`, `agent_name`, `error_message`, `occurred_at`
- A subscription is considered "rate-limited" if it has a rate-limit event within the last 2 hours
- Clean up tracking records older than 24 hours

### Settings UI

- Checkbox in **Settings → Subscriptions** section: **"Automatically switch subscriptions when usage limits or auth failures are reached"**
- Helper text: _"When enabled, agents automatically try a different subscription on the first rate-limit (429) or auth failure (401/403/expired token/low credit). Requires at least 2 registered subscriptions."_
- Store as system setting: `auto_switch_subscriptions` (boolean, default `true` per #441 — opt-out, not opt-in)

### Dashboard Visibility

- When an auto-switch occurs, show it in the agent's activity stream
- The agent header subscription badge should update to reflect the new subscription (already happens via existing WebSocket refresh)

---

## Technical Design Notes

### Where detection happens

Rate-limit errors are currently detected in **two places**:

1. **Agent container** (`docker/base-image/agent_server/services/claude_code.py`): `_is_rate_limit_message()` detects the error during execution and returns HTTP 429
2. **Backend** receives the 429 from agent container (or via schedule execution results)

The auto-switch logic should live in the **backend** since it needs access to the subscription registry and can coordinate across agents.

### Suggested flow

```
Agent container detects rate limit → returns 429 to caller
    ↓
Backend receives 429 (via schedule executor or chat proxy)
    ↓
Backend increments consecutive rate-limit counter for (agent, subscription)
    ↓
If counter ≥ 2 AND auto_switch enabled AND alternatives exist:
    ↓
Backend selects best alternative subscription
    ↓
Backend calls assign_subscription (existing flow — DB + container restart)
    ↓
Backend logs event, sends notification, retries execution
```

### Files likely to change

| Layer | File | Change |
|-------|------|--------|
| DB | `src/backend/db/subscriptions.py` | Add rate-limit tracking table/queries |
| DB | `src/backend/db/migrations.py` | New migration for tracking table + system setting |
| Service | `src/backend/services/subscription_service.py` | Auto-switch orchestration logic |
| Router | `src/backend/routers/subscriptions.py` | Setting endpoint, rate-limit tracking |
| Schedule | `src/backend/services/agent_service/schedule.py` | Hook into schedule execution failure path |
| Chat | Agent chat proxy (wherever 429 is received) | Hook into chat failure path |
| Frontend | `src/frontend/src/views/Settings.vue` | Add checkbox to Subscriptions section |
| MCP | `src/mcp-server/src/tools/subscriptions.ts` | Optional: expose setting via MCP |

### Edge cases

- **All subscriptions exhausted**: Log warning, do not switch, surface error to user as today
- **Agent has ANTHROPIC_API_KEY (not subscription)**: Auto-switch does not apply — only for subscription-based agents
- **Concurrent switches**: Use DB-level locking to prevent two agents from switching to the same subscription simultaneously
- **Rapid flip-flopping** (#441): the 2h skip-list on alternative selection (`is_subscription_rate_limited` + `select_best_alternative_subscription`) is the only thrash guard. When an agent switches A→B and B also fails, A stays flagged for 2h post-switch (by the still-recorded events from before the switch — see #444), so no ping-pong back to A.

---

## Acceptance Criteria

- [x] System setting `auto_switch_subscriptions` exists and defaults to ON (#441 — flipped to opt-out)
- [x] Settings UI shows checkbox with helper text in Subscriptions section
- [x] Subscription failure events are tracked per (agent, subscription) with timestamps
- [x] **A single rate-limit (429) failure** triggers auto-switch to a different subscription (#441 — threshold 2 → 1)
- [x] **A single auth-class failure** (401/403/credit balance/expired token/etc.) also triggers auto-switch (#441 — broadened scope)
- [x] Rate-limited subscriptions (last 2 hours) are skipped during selection — no regression on the ping-pong guard (#444)
- [x] Auto-switch is logged as an activity event on the agent
- [x] Notification is sent to agent owner on auto-switch (text adapts per failure kind)
- [x] No switch occurs if setting is disabled, only 1 subscription exists, or all alternatives are recently rate-limited
- [x] Subscription badge in agent header updates after auto-switch

## History

- 2026-03-21 — initial implementation (issue #153, threshold = 2, 429-only, default OFF)
- 2026-04-25 — #441: threshold dropped to 1, broadened to auth-class failures, default flipped to ON. `handle_rate_limit_error` kept as a backward-compat shim around the new `handle_subscription_failure(failure_kind=…)`.
