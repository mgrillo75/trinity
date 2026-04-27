# Feature: Capacity Management (CapacityManager)

## Overview

Single public facade for per-agent execution capacity. Replaces the
three-class pyramid `ExecutionQueue` + `SlotService` + `BacklogService`
with one entry point: `services/capacity_manager.py`. Issue #428 (PR #527,
Tier 2.5 of `docs/planning/ORCHESTRATION_RELIABILITY_2026-04.md`).

## Why one facade

Three primitives had grown three different "is this agent free?" stories
that had to stay in sync at every caller site. Routers were directly
composing `ExecutionQueue.acquire` + `SlotService.acquire_slot` +
`BacklogService.enqueue` and reasoning about which combination of return
values meant "admitted vs queued vs reject vs 429." `CapacityManager`
collapses that decision into a single `acquire(...)` call gated by an
`overflow_policy` argument. `SlotService` and `BacklogService` survive as
private internals (each has a distinct, well-tested job); `ExecutionQueue`
is deleted — its N=1 count gate is now `SlotService`, and its in-memory
FIFO is a Redis LIST owned inline by `CapacityManager`.

## Public API

All callers reach for `get_capacity_manager()` from
`services/capacity_manager.py`. Full signatures live in that file (~480
LOC); summary table:

| Method | Purpose |
|--------|---------|
| `acquire(agent, exec_id, max_concurrent, *, overflow_policy, overflow_payload?, ...)` | Try to admit; on overflow, dispatch to chosen policy. Returns `AcquireResult{state, execution_id, queue_position?}`. Raises `CapacityFull` when at capacity AND overflow is unavailable/full. |
| `release(agent, exec_id)` | Release a slot. In-memory queue is popped (bookkeeping); persistent backlog drains via internal slot-release callback. |
| `release_if_matches(agent, exec_id)` | Watchdog-safe release: only releases if `exec_id` actually holds a slot. Returns `bool`. |
| `get_status(agent, max_concurrent)` | `QueueStatus` for the `/api/agents/{name}/queue` endpoint (current + in-memory queue only; persistent backlog is exposed via executions endpoints). |
| `get_all_states(agent_capacities)` | Bulk capacity meter for the dashboard. |
| `get_slot_state(agent, max_concurrent)` | Per-slot detail for the agent_config router. |
| `reclaim_stale(agent_timeouts?)` | Reclaim slots whose dynamic TTL has expired. Used by the cleanup watchdog. |
| `force_release(agent)` | Emergency: clear ALL slots + the in-memory queue. Returns `ForceReleaseResult`. |
| `clear_in_memory_queue(agent)` | Clear only the overflow queue (running executions untouched). |
| `cancel_all_overflow(agent, reason)` | Cancel queued (in-memory + persistent) — used on agent deletion. Returns count of persistent cancellations. |
| `run_maintenance(max_age_hours=24)` | Periodic: expire stale persistent rows + drain orphaned backlog. Called from `main.py` 60s loop. |

`get_capacity_manager()` returns a process-wide singleton.
`reset_capacity_manager()` exists for tests.

## Overflow policies

Three modes selected per call via the `overflow_policy` keyword:

| Policy | Behavior at capacity | When to use |
|--------|----------------------|-------------|
| `reject` | Raises `CapacityFull(reason="rejected")`. | Internal callers that have already pre-acquired upstream — e.g., `TaskExecutionService` (the router admitted the slot; the service is just being defensive). |
| `queue_in_memory` | LPUSH onto Redis LIST `agent:queue:{name}` bounded by `IN_MEMORY_DEPTH=3`. Returns `state="queued_in_memory"` with a 1-based `queue_position`. The caller still proceeds — the agent's Claude subprocess is the real serialization point; the queue exists for observability + crude rate limiting. Raises `CapacityFull(reason="in_memory_full")` at depth 3. | `/chat` (synchronous HTTP, short request, caller is blocked anyway). |
| `queue_persistent` | Marks the pre-created `schedule_executions` row `status='queued'` with `queued_at` + `backlog_metadata`. Returns `state="queued_persistent"`. Caller should reply 202 Accepted; the drain reconstructs the request later. Raises `CapacityFull(reason="persistent_full")` if the backlog is at its configured depth. Requires `overflow_payload: PersistentTaskPayload`. | `/task` (async + sync long-poll variants). Restart-durable. |

## End-to-end flow

### `/chat` — short synchronous, in-memory queue

`src/backend/routers/chat.py` (chat endpoint):

1. Resolve `max_parallel_tasks` from agent ownership row.
2. `await capacity.acquire(agent_name=..., execution_id=..., max_concurrent=N, overflow_policy="queue_in_memory", source=USER, message=...)`.
3. On `state="admitted"` or `"queued_in_memory"`, proceed to call the agent container. (The in-memory queue position is informational; the agent serializes Claude subprocess execution itself.)
4. On `CapacityFull(reason="in_memory_full")` → 429 to client.
5. In `finally`: `await capacity.release(agent_name, execution_id)` — releases the slot AND pops the next in-memory bookkeeping entry.

### `/task` async — at-capacity → backlog → drain on release

`src/backend/routers/chat.py` (parallel task endpoint, async mode):

1. Create `schedule_executions` row eagerly via `db.create_task_execution` so the caller has an `execution_id` to return.
2. Build `PersistentTaskPayload(request, effective_timeout, user_id, ...)`.
3. `await capacity.acquire(..., overflow_policy="queue_persistent", overflow_payload=payload)`.
4. On `state="admitted"`: spawn the background task as usual.
5. On `state="queued_persistent"`: return 202 with the existing `execution_id` — no work happens yet.
6. On `CapacityFull(reason="persistent_full")` → 429 (backlog is also at depth).

When ANY slot for that agent is released later (any caller, any policy), `CapacityManager._on_slot_released` fires (registered with `SlotService.register_on_release` in the constructor), which calls `BacklogService.drain_next(agent_name)`. The drain atomically claims one queued row and re-spawns the persisted request via `_run_async_task_with_persistence`. This is the path that survives a backend restart — the rows are durable; the orphan-drain in `run_maintenance()` resumes them on the next boot.

The sync `/task` long-poll variant uses the same `queue_persistent` path and waits on `services/sync_waiter.py` for the eventual drain to flip the row to terminal state (#498).

### Termination

`src/backend/routers/chat.py` terminate endpoint calls
`capacity.force_release(agent_name)` to clear all slots + the in-memory
queue at once.

## Storage map

Keys/columns are intentionally unchanged from the predecessor classes so
in-flight executions across the upgrade keep working.

| Concern | Storage | Key / column |
|---------|---------|--------------|
| Active slot counter (per agent) | Redis ZSET | `agent:slots:{name}` (member = exec_id, score = unix ts) |
| Per-slot metadata | Redis HASH | `agent:slot:{name}:{exec_id}` (auto-expires via dynamic TTL = `agent.timeout + 5min` buffer) |
| In-memory overflow queue | Redis LIST | `agent:queue:{name}` (LPUSH new, RPOP oldest, depth ≤ `IN_MEMORY_DEPTH=3`) |
| Persistent overflow backlog | SQLite | `schedule_executions` rows where `status='queued'` (driven by `queued_at` ASC for FIFO; `backlog_metadata` JSON holds the request to replay; partial index `idx_executions_queued`) |

## Maintenance & recovery

Two periodic loops keep capacity state honest:

- **`main.py` 60s loop → `capacity.run_maintenance(max_age_hours=24)`** —
  expires `status='queued'` rows older than 24h to FAILED, then calls
  `_backlog.drain_orphans_all()` to resume any backlog rows that didn't
  get a release callback (typically after a backend restart between
  enqueue and drain).
- **`services/cleanup_service.py` watchdog (5min tick)** — calls
  `capacity.reclaim_stale(agent_timeouts={...})` to release slots whose
  per-agent dynamic TTL has expired; uses `release_if_matches(agent, exec_id)` (TOCTOU-safe) when reconciling individual orphaned executions so it only releases slots the targeted execution actually holds.

## What replaced what

The CapacityManager facade is the only public surface. The earlier docs
(`execution-queue.md`, `parallel-capacity.md`, `persistent-task-backlog.md`)
now describe internal implementation details rather than independent caller
APIs.

| Old concept | CapacityManager equivalent |
|-------------|----------------------------|
| `ExecutionQueue.acquire(...)` (N=1 mutex + in-memory FIFO) | `acquire(..., overflow_policy="queue_in_memory")` with `max_concurrent=1` |
| `ExecutionQueue.release(...)` | `release(...)` |
| `ExecutionQueue.get_status(...)` | `get_status(...)` |
| `ExecutionQueue.force_release(...)` | `force_release(...)` |
| `SlotService.acquire_slot(...)` | `acquire(..., overflow_policy="reject")` |
| `SlotService.release_slot(...)` | `release(...)` |
| `SlotService.cleanup_stale_slots(...)` | `reclaim_stale(...)` |
| `SlotService.get_slot_state(...)` / `get_all_slot_states(...)` | `get_slot_state(...)` / `get_all_states(...)` |
| `SlotService.force_clear_slots(...)` | `force_release(...)` (combined with in-memory clear) |
| `BacklogService.enqueue(...)` | `acquire(..., overflow_policy="queue_persistent", overflow_payload=...)` |
| `BacklogService.drain_next(...)` | Internal: fired by SlotService release callback wired in `__init__` |
| `BacklogService.expire_stale(...)` + `drain_orphans_all(...)` | `run_maintenance(...)` |
| `BacklogService.cancel_all_backlog(...)` | `cancel_all_overflow(...)` (also clears in-memory) |
| Manual wiring of `SlotService.register_on_release(BacklogService.drain_next)` in `main.py` | Done internally in `CapacityManager.__init__` |

## Caller sites

| Caller | Location | Policy |
|--------|----------|--------|
| `/chat` | `src/backend/routers/chat.py` | `queue_in_memory` |
| `/task` async | `src/backend/routers/chat.py` | `queue_persistent` |
| `/task` sync long-poll | `src/backend/routers/chat.py` (waits on `sync_waiter`) | `queue_persistent` |
| Terminate endpoint | `src/backend/routers/chat.py` | `force_release` |
| `TaskExecutionService` | `src/backend/services/task_execution_service.py` | `reject` (router pre-acquired) |
| Cleanup watchdog | `src/backend/services/cleanup_service.py` | `reclaim_stale` + `release_if_matches` |
| Maintenance tick | `src/backend/main.py` (60s loop) | `run_maintenance` |
| `/api/agents/{name}/queue` | `src/backend/routers/agents.py` | `get_status` |
| Dashboard capacity meter | agent_config router | `get_all_states`, `get_slot_state` |
| Agent deletion | `src/backend/routers/agents.py` | `cancel_all_overflow` |

## Issue references

- **#428** — Tier 2.5 facade work (this flow). PR #527, branch `feature/428-capacity-manager`.
- **CAPACITY-001** — `SlotService` (now internal). See [parallel-capacity.md](parallel-capacity.md).
- **BACKLOG-001 (#260)** — `BacklogService` (now internal). See [persistent-task-backlog.md](persistent-task-backlog.md).
- **EXEC-024** — `TaskExecutionService` consumer. See [task-execution-service.md](task-execution-service.md).
- **TIMEOUT-001** — per-agent dynamic slot TTL.
- `docs/planning/ORCHESTRATION_RELIABILITY_2026-04.md` — Tier 2.5 (Simplification) plan; this issue is the cornerstone of "one capacity surface, three policies."

## Related flows

- [parallel-capacity.md](parallel-capacity.md) — `SlotService` internals (Redis ZSET counter, dynamic TTL). Now an internal-only module reachable through `CapacityManager`.
- [persistent-task-backlog.md](persistent-task-backlog.md) — `BacklogService` internals (SQL row state machine, FIFO claim, drain). Now an internal-only module reachable through `CapacityManager`.
- [execution-queue.md](execution-queue.md) — historical doc for the deleted `ExecutionQueue` class. Behavior preserved via `acquire(..., max_concurrent=1, overflow_policy="queue_in_memory")` + `release(...)`.
- [task-execution-service.md](task-execution-service.md) — primary internal consumer (uses `reject` policy).
- [parallel-headless-execution.md](parallel-headless-execution.md) — `/task` endpoint flow; the persistent path is the queue-overflow story.
- [cleanup-service.md](cleanup-service.md) — uses `reclaim_stale` + `release_if_matches` for stale-slot recovery.
- [execution-termination.md](execution-termination.md) — uses `force_release`.
