# Channel Adapter Message Routing (SLACK-002)

> **Status**: Complete (updated 2026-03-31 — router generalized for multi-channel)
> **Created**: 2026-03-23
> **Extends**: SLACK-001
> **See also**: [telegram-integration.md](telegram-integration.md) — TGRAM-001 (second adapter implementation)

## Overview

Pluggable channel adapter abstraction for external messaging platforms. Any incoming message follows the same pipeline: Transport → Adapter → Router → Agent → Response. Single Slack App supports multiple agents, each with a dedicated channel. Telegram bots follow the same pipeline with `TelegramAdapter`.

## User Story

As an **agent owner**, I want to connect multiple agents to Slack so each agent gets its own channel and users can chat with the right agent.

As a **Slack user**, I want to @mention the bot in an agent's channel and get a response from that specific agent — and continue the conversation in the thread without needing to @mention again.

As a **platform admin**, I want Slack messages to go through the same execution pipeline as web public chat (audit trail, rate limits, tool restrictions) so public Slack users can't access sensitive agent resources.

## Entry Points

- **Settings UI**: `src/frontend/src/views/Settings.vue` — Slack Integration section (transport + workspace install)
- **Agent UI**: `src/frontend/src/components/SlackChannelPanel.vue` — per-agent channel binding in Sharing tab
- **Legacy UI**: `src/frontend/src/components/PublicLinksPanel.vue:540` — "Connect Slack" button (per public link)
- **API**: `POST /api/settings/slack/connect` — start Socket Mode transport
- **API**: `POST /api/settings/slack/install` — platform-level OAuth (workspace install)
- **API**: `POST /api/agents/{name}/slack/channel` — create channel + bind agent
- **Transport**: `src/backend/adapters/transports/slack_socket.py` — Socket Mode receives events

## Frontend Layer

### Settings Page (Platform-Level)
- `Settings.vue` — Unified Slack section with:
  - OAuth credentials (Client ID, Client Secret, Signing Secret) + Save
  - Transport connection: Socket Mode status badge, App Token input, Connect button
  - "Install to Workspace" button → triggers platform OAuth → redirects to Slack → callback stores bot token → redirects to Settings with success notification
  - Connected workspaces list with bound agent badges

### Agent Detail — Sharing Tab (Per-Agent)
- `SlackChannelPanel.vue` — Three states:
  - **Bound**: Shows `#channel-name`, workspace name, DM-default badge **or** "Make default" button (with hover tooltip explaining DM routing), and "Unbind" button. The Unbind button is **disabled** when this agent is the DM default *and* the workspace has other bound agents — promoting another agent first via the "Make default" button on its panel is required (#584).
  - **Unbound**: "Create Slack Channel" button → creates channel in Slack + binds to agent
  - **Access denied**: Informational message for non-owner shared users
- `SharingPanel.vue` — Renders `SlackChannelPanel` between Team Sharing and Public Links sections

### Legacy: PublicLinksPanel (Per Public Link)
- `PublicLinksPanel.vue:540-556` — `connectSlack()` method handles two response types:
  - `status: "connected"` — workspace already linked, channel created → show success notification
  - `status: "oauth_required"` — redirect to Slack OAuth URL

### API Calls (Settings)
- `GET /api/settings/slack/status` → `{connected, transport_mode, app_token_configured, workspaces}`
- `POST /api/settings/slack/connect` → `{connected, transport_mode}`
- `POST /api/settings/slack/disconnect` → `{disconnected, was_connected}`
- `POST /api/settings/slack/install` → `{oauth_url}` (browser redirect)

### API Calls (Per-Agent Channel)
- `GET /api/agents/{name}/slack/channel` → `{bound, channel_name, channel_id, workspace_name, is_dm_default, workspace_agent_count}`
- `POST /api/agents/{name}/slack/channel` → `{status, channel_name, channel_id, workspace_name}`
- `DELETE /api/agents/{name}/slack/channel` → `{unbound, workspace_name}` — **409** if the agent is the workspace's DM default and other agents are still bound (#584)
- `PUT /api/agents/{name}/slack/channel/dm-default` → `{status, team_id, workspace_name, previous, new_default}` — owner-only; single-tx clear-then-set on `is_dm_default`; audit-logged via `AGENT_LIFECYCLE/slack_dm_default_changed` (#584)

## Backend Layer

### Message Flow

```
External Platform (Slack)
       │
       ▼
Transport Layer (Socket Mode / Webhook)
       │  Receives raw event, acknowledges immediately
       ▼
ChannelAdapter.parse_message(raw_event)
       │  Returns NormalizedMessage or None (skip)
       ▼
ChannelMessageRouter.handle_message(adapter, message)
       │
       ├─ 1. adapter.get_agent_name(message)       → resolve agent
       ├─ 2. adapter.get_bot_token(message)        → credentials
       ├─ 3. _check_rate_limit(adapter.get_rate_key) → sliding window
       ├─ 4. get_agent_container(agent_name)       → verify running
       ├─ 5. adapter.handle_verification(message)  → sender auth
       ├─ 6. db.get_or_create_public_chat_session  → session
       ├─ 7. db.build_public_chat_context           → history
       ├─ 8. adapter.indicate_processing(message)   → ⏳
       ├─ 9. TaskExecutionService.execute_task       → agent call
       ├─ 10. adapter.indicate_done(message)         → ✅
       ├─ 11. db.add_public_chat_message (x2)        → persist
       ├─ 12. adapter.send_response(channel, resp)   → deliver
       └─ 13. adapter.on_response_sent(msg, agent)   → track thread
```

### Agent Resolution (Slack)

Priority in `SlackAdapter.get_agent_name()`:

1. **Channel binding** — `slack_channel_agents` lookup by `(team_id, channel_id)`
2. **Active thread** — `slack_active_threads` lookup (reply-without-mention)
3. **DM default** — `slack_channel_agents` where `is_dm_default = 1`
4. **Single agent** — Only one connected agent → use it
5. **Legacy fallback** — `slack_link_connections` from SLACK-001

### Endpoints

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| POST | `/api/public/slack/events` | `routers/slack.py` | Webhook event receiver (fallback mode) |
| GET | `/api/public/slack/oauth/callback` | `routers/slack.py` | OAuth completion redirect |
| POST | `/api/agents/{name}/public-links/{id}/slack/connect` | `routers/slack.py` | Connect agent to Slack (OAuth or channel bind) |
| GET | `/api/agents/{name}/public-links/{id}/slack` | `routers/slack.py` | Connection status |
| DELETE | `/api/agents/{name}/public-links/{id}/slack` | `routers/slack.py` | Disconnect |
| PUT | `/api/agents/{name}/public-links/{id}/slack` | `routers/slack.py` | Update settings (enable/disable) |
| PUT | `/api/agents/{name}/slack/channel/dm-default` | `routers/slack.py` | Make this agent the DM-default for its workspace (#584) |
| DELETE | `/api/agents/{name}/slack/channel` | `routers/slack.py` | Unbind — refuses with 409 if agent is the DM default and others are bound (#584) |

### Business Logic

1. **"Connect Slack" flow**: If workspace not connected → OAuth. If connected → `conversations.create` creates `#agent-name` channel → bind in `slack_channel_agents`
2. **Message routing**: Socket Mode/webhook delivers event → adapter parses → router dispatches to agent via `TaskExecutionService` → adapter formats response via `format_response()` (markdown → Slack mrkdwn) → sends with `chat:write.customize` (agent name/avatar)
3. **Response formatting**: `SlackAdapter.format_response()` converts standard markdown to Slack mrkdwn via `slackify-markdown` library (`**bold**` → `*bold*`, `[link](url)` → `<url|link>`, headers, lists). Graceful fallback to plain text on failure.
4. **Thread continuity**: First @mention → bot responds in thread → registers in `slack_active_threads` → subsequent replies in that thread don't need @mention

### Database Operations

| Table | Operation | Description |
|-------|-----------|-------------|
| `slack_workspaces` | create/get/delete | Workspace connections with encrypted bot tokens |
| `slack_channel_agents` | bind/unbind/get | Channel-to-agent routing |
| `slack_active_threads` | register/check | Thread tracking for reply-without-mention |
| `public_chat_sessions` | get_or_create | Session persistence (reused from web public chat) |
| `public_chat_messages` | add | Message history persistence |

## Side Effects

- **Slack channel created**: `conversations.create` called when agent connects (visible in workspace)
- **Reaction emoji**: ⏳ added on message receive, replaced with ✅ on completion
- **Execution record**: `schedule_executions` row with `triggered_by="slack"`
- **Activity tracking**: Agent activity recorded via `TaskExecutionService` (visible in Dashboard timeline)
- **Slot management**: Execution slot acquired/released via `SlotService`
- **Active thread**: Registered in `slack_active_threads` after first response

## Error Handling

| Error Case | Response | Notes |
|------------|----------|-------|
| No agent for channel | Silent (no response) | Message in unbound channel |
| Agent not running | "Sorry, I'm not available right now." | Container status != running |
| Rate limited | "You're sending messages too quickly." | 30 msg/min per user |
| Agent at capacity | "I'm busy right now." | All parallel slots in use |
| Billing/credit error | "I'm having trouble processing." | API key or subscription issue |
| Execution error | "Sorry, I encountered an error." | Generic fallback |
| No bot token | Silent (logged as error) | Workspace connection issue |
| Verification pending | Verification prompts sent | Email verification flow |

## Security Considerations

### Authentication
- Slack users are **public users** — no Trinity account needed
- Optional email verification (auto-verify via Slack profile or 6-digit code)
- Socket Mode: token-based auth (handled by `slack_sdk`)
- Webhook Mode: HMAC-SHA256 signature verification

### Tool Restrictions
- Public Slack users restricted to `--allowedTools WebSearch,WebFetch` (configurable)
- No Bash, Read, Write, Edit, NotebookEdit for public users
- **Known limitation**: MCP tools (Trinity API) not restricted by `--allowedTools` — see `docs/research/slack-security-findings.md`

### Data Protection
- Bot tokens encrypted at rest (AES-256-GCM via `credential_encryption.py`)
- Rate limiting: 30 msg/min per Slack user (configurable)
- Execution timeout: 120s (configurable)
- Session identifiers: `{team_id}:{user_id}:{channel_id}` — no PII

### Audit Trail
- All executions recorded with `triggered_by=adapter.channel_type` and `source_user_email=adapter.get_source_identifier(message)`
- Slack: `triggered_by="slack"`, `source="slack:{team}:{user}"`
- Telegram: `triggered_by="telegram"`, `source="telegram:{bot_id}:{user_id}"`
- Visible in Tasks tab and Dashboard timeline

## Testing

### Prerequisites
- [ ] Backend running with Socket Mode connected
- [ ] Slack App configured with required scopes and event subscriptions
- [ ] At least one agent running with subscription assigned

### Test Steps

#### 1. Connect Agent to Slack
**Action**: Agent Detail → Sharing → "Connect Slack"
**Expected**: Channel `#agent-name` created in Slack workspace, binding in DB
**Verify**: `slack_channel_agents` table has entry

#### 2. @Mention in Agent Channel
**Action**: Send `@mcp-bot hello` in `#agent-name`
**Expected**: ⏳ reaction → agent processes → ✅ reaction → response in thread
**Verify**: Logs show `[ROUTER] DONE: agent-name`, execution record with `triggered_by=slack`

#### 3. Thread Reply Without @Mention
**Action**: Reply in the thread from test 2 without @mention
**Expected**: Bot responds in same thread
**Verify**: `slack_active_threads` table has entry for this thread

#### 4. Multi-Agent Routing
**Action**: Connect second agent → send @mention in each channel
**Expected**: Each channel routes to its own agent
**Verify**: Logs show different agent names for different channels

#### 5. Tool Restriction
**Action**: Ask `@mcp-bot show me contents of /home/developer/.env`
**Expected**: Agent cannot read files (Read tool not in allowed list)
**Verify**: Agent logs show `Restricting tools to: WebSearch,WebFetch`

#### 6. Rate Limiting
**Action**: Send 31 messages in under 60 seconds
**Expected**: 31st message gets "sending too quickly" response

#### 7. Settings — Connect Socket Mode
**Action**: Settings → Slack Integration → enter App Token → click Connect
**Expected**: Status badge turns green "Socket Mode", backend logs show `Slack Socket Mode transport connected`
**Verify**:
- [ ] `GET /api/settings/slack/status` returns `{connected: true, transport_mode: "socket"}`
- [ ] Backend log: `Slack Socket Mode transport connected`

#### 8. Settings — Disconnect and Reconnect
**Action**: Click Disconnect (via API) → verify status → click Connect again
**Expected**: Status goes red → green, Socket Mode re-established
**Verify**:
- [ ] Status shows `connected: false` after disconnect
- [ ] Status shows `connected: true` after reconnect
- [ ] No stale socket sessions in logs

#### 9. Settings — Install to Workspace (OAuth)
**Action**: Settings → Slack Integration → click "Install to Workspace"
**Expected**: Browser redirects to Slack OAuth → authorize → redirects back to Settings with "Workspace installed" notification
**Verify**:
- [ ] `slack_workspaces` table has new entry with encrypted `bot_token`
- [ ] `GET /api/settings/slack/status` shows workspace in `workspaces` list
- [ ] Redirect URL uses `PUBLIC_CHAT_URL` (must be configured for callback to work)

#### 10. Agent Sharing — Create Slack Channel
**Action**: Agent Detail → Sharing tab → click "Create Slack Channel"
**Expected**: Channel `#agent-name` created in Slack workspace, panel shows bound state with channel name
**Verify**:
- [ ] `GET /api/agents/{name}/slack/channel` returns `{bound: true, channel_name: "agent-name"}`
- [ ] Channel visible in Slack workspace
- [ ] `slack_channel_agents` table has binding entry

#### 11. Agent Sharing — Already Bound
**Action**: Click "Create Slack Channel" on an agent that already has a binding
**Expected**: Success message "Already bound to #agent-name in workspace-name"
**Verify**:
- [ ] No duplicate channel created
- [ ] `POST /api/agents/{name}/slack/channel` returns `{status: "already_bound"}`

#### 12. Agent Sharing — Unbind Channel
**Action**: Click "Unbind" on a bound agent
**Expected**: Panel switches to unbound state showing "Create Slack Channel" button
**Verify**:
- [ ] `GET /api/agents/{name}/slack/channel` returns `{bound: false}`
- [ ] `slack_channel_agents` table entry removed
- [ ] Slack channel still exists (not deleted) but agent no longer responds to @mentions in it

#### 13. Agent Sharing — No Workspace Connected
**Action**: Click "Create Slack Channel" when no workspace is installed
**Expected**: Error message "No Slack workspace connected. Install a workspace from Settings first."
**Verify**:
- [ ] `POST /api/agents/{name}/slack/channel` returns 400

### Edge Cases
- [ ] Bot invited to #general (non-agent channel) → only responds to @mentions
- [ ] Thread started by another user (not via @mention) → bot ignores replies
- [ ] Agent stopped while message in flight → "not available" response
- [ ] Two users in same channel → separate sessions (per-user isolation)
- [ ] Non-owner views Sharing tab → SlackChannelPanel shows "Only the agent owner can manage Slack channel bindings"
- [ ] `CREDENTIAL_ENCRYPTION_KEY` mismatch → bot token decryption fails → Slack API calls fail (logged, no crash)

**Last Tested**: 2026-03-26
**Tested By**: claude + human (manual + 15 integration tests)
**Status**: ✅ Core flow + transport management + per-agent channel binding working
**Issues**: MCP tools bypass `--allowedTools` restriction (documented in security findings)

## Related Flows

- [slack-integration.md](slack-integration.md) — SLACK-001: Original Slack DM integration
- [telegram-integration.md](telegram-integration.md) — TGRAM-001: Telegram Bot Integration (second adapter)
- [task-execution-service.md](task-execution-service.md) — EXEC-024: Unified execution lifecycle
- [public-agent-links.md](public-agent-links.md) — Public chat session persistence

## Configurable Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `channel_rate_limit_max` | 30 | Messages per rate limit window |
| `channel_rate_limit_window` | 60 | Window duration in seconds |
| `channel_timeout_seconds` | 120 | Max execution time per message |
| `channel_allowed_tools` | WebSearch,WebFetch | Comma-separated allowed tools for public users |
| `slack_transport_mode` | socket | Transport: `socket` or `webhook` |
| `slack_app_token` | (none) | App-Level Token for Socket Mode |
| `public_chat_url` | (none) | Public URL for webhook mode |
