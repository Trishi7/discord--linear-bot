# Discord → Linear Triage Bot — Technical Documentation

Reference for developers and operators. The [README](./README.md) covers
motivation and first-time setup; this document covers internals, the message
lifecycle, configuration, the data model, failure modes, and extension points.

---

## 1. System overview

The bot is a single long-running Python process that:

1. Connects to Discord via `discord.py` (Gateway WebSocket).
2. On each message in a monitored channel, runs a cheap pre-filter, then
   classifies the thread with Claude (vision-enabled).
3. Posts an approval embed in a private channel and waits for a ✅ / ❌
   reaction.
4. On ✅, creates a Linear issue via GraphQL with a category label and
   priority; on ❌, marks it discarded.
5. Persists every step in SQLite so restarts don't lose pending approvals or
   re-classify already-seen messages.

```
┌──────────────┐   on_message    ┌────────────┐
│  Discord     │ ───────────────▶│ Pre-filter │── reject ──▶ drop
│  (monitored) │                 └────┬───────┘
└──────────────┘                      │ pass
                                      ▼
                              ┌───────────────┐
                              │ Thread        │  reply chain + history
                              │ collector     │  + image attachments
                              └────┬──────────┘
                                   ▼
                              ┌───────────────┐
                              │ Classifier    │  Claude (vision)
                              │ (classifier.py)│ → JSON verdict
                              └────┬──────────┘
                                   │ category != not_actionable
                                   │ confidence ≥ MIN_CONFIDENCE
                                   ▼
                              ┌───────────────┐    SQLite
                              │ Approval embed│ ──────────▶ status=pending
                              │ in #triage    │
                              └────┬──────────┘
                                   │ on_raw_reaction_add
                                   ▼
                       ✅  ┌───────┴────────┐  ❌
                ┌─────────┤                ├─────────┐
                ▼         │                │         ▼
        ┌─────────────┐   │                │   ┌──────────┐
        │ Linear      │   │                │   │ mark     │
        │ create_issue│   │                │   │ rejected │
        └─────┬───────┘   └────────────────┘   └──────────┘
              │
              ▼ mark approved + issue id
        ┌────────────┐
        │ Reply with │
        │ issue link │
        └────────────┘
```

---

## 2. Module reference

| File | Responsibility |
|---|---|
| `main.py` | Entry point. Configures logging, validates env, starts the bot. |
| `config.py` | Loads & exposes env-var config; `validate()` returns missing required keys. |
| `bot.py` | `TriageBot` (`discord.Client`). Owns the `on_message` and `on_raw_reaction_add` handlers, thread context collection, approval embed, and Linear creation glue. |
| `classifier.py` | `Classifier` wraps the Anthropic SDK. Runs the sync SDK call in a thread, parses strict JSON, normalises/validates fields. |
| `linear_client.py` | `LinearClient` — minimal Linear GraphQL client. Creates issues, looks up or auto-creates the category label, maps priority. |
| `db.py` | `DB` — thin SQLite wrapper. Three jobs: dedup, pending-approval lookup, audit. |
| `requirements.txt` | `discord.py`, `anthropic`, `httpx`, `python-dotenv`. |
| `bot_state.db` | SQLite file created at runtime (path configurable). |

---

## 3. Configuration

All config is environment-driven via `.env` (loaded with `python-dotenv`).
`config.validate()` runs at startup and exits with code `2` if any required
value is missing.

### Required

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Discord Developer Portal. Requires the **MESSAGE CONTENT** privileged intent. |
| `MONITORED_CHANNEL_IDS` | Comma-separated channel IDs to listen on. |
| `APPROVAL_CHANNEL_ID` | Single channel ID where approval embeds are posted. |
| `ANTHROPIC_API_KEY` | Claude API key used by the classifier. |
| `LINEAR_API_KEY` | Linear personal API key. Used as the `Authorization` header verbatim. |
| `LINEAR_TEAM_ID` | UUID of the Linear team to create issues in. (Note: the *UUID*, not the team key.) |

### Optional / tuning

| Variable | Default | Effect |
|---|---|---|
| `CLASSIFIER_MODEL` | `claude-sonnet-4-6` | Anthropic model id. `claude-haiku-4-5-20251001` is the cheap default; `claude-opus-4-7` for highest quality. |
| `MIN_MESSAGE_LENGTH` | `20` | Skip messages shorter than this, **unless** they carry an attachment (screenshots often have one-line captions). |
| `MIN_CONFIDENCE` | `0.6` | Drop verdicts below this threshold. Raise to cut false positives. |
| `CLASSIFY_DELAY_SECONDS` | `0` | If > 0, wait this many seconds before classifying so follow-up clarifications can land in the same thread. The message is re-fetched after the sleep. |
| `DB_PATH` | `./bot_state.db` | Location of the SQLite file. |

---

## 4. Message lifecycle

### 4.1 Pre-filter (`bot.on_message`)

In order, a message is dropped if any of:

1. Author is a bot (`message.author.bot`).
2. Channel id not in `MONITORED_CHANNEL_IDS`.
3. Length < `MIN_MESSAGE_LENGTH` **and** no attachments.
4. Already in the `processed` table (dedup on restart/replay).

If `CLASSIFY_DELAY_SECONDS > 0`, the bot sleeps that long, re-checks dedup,
and re-fetches the message so any edits during the wait are included.

### 4.2 Thread context (`_collect_thread_context`)

Goals: classify *the conversation*, not the message in isolation. Steps:

1. Walk the reply chain up to `CONTEXT_REPLY_DEPTH = 25` ancestors via
   `Message.reference`.
2. If the message lives in a `discord.Thread`, pull up to
   `CONTEXT_HISTORY_LIMIT = 200` thread messages (chronological).
3. Otherwise scan recent channel history (same limit) and pull in any message
   that replies to anything already in the set — i.e. descendants of the
   conversation.
4. Sort by `created_at` and return.

If an *earlier* message in the assembled thread is already in `processed`,
the new one is treated as a follow-up and dropped, so a single conversation
generates at most one approval embed.

Attachments: image attachments from any message in the thread are passed to
the classifier as Claude `image` blocks (capped at
`MAX_IMAGES_PER_CLASSIFY = 8`). Non-image attachments are listed inline by
filename + URL.

### 4.3 Classification (`classifier.Classifier.classify`)

Builds a Claude `messages.create` call:

- `system` = the prompt in `classifier.py` defining the JSON schema and
  category/priority/confidence guidance.
- `messages[0]` (user) = N image blocks followed by one text block built from
  `USER_TEMPLATE` (channel, reporter, participants, formatted conversation).

The Anthropic SDK is synchronous; the call is offloaded with
`asyncio.to_thread` so the Discord event loop is never blocked.

The response is parsed by `_extract_json`, which:

1. Strips ```` ```json ```` / ```` ``` ```` fences.
2. Falls back to grabbing the first balanced `{...}` block if the model added
   prose.
3. Normalises: `category` must be one of `bug | task | improvement |
   not_actionable`; `priority` falls back to `medium`; `confidence` is
   clamped to `[0.0, 1.0]`; `title` is trimmed to 80 chars.

On any API error, malformed JSON, or unknown category, `classify` returns
`None` and the message is silently dropped.

### 4.4 Verdict gating

After classification, the verdict is dropped if:

- `category == "not_actionable"`, or
- `confidence < MIN_CONFIDENCE`.

Otherwise the bot posts an approval embed.

### 4.5 Approval embed (`_post_for_approval`)

Built in the approval channel as a Discord `Embed`:

- Title = `{icon} {verdict.title}` (icons in `CATEGORY_ICON`).
- Color per category (`CATEGORY_COLOR`).
- `url` = jump link to the source message.
- Fields: Category, Priority, Confidence%, Source (channel + author
  mention), Original (≤500 char snippet).
- Footer instructs ✅ / ❌.

Both reactions are pre-seeded by the bot. The embed's message id is stored
in `processed.approval_message_id` for reverse lookup.

### 4.6 Approval handler (`on_raw_reaction_add`)

Uses **raw** reactions so it works even if the embed isn't in the bot's
message cache (e.g. after a restart). Filters:

- Ignore reactions by the bot itself.
- Ignore reactions outside the approval channel.
- Ignore emojis other than ✅ / ❌.

Looks up the entry via `DB.get_by_approval`. If missing or not `pending`,
returns silently (idempotent).

- **❌** → `DB.mark_rejected`, reply "❌ Discarded.", done.
- **✅** → `_create_linear_issue`.

### 4.7 Linear creation (`_create_linear_issue` + `LinearClient`)

The source Discord message is **re-fetched** at approval time so edits
between post and approval are reflected. The Linear description is built
from the verdict description plus a footer block:

```
{verdict.description}

---
**Reported by:** {display name}
**Source:** {jump link}

**Original message:**
> {first 1500 chars of current message text}

**Attachments:**
- [filename](url)
- ...
```

`LinearClient.create_issue`:

1. Maps `category` → label name (`Bug` / `Feature` / `Improvement`).
2. `_get_or_create_label`: looks up the label by name on the team, creates
   it if missing, caches the id in-memory for the process lifetime.
3. Maps `priority` → Linear's integer (`urgent=1, high=2, medium=3, low=4`;
   defaults to `3`).
4. Calls `issueCreate` and returns `{id, identifier, url, title}`.

On any exception, the bot replies in the approval channel with
`⚠️ Failed to create Linear issue: ...` and **does not** mark the entry as
approved — so the operator can re-react ✅ after fixing the upstream issue
(once the per-row status check is changed; see "Known caveats" §7). The
broad `except (LinearError, Exception)` is deliberate: a Linear blip must
never kill the bot.

On success: `DB.mark_approved(approval_message_id, issue_id)`, reply with a
hyperlinked issue identifier.

---

## 5. Data model

SQLite, single table:

```sql
CREATE TABLE processed (
    message_id          TEXT PRIMARY KEY,   -- Discord source message id
    channel_id          TEXT NOT NULL,      -- source channel id
    classification_json TEXT NOT NULL,      -- the verdict dict, JSON-encoded
    approval_message_id TEXT,               -- the embed's message id (nullable)
    linear_issue_id     TEXT,               -- set when approved
    status              TEXT NOT NULL,      -- pending | approved | rejected
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_approval ON processed(approval_message_id);
CREATE INDEX ix_status   ON processed(status);
```

State transitions:

```
(absent) ──record_pending──▶ pending ──mark_approved──▶ approved
                                  └──mark_rejected──▶ rejected
```

`DB.already_processed(message_id)` is the dedup primitive. `get_by_approval`
is the reverse lookup used by the reaction handler. All writes commit at the
end of the `conn()` context manager.

### Useful ad-hoc queries

```sql
-- Pending approvals older than 24h:
SELECT message_id, classification_json, created_at
FROM processed
WHERE status = 'pending'
  AND created_at < datetime('now', '-1 day');

-- Approval rate over the last 7 days:
SELECT status, COUNT(*) FROM processed
WHERE created_at > datetime('now', '-7 days')
GROUP BY status;

-- Category mix:
SELECT json_extract(classification_json, '$.category') AS cat,
       COUNT(*) AS n
FROM processed GROUP BY cat ORDER BY n DESC;
```

---

## 6. Operations

### Running

```bash
python3 -m venv venv
source venv/bin/activate     # or: venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env         # fill in values
python main.py
```

The process runs in the foreground. Stdout/stderr carry structured logs at
`INFO`. Use a service manager (systemd, supervisord, Docker `restart:
unless-stopped`) for production.

### Required Discord setup

- Privileged intent: **MESSAGE CONTENT** must be enabled on the bot
  application or `on_message` will never see text.
- OAuth2 scopes: `bot`. Permissions: View Channels, Read Message History,
  Send Messages, Add Reactions.
- The bot must be invited to *both* the monitored and approval channels.

### Required Linear setup

- A **personal API key** (Settings → API).
- The **team UUID** (not the team key like `ENG`). Easiest path: hit the
  Linear GraphQL endpoint with the query in `linear_client.py`'s prelude
  area, or use Linear's docs/playground to run
  `query { teams { nodes { id key name } } }` and pick the matching `id`.

The category labels (`Bug`, `Feature`, `Improvement`) are auto-created on
the team if they don't exist, so no upfront label setup is required.

### Backups

`bot_state.db` is the only stateful artifact. If you care about the audit
trail, back it up. Losing it means:

- Already-seen messages may be re-classified on next run (no harm, just
  duplicate API spend).
- Pending approvals lose their backing entry; reactions on them are
  silently ignored. Re-post the message to re-trigger.

### Logs to watch

`INFO` is enough for routine operation. Key lines:

- `Classifying msg=... reporter=... channel=... thread=N images=K` — a
  classification kicked off.
- `msg=... → not_actionable; dropping` / `confidence X < Y; dropping` —
  pre-publication drops, useful for tuning `MIN_CONFIDENCE`.
- `Approval msg=... → approved → linear=ENG-123` — happy path.
- `Failed to create Linear issue` — investigate Linear API key / team id /
  rate limits.

---

## 7. Failure modes & known caveats

- **Anthropic timeouts / rate limits.** Classifier returns `None`; the
  message is dropped with a `WARN`/`EXC` log. The message is **not** marked
  processed, so it would be re-tried on restart only if it appeared again in
  history scans. There is no in-process retry today.
- **Linear API failure on approve.** The bot replies with the error and
  leaves the entry as `pending`. Reacting ✅ again will retry. (Today the
  reaction handler short-circuits on `status != pending`, so the first
  failure correctly stays retry-able since it never moves out of `pending`.)
- **Race on approve.** Two operators reacting ✅ in quick succession both
  pass the `status == 'pending'` check; the second creates a duplicate
  Linear issue. Acceptable for current volume; mitigated by reviewer
  convention. A `BEGIN IMMEDIATE` + UPDATE-then-check pattern would fix it.
- **Editing the approval embed.** The bot never edits its own embeds after
  posting. The reply line is the only feedback on outcome.
- **Image fetching.** Claude fetches Discord CDN image URLs directly. If a
  message is deleted before classification, image blocks may fail.
- **`MAX_IMAGES_PER_CLASSIFY = 8`** caps payloads; extras are listed by URL
  in the text only.
- **Thread context scope.** `CONTEXT_REPLY_DEPTH = 25` ancestors and
  `CONTEXT_HISTORY_LIMIT = 200` messages. Long-running threads beyond that
  are truncated.

---

## 8. Extension points

The codebase is small on purpose; common extensions:

- **Per-channel category override** (e.g. `#bugs` → always `bug`). Hook in
  `bot.on_message` after `_collect_thread_context`, before `classifier`.
- **Slash command to triage retroactively.** Add `discord.app_commands`,
  call the same `classify → post_for_approval` path with a fetched message.
- **Duplicate detection against open Linear issues.** Before posting, run
  an embedding search over open issues for that team; if cosine similarity
  > threshold, attach a link in the embed instead of (or in addition to)
  creating a new issue.
- **Auto-create without approval.** Skip `_post_for_approval` once you trust
  the classifier in a given channel — but keep the dedup write.
- **Metrics.** Add Prometheus counters: `classified_total{category}`,
  `approved_total`, `rejected_total`, `linear_errors_total`. Wire to
  `_post_for_approval`, `mark_approved`, `mark_rejected`, and the Linear
  exception path.
- **Alternative ticket backends.** `LinearClient` is the only ticket-system
  coupling; a `JiraClient` / `GitHubIssueClient` with the same `create_issue`
  shape is a drop-in.

---

## 9. Cost model

Per classifier call: roughly 500 input + 200 output tokens (more with
images). At 1,000 qualifying messages/day:

| Model | Approx. daily cost |
|---|---|
| `claude-sonnet-4-6` | $5–8 |
| `claude-haiku-4-5-20251001` | < $1 |
| `claude-opus-4-7` | ~10× sonnet |

The pre-filter (length, bot author, dedup) typically eliminates 70–90 % of
raw traffic before reaching the classifier, so "qualifying" is much smaller
than total message volume in most servers.
