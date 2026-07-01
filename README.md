# Discord → Linear Triage Bot

A Discord bot that triages messages from a small allowlist of reporters in
monitored channels, classifies them with an LLM, and reconciles each one with
Linear: create a new issue, comment on an existing one (optionally with a
status transition), or do nothing. Behind `REQUIRE_APPROVAL` the proposed
action is gated on a ✅/❌ react in a private channel.

Also exposes a **read-only QUERY MODE** when @-mentioned, with three intents:

- **`issue_status`** — a status check on **one specific issue**, by key
  ("status of NFT-123") or by fuzzy description ("what's the status of the DMs
  issue", "where are we on the payout bug").
- **`person_activity`** — "what is `<person>` working on?", which merges a
  person's **assigned/active Linear issues** with their **recent Discord
  activity** from monitored channels into one synthesised reply.
- **`issue_list`** — questions about Linear issues as a **group** ("list my open
  bugs", "what's open with the Bug label", "issues Sid raised last week").

Two cross-cutting behaviours:

- **Source scoping** — a question can be scoped to **Discord only**, **Linear
  only**, or **both**. "what did Harsh post on discord" hits only Discord; "what
  is Harsh assigned in Linear" hits only Linear; an unscoped question uses both.
  A source-scoped reply never carries a section for the other source.
- **Edited messages re-trigger QUERY MODE only** — editing an @-mention question
  re-runs the query on the new text and posts a fresh reply; editing an ordinary
  message does nothing. Edits are **never** routed into the report/create path.

Query mode (and edit-handling) is **strictly read-only** — no create / comment /
status change, ever.

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
@mention  ──▶ parse_query (LLM) ──┬─ issue_status ──▶ get_issue (by key) or
 or EDIT of    → intent + source  │                   find_issues_by_text (fuzzy)
 an @mention     + subject/labels │                     ├─ 1 match  ──▶ reply
                                  │                     ├─ 2–5      ──▶ ask which
                                  │                     └─ 0        ──▶ honest miss
                                  ├─ person_activity ──▶ resolve_person
                                  │    (source: discord | linear | both)
                                  │    ├─ Linear:  active_issues_for_user
                                  │    └─ Discord: scan_recent_messages
                                  │          └─▶ LLM synthesis ──▶ reply
                                  └─ issue_list ──▶ list/get/search ──▶ reply
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
├── classifier.py       # report classifier + query parser + activity synthesiser
├── linear_client.py    # Linear GraphQL client (labels, states, members, issues, comments)
├── query.py            # read-only person resolution + Discord activity scan
└── bot.py              # Discord bot: on_message, on_raw_message_edit, on_raw_reaction_add, query mode
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
user UUID. It is the **cross-link between a person's Discord and Linear
identities**, used in two places:

```env
DISCORD_LINEAR_MAP={"123456789":"sid@nfthing.com","987654321":"harsh@nfthing.com"}
```

**1. Report-path assignee resolution** for `mentioned_assignees[0]`:

1. If the mentioned person's Discord ID is in the map → resolve their Linear
   email/id against the team's members → use that user id.
2. Otherwise, match the display name (case-insensitive) against the team's
   member `displayName` / `name`.
3. If neither matches → unassigned, with `_Intended assignee: @<name>_` noted
   in the issue description.

**2. Query-mode `person_activity` cross-linking** (`query.resolve_person`):
once a free-text name is matched to a **Linear** user, if that user's email/UUID
appears as a **value** in the map, the corresponding Discord ID (the key) is used
as the **exact** Discord identity to scan. Without a map entry, the Discord side
falls back to display-name matching against recent monitored-channel posters and
guild members — so **mapping quality directly determines how reliably the two
sides line up** for someone whose Discord and Linear names differ.

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
question. A **separate** LLM call (`classifier.parse_query`, strict JSON)
classifies it into one of three intents (`issue_status`, `person_activity`,
`issue_list`) and also emits a **`source`** (`discord` | `linear` | `both`) plus
an optional **category** (`labels`, e.g. `Bug`).

### Source scoping (`discord` / `linear` / `both`)

The parser infers *which system* a question is about, so a source-scoped reply
only ever touches — and only ever shows — that source:

| Phrasing cues                                             | `source`  | Effect |
|----------------------------------------------------------|-----------|--------|
| "on discord", "in the channel", "posted", "mentioned/said" | `discord` | Discord scan only; **no** Linear section |
| "in linear", "assigned to", "ticket", "issue", "status of" | `linear`  | Linear lookup only; **no** Discord section |
| no explicit source                                        | `both`    | combined (previous behaviour) |

```
@TriageBot what bugs did Harsh mention on discord today   → discord-only, Bug-filtered
@TriageBot what is Harsh assigned in linear               → linear-only
@TriageBot what is Harsh working on                       → both
```

A **Discord-scoped `issue_list`** ("what bugs did Harsh mention on discord") is
*not* a Linear query — it's redirected to the Discord activity path so Linear is
never queried for it. The reply header always reflects the source(s) actually
used.

### `issue_status` — one specific issue, by key or description

```
@TriageBot status of NFT-123
@TriageBot what's the status of the DMs issue
@TriageBot where are we on the payout bug
@TriageBot tell me the description of NFT-610
@TriageBot tell me more about NFT-610
```

**Detail level.** The query parser sets `detail` per phrasing: `"description"`
("description of NFT-123", "what does NFT-123 say") **leads with the full issue
description**; `"more"` ("tell me more about / details of NFT-123") shows the
description **under** the summary; `"none"` (a plain status check) keeps the
one-line summary + latest comment. The description is pulled from Linear (now
SELECTed in `get_issue`), truncated to ~1500 chars — and further if needed to fit
Discord's message ceiling — with a "…(full text in Linear)" pointer + URL so it's
never silently dropped. An empty description is reported as "(no description set)".

Flow (`bot._handle_issue_status`):

- **Explicit key** (e.g. `NFT-123`) → `get_issue` directly; reply with title,
  status, assignee, link, and latest comment. Missing key → honest "couldn't
  find **NFT-123**".
- **Free-text subject** → `linear_client.find_issues_by_text(subject)`, which
  returns **all** open matches (open issues first, ranked by relevance then
  recency):
  - **exactly one match** (and no "list them all" phrasing) → full status answer
    (re-fetched for the latest comment),
  - **several matches, or phrasing that asks for all of them** ("all issues
    containing DMs", "list the DM tickets") → the bot **lists every match**
    (identifier + current status + title), capped at 10 with a "showing N of M"
    note; it no longer forces a disambiguation question,
  - **no match** → says plainly that no *open* issue matched "`<subject>`", names
    what was searched, and offers to widen to closed issues.

> **Note — matching spans more than titles.** `find_issues_by_text` uses Linear's
> `searchIssues` full-text index (title + description + comments) and is
> acronym/synonym-aware, so **both "DMs" and "direct messages" surface the same
> direct-message issues** without the bot maintaining its own alias table. Quality
> still tracks how issues are worded, but is not limited to the exact title.

### `issue_list` — questions about Linear issues

```
@TriageBot status of NFT-123
@TriageBot list the issues I raised in the last 2 weeks and their statuses
@TriageBot what's open with the Bug label
@TriageBot any open BE bugs
```

Parsed into a filter spec — reporter / time window (`window_days`) / labels /
states / free text — then fetched via `linear_client.get_issue` / `list_issues`
/ `search_issues` and replied to in the same channel.

### `person_activity` — "what is `<person>` working on?"

```
@TriageBot what is Sid working on?
@TriageBot what's Harsh been up to this week?
@TriageBot what am I working on?
```

Flow (`bot._handle_person_activity`):

1. **`query.resolve_person(name)`** maps the free-text name to **both** a Linear
   user (matched against `list_team_members` by displayName / name / email, with
   first-name / partial fallback) and a Discord user (via `DISCORD_LINEAR_MAP`
   when the Linear user is mapped, else display-name match against recent posters
   / guild members).
   - **Ambiguous** (several plausible matches) → the bot replies asking which
     person and does nothing else — it never guesses.
   - **No match** in either system → it says so plainly.
2. **Linear side** — `active_issues_for_user(linear_user.id, now − window)`:
   issues assigned to them that are in a `started`-type state **or** were updated
   within the window.
3. **Discord side** — `scan_recent_messages(...)` walks `channel.history` across
   monitored channels only, matching by Discord ID (preferred) or display name.
4. **Synthesis** — the in-scope result sets are handed to
   `classifier.summarize_person_activity`, which writes one concise reply. Under
   `source="both"` that's a **Working on (Linear)** list (identifier, title,
   status, link) *and* a 1–3 sentence **Recent Discord activity** summary with
   jump links. Under a scoped `source` **only that one section is produced** —
   the out-of-scope data is never even sent to the model, so a Discord-only
   answer can't leak a Linear block (and vice versa). A `category` (e.g. `Bug`)
   narrows both sides — Linear issues are label-filtered, the Discord summary is
   biased to matching messages. The model **summarises** Discord — it must not
   dump raw logs or invent issues/links; an empty source is "nothing in
   `<source>`". If the synthesis call fails, a source-aware deterministic
   fallback render is used instead.

The window defaults to `QUERY_DISCORD_LOOKBACK_DAYS` when the question gives no
time frame.

### Environment knobs (person_activity)

| Variable                        | Default | Purpose                                                      |
|---------------------------------|---------|--------------------------------------------------------------|
| `QUERY_DISCORD_LOOKBACK_DAYS`   | `14`    | How far back to scan Discord for a person-activity query.     |
| `QUERY_MAX_MESSAGES_PER_CHANNEL`| `400`   | Hard cap on messages scanned per monitored channel per query. |

### Limitations

- **Monitored channels only** — DMs, threads in other channels, and any channel
  not in `MONITORED_CHANNEL_IDS` are invisible to the Discord scan.
- **Lookback-bounded** — only the last `QUERY_DISCORD_LOOKBACK_DAYS` days are
  scanned, capped at `QUERY_MAX_MESSAGES_PER_CHANNEL` messages per channel, so a
  very chatty channel can be truncated. Replies note the coverage
  ("last N days, monitored channels only").
- **Name-matching quality depends on `DISCORD_LINEAR_MAP`** — without a map entry
  linking a person's Discord ID to their Linear identity, the Discord side relies
  on display-name matching, which is weaker when the two names diverge.
- Channels the bot can't read are skipped (logged), not errored.

### Edited messages (QUERY MODE only)

Editing a message re-runs **only** query mode — never the report path
(`bot.on_raw_message_edit`, raw so edits to uncached/older messages still fire):

- Same pre-filters as `on_message`: ignores bots, non-allowlisted authors, and
  channels outside `MONITORED_CHANNEL_IDS`; skips no-op edits (embed/pin
  resolves) when the text is provably unchanged.
- If the **edited** text reads as an @-mention question → the same
  `_handle_query` path runs on the **new** text and posts a **fresh reply** (a
  new message — the old answer is not edited). So editing "what is Sam working
  on" → "what is Harsh working on" makes the bot answer for Harsh.
- If the edited message is **not** a query (an ordinary report or other message)
  → **nothing happens**. An edited bug report never creates a second ticket and
  never touches the create/comment/status pipeline.

### Hard guarantees (all intents)

- Query mode — including edit re-triggers — is **read-only**. It never calls
  `create_issue`, `add_comment`, or `set_issue_status`.
- Query mode runs **before** the report pipeline, so a question can never
  become a ticket; edits are never routed into the report pipeline at all.
- A source-scoped answer never shows a section for the other source.
- `issue_status` never replies with a bare null — every branch (hit, ambiguous,
  miss, bad input) produces a useful message.
- Reply-pings don't trigger queries — only explicit `<@bot_id>` mentions do.
- Anyone in the channel can query (no reporter allowlist for queries).

If the @-mention is in a monitored channel but the parser decides the message
isn't a question it can answer (`is_query=false`), the report path is given a
chance instead. In the approval channel that fall-through replies politely
with a help nudge instead of dropping silently.

## Architecture separation

Clean module roles:

- **`classifier.py`** — `Anthropic`-backed text/JSON producer. Three prompts:
  report classifier, query parser, and the `person_activity` synthesiser
  (`summarize_person_activity`). **Never** touches Linear or Discord.
- **`linear_client.py`** — all Linear reads/writes over GraphQL. No Linear MCP.
  Helpers: `resolve_label_ids`, `resolve_assignee`, `create_issue`,
  `add_comment`, `set_issue_status`, `list_team_states`, `search_issues`,
  `find_issues_by_text` (fuzzy title/keyword lookup for `issue_status`),
  `get_issue`, `list_issues`, plus the query-mode reads `list_team_members`,
  `active_issues_for_user` (optionally label-filtered), `recent_issues_created_by`.
- **`query.py`** — read-only building blocks for `person_activity`:
  `scan_recent_messages` (Discord history scan over monitored channels) and
  `resolve_person` (free-text name → Linear user + Discord user). Takes the
  Linear client and Discord client as parameters; never mutates either.
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
| `QUERY_DISCORD_LOOKBACK_DAYS` | 14                 | Days back to scan Discord for a `person_activity` query.                |
| `QUERY_MAX_MESSAGES_PER_CHANNEL` | 400             | Hard cap on messages scanned per monitored channel per query.           |

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
- **Custom synonym/alias map** — `issue_status` fuzzy search already leans on
  Linear's full-text index (title + description + comments), which is
  acronym/synonym-aware (DMs ↔ "direct messages" resolves today). A bot-side alias
  map for domain jargon Linear's index *doesn't* connect would be a further step,
  but isn't needed for the common cases.
- **Per-channel category overrides** — easy to add in `bot._decide_plan`.
- **Metrics** — add Prometheus counters around classified / approved /
  rejected / commented / dup-matched.
