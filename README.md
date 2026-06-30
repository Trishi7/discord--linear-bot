# Discord → Linear Triage Bot

A Discord bot that triages messages from a small allowlist of reporters in
monitored channels, classifies them with an LLM, and reconciles each one with
Linear: create a new issue, comment on an existing one (optionally with a
status transition), or do nothing. Behind `REQUIRE_APPROVAL` the proposed
action is gated on a ✅/❌ react in a private channel.

Also exposes a **read-only QUERY MODE** when @-mentioned, for asking Linear
questions ("status of NFT-123", "list my open bugs", "what's open with the Bug
label") directly from Discord.

## Why a human-in-the-loop step?

Without it, an LLM running over a busy Discord will create a steady stream of
junk tickets — banter, off-topic chatter, vague complaints, duplicates. A
single React-with-✅ step in a private channel cuts noise to near zero with
minimal operational cost. Toggle `REQUIRE_APPROVAL=false` for auto-create once
you're confident in the classifier's precision in your specific channels.

## Architecture

```
            ┌─────────────────── REPORT PATH ──────────────────────────┐
Discord ──▶ Pre-filter ──▶ LLM classifier ──▶ Plan ──▶ Approval embed ──▶ ✅ Linear
 channels   (channel,      (Claude, strict        │      (or auto)
            allowlist,      JSON)                 │
            length)                               │
                                                  └── SQLite (dedup, audit,
                                                      message→issue mapping)

            ┌─────────────────── QUERY MODE (read-only) ───────────────┐
@mention ──▶ parse_query (LLM) ──▶ list_issues / get_issue / search ──▶ reply
```

## Project layout

```
discord-linear-bot/
├── README.md
├── requirements.txt
├── .env.example
├── main.py             # entry point
├── config.py           # env loading + allowlist parsing
├── db.py               # SQLite state store
├── classifier.py       # report classifier + query parser (both pure JSON)
├── linear_client.py    # Linear GraphQL client (labels, states, members, issues, comments)
└── bot.py              # Discord bot: on_message, on_raw_reaction_add, query mode
```

## Categories & labels

The classifier returns **strict JSON** with the following category vocabulary:

| Category      | Meaning                                                        | Linear label applied |
|---------------|----------------------------------------------------------------|----------------------|
| `bug`         | Something is broken or not behaving as expected.               | `Bug`                |
| `feature`     | Build or add something new that does not yet exist.            | `Feature`            |
| `improvement` | A tweak to existing behaviour (perf, UX, polish, refactor).    | `Improvement`        |
| `noise`       | Chit-chat, acks, questions, plain status — nothing to do.      | _(no ticket)_        |

The classifier also returns:

- **`needs_triage: bool`** — set when the message is plausibly actionable but
  the category is uncertain. Bypasses the `MIN_CONFIDENCE` floor so a
  needs-triage item is never silently dropped. When set, the bot **creates the
  issue unassigned** with a `⚠️ Needs triage` note in the description.
- **`is_new_issue: bool`** — `false` for replies/follow-ups that refer to the
  same thing as their parent. Drives create-vs-update (below).
- **`status_signal`** — `none | resolved | in_progress | cannot_reproduce`.
  Drives status transitions on existing issues.
- **`area_labels`** — subset of `["BE", "FE", "UI"]`. Applied alongside the
  category label.
- **`mentioned_assignees`** — Discord display names in mention order;
  `mentioned_assignees[0]` becomes the proposed Linear assignee.

### Fixed label allowlist

The bot will **only ever apply** these six labels to Linear issues:

```
BE   FE   UI   Bug   Feature   Improvement
```

These labels must already exist on the Linear team — **the bot never
auto-creates labels**. Allowed names that aren't on the team are logged and
skipped. Names outside the allowlist are silently dropped before reaching
Linear.

## Reporter allowlist

Only messages from configured reporters are eligible for the report path.
Anyone in a monitored channel can use **query mode** by @-mentioning the bot.

| Env var                    | Format                              | Meaning                                                          |
|----------------------------|-------------------------------------|------------------------------------------------------------------|
| `ALLOWED_REPORTER_IDS`     | Comma-separated Discord user IDs    | Primary allowlist (numeric snowflakes).                          |
| `ALLOWED_REPORTER_NAMES`   | Comma-separated display names       | Case-insensitive fallback. Default: `Sid,Harsh,Trishi`.          |

A reporter passes the gate if their Discord ID is in `ALLOWED_REPORTER_IDS`
**OR** their display name (lower-cased) is in `ALLOWED_REPORTER_NAMES`.
If both lists end up empty, startup logs a clear warning ("the bot will act on
NOBODY") and continues — useful for staging environments.

Messages outside monitored channels and from non-allowlisted authors are
dropped at debug level only, to keep the terminal log readable.

## `DISCORD_LINEAR_MAP`

JSON object mapping Discord user ID (string) → Linear user email **or** Linear
user UUID. Used for assignee resolution.

```env
DISCORD_LINEAR_MAP={"123456789":"sid@nfthing.com","987654321":"harsh@nfthing.com"}
```

Resolution order for `mentioned_assignees[0]`:

1. If the mentioned person's Discord ID is in the map → resolve their Linear
   email/id against the team's members → use that user id.
2. Otherwise, match the display name (case-insensitive) against the team's
   member `displayName` / `name`.
3. If neither matches → unassigned, with `_Intended assignee: @<name>_` noted
   in the issue description.

Invalid JSON in `DISCORD_LINEAR_MAP` logs a warning and falls back to `{}` —
it never crashes startup.

## Approval gate (`REQUIRE_APPROVAL`)

Boolean. Accepts `true / false / 1 / 0 / yes / no` (case-insensitive). Default
`true`.

| Value | Behaviour |
|-------|-----------|
| `true`  | Proposed action is posted as an embed to `APPROVAL_CHANNEL_ID`. The embed states the action explicitly (e.g. **"Create (needs triage, unassigned)"**, **"Comment + mark NFT-123 resolved"**). ✅ executes it, ❌ discards. |
| `false` | The action runs immediately. A short confirmation (`✅ Created [NFT-123](url) — title`, `💬 Commented on [NFT-123](url) and marked resolved`, etc.) is posted to the approval channel. |

## Create vs. update (dedup)

**One Discord message never creates two tickets.** The bot decides between
creating a new issue and commenting on an existing one using two signals:

1. **Thread linkage** — if the message replies to / is in the same thread as
   an already-processed message that has a stored `linear_issue_id`:
   - `is_new_issue == false` → **comment** on that issue (author, timestamp,
     message text, attachment links). If `status_signal != "none"` → also
     transition the issue (see below).
   - `is_new_issue == true` → classifier says this is a *separate* item;
     continue to the create path.

2. **Search-by-title** — before creating, the bot runs `search_issues(title)`
   and only treats a hit as a duplicate when:
   - the title matches the proposed title exactly (case- and whitespace-normalised), **AND**
   - the matched issue's state is in an OPEN type (`backlog` / `unstarted` /
     `started` / `triage`).
   On a clear match → comment instead of creating. Anything fuzzier → create.

Dedup is enforced two ways:

- **Per Discord message**: `db.already_processed(message.id)` blocks any
  re-classification of the same message id (e.g. on bot restart, late events).
- **Per Linear issue**: every processed message stores its
  `linear_issue_id` in SQLite, so future replies/follow-ups in the same thread
  re-bind to the same issue rather than spawning a duplicate.

## Status-by-TYPE transitions

Linear workflow state **names** are workspace-customisable; the bot drives
transitions off the stable **type** (`backlog` / `unstarted` / `started` /
`completed` / `canceled` / `triage`). It picks the first state of the target
type the team exposes:

| `status_signal`      | Target state TYPE | Example name |
|----------------------|-------------------|--------------|
| `resolved`           | `completed`       | "Done"       |
| `in_progress`        | `started`         | "In Progress" |
| `cannot_reproduce`   | `canceled`        | "Cancelled"  |

If the team has no state of the target type → log + no-op (never raises).
Transitions only apply when commenting on the parent thread issue — `comment`
on a dup-by-search is intentionally **not** auto-transitioned (we matched by
title only and shouldn't assume the issue's progress changed).

## Query mode (read-only)

@-mention the bot in a monitored channel **or** the approval channel with a
question:

```
@TriageBot status of NFT-123
@TriageBot list the issues I raised in the last 2 weeks and their statuses
@TriageBot what's open with the Bug label
@TriageBot any open BE bugs
@TriageBot what did Sid report this week
```

The question is parsed by a **separate** LLM call (`classifier.parse_query`,
strict JSON) into a filter spec — reporter / time window / labels / state /
free text — then fetched via `linear_client.get_issue` / `list_issues` /
`search_issues` and replied to in the same channel.

**Hard guarantees:**

- Query mode is **read-only**. It never calls `create_issue`, `add_comment`,
  or `set_issue_status`.
- Query mode runs **before** the report pipeline, so a question can never
  become a ticket.
- Reply-pings don't trigger queries — only explicit `<@bot_id>` mentions do.
- Anyone in the channel can query (no reporter allowlist for queries).

If the @-mention is in a monitored channel but the parser decides the message
isn't a Linear question (`is_query=false`), the report path is given a
chance instead. In the approval channel that fall-through replies politely
with a help nudge instead of dropping silently.

## Architecture separation

Three modules with clean roles:

- **`classifier.py`** — pure JSON producer. Two prompts (report classifier +
  query parser), both `Anthropic`-backed. **Never** touches Linear.
- **`linear_client.py`** — all Linear reads/writes over GraphQL. No Linear MCP.
  Helpers: `resolve_label_ids`, `resolve_assignee`, `create_issue`,
  `add_comment`, `set_issue_status`, `list_team_states`, `search_issues`,
  `get_issue`, `list_issues`.
- **`bot.py`** — orchestrates Discord events, decides plans, posts embeds,
  handles ✅/❌, runs query mode, persists state via `db.py`.

## Setup

### 1. Create the Discord bot

1. https://discord.com/developers/applications → New Application.
2. Bot → Add Bot. Copy the token.
3. **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT**.
4. OAuth2 → URL Generator → scopes `bot`. Permissions: View Channels,
   Send Messages, Add Reactions, Read Message History. Invite to your server.

### 2. Get your IDs

Enable Developer Mode in Discord (Settings → Advanced), right-click each
channel → Copy Channel ID:

- One or more **monitored** channel IDs (where reporters post).
- One **approval** channel ID (private staff-only).

### 3. Linear credentials

1. Linear → Settings → API → Personal API keys → Create. Copy the key.
2. Find the NFThing2.0 team UUID (Linear GraphQL `teams { nodes { id key name } }`).
3. Make sure the six allowed labels exist on the team:
   `BE`, `FE`, `UI`, `Bug`, `Feature`, `Improvement`. The bot will not create them.

### 4. Install and run

```bash
python -m venv venv
source venv/bin/activate            # or: .\venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
cp .env.example .env                # edit values
python main.py
```

## Tuning knobs (env vars)

See `.env.example` for the full list with comments. Key knobs:

| Variable                  | Default                | Purpose                                                                 |
|---------------------------|------------------------|-------------------------------------------------------------------------|
| `MIN_MESSAGE_LENGTH`      | 20                     | Skip text-only messages shorter than this. Attachments are exempt.      |
| `MIN_CONFIDENCE`          | 0.6                    | Drop classifier verdicts below this — bypassed when `needs_triage=true`.|
| `CLASSIFY_DELAY_SECONDS`  | 0                      | Wait this long before classifying so follow-ups can land in context.    |
| `CLASSIFIER_MODEL`        | `claude-sonnet-4-6`    | Swap to `claude-haiku-4-5-20251001` for ~10× cost reduction.            |
| `REQUIRE_APPROVAL`        | `true`                 | Toggle the ✅ step.                                                      |
| `ALLOWED_REPORTER_IDS`    | _(empty)_              | Numeric Discord user IDs allowed to report.                             |
| `ALLOWED_REPORTER_NAMES`  | `Sid,Harsh,Trishi`     | Case-insensitive display-name fallback.                                 |
| `DISCORD_LINEAR_MAP`      | `{}`                   | Discord user id → Linear email/UUID, JSON object.                       |

## Operational notes

- The SQLite file (`bot_state.db` by default) is the only stateful piece. Back
  it up if you care about audit history or the message→issue mapping.
- Pending approvals survive bot restarts — the reaction handler reads from
  SQLite, not memory.
- Descriptions and comment bodies are baked at plan time. Edits to the source
  Discord message between proposal and ✅ are **not** picked up.
- Auto-execute failures (`REQUIRE_APPROVAL=false`) post a `⚠️ Auto-execute
  failed` notice to the approval channel and mark the row rejected, so the
  message isn't retried on its own — investigate and re-trigger manually.

## What's deliberately not in scope

- **Slash commands** — would need `discord.app_commands` registration. Query
  mode covers most query needs via @-mention.
- **Embedding-based duplicate detection** — current dedup is title-equality on
  open issues. Worthwhile v2 with `voyage-3` or similar.
- **Per-channel category overrides** — easy to add in `bot._decide_plan`.
- **Metrics** — add Prometheus counters around classified / approved /
  rejected / commented / dup-matched.
