# Discord → Linear Triage Bot

A Discord bot that triages messages from a small allowlist of reporters in
monitored channels, classifies them with an LLM, and reconciles each one with
Linear: create a new issue, comment on an existing one (optionally with a
status transition), or do nothing. Behind `REQUIRE_APPROVAL` the proposed
action is gated on a ✅/❌ react in a private channel.

Also exposes a **read-only QUERY MODE** when @-mentioned. Linear questions are
answered by a **tool-driven loop** (`query_engine.py`): the bot's own Claude call
is handed a set of read-only Linear tools and decides which to call, so it answers
open-ended questions — **due dates, status-change history, priority / estimate /
cycle / sub-issues, sorted lists, "what changed"** — **without a handler per
phrasing**. Discord-scoped questions ("what did Harsh post on discord") keep a
dedicated Discord-history scan + identity-resolution path.

Read-only Linear tools the engine can call:

- **`search_issues`** — full-text search over **title + description + comments**
  (acronym/synonym-aware — "DMs" and "direct messages" match the same issues).
- **`get_issue`** — one issue in full: state, assignee, labels, priority,
  estimate, dueDate, cycle, project, parent, sub-issues, latest comment.
- **`get_issue_history`** — the status / assignee / priority / due-date change
  timeline ("when did NFT2-610 change status").
- **`list_issues`** — filter by assignee / labels / state type / created-after /
  updated-after / due-date range / priority, sorted by `updatedAt`, `priority`,
  or `dueDate`.
- **`resolve_member` / `list_team_members`** — map a name to a Linear user.
- **`source_message_for_issue` / `tracked_issue_for_message`** — Discord↔Linear
  linking (see below).

Two cross-cutting behaviours:

- **Source scoping** — a question is scoped to **Discord**, **Linear**, or
  **both**. `discord` questions ("what did Harsh post on discord") use the local
  Discord scan; `linear` / `both` go to the tool-driven engine. A source-scoped
  reply never carries a section for the other source.
- **Edited messages re-trigger QUERY MODE only** — editing an @-mention question
  re-runs the query on the new text and posts a fresh reply; editing an ordinary
  message does nothing. Edits are **never** routed into the report/create path.

**Discord ↔ Linear linking.** The bot stores a message→issue mapping for every
issue it files/updates, and query mode can read it both ways: "which Discord
message is behind NFT2-591" (`source_message_for_issue` — returns the originating
message with author, timestamp, snippet, and a jump link) and, as a reply to a
message, "is this already tracked?" (`tracked_issue_for_message`). **Limitation:**
stored links only exist for issues **the bot itself created or updated** — for an
issue filed manually in Linear the engine says so plainly rather than guessing;
there is no heuristic message-matching (if added later, such matches would be
clearly labelled "possible match", not a stored link).

Query mode (and edit-handling) is **strictly read-only** — no create / comment /
status change, ever — and the tool loop is capped at **5 iterations** (then it
answers with whatever it has gathered).

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
@mention  ──▶ parse_query (LLM) ──┬─ source=discord ──▶ resolve_person
 or EDIT of    → is_query+source  │                     + scan_recent_messages
 an @mention                      │                     └─▶ LLM synthesis ──▶ reply
                                  │
                                  └─ source=linear|both ──▶ QUERY ENGINE
                                       (tool-use loop, ≤5 iterations)
                                       model ⇄ read-only Linear tools:
                                         search_issues · get_issue ·
                                         get_issue_history · list_issues ·
                                         resolve_member · list_team_members ·
                                         source_message_for_issue ·
                                         tracked_issue_for_message
                                       └─▶ final text ──▶ reply
```

## Project layout

```
discord-linear-bot/
├── README.md
├── requirements.txt
├── .env.example
├── main.py             # entry point
├── config.py           # env loading + allowlist parsing
├── db.py               # SQLite state store (+ read-only message↔issue linkage)
├── classifier.py       # report classifier + query router (parse_query) + activity synthesiser
├── linear_client.py    # Linear GraphQL client (labels, states, members, issues, history, comments)
├── query_engine.py     # tool-driven read-only Linear query engine (tool-use loop)
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
question. A lightweight LLM call (`classifier.parse_query`) decides two things:
is this a question at all (`is_query`), and its **`source`** scope
(`discord` | `linear` | `both`). Routing then follows the source:

- **`source = discord`** → the local Discord path: `query.resolve_person` +
  `scan_recent_messages` + LLM synthesis (*person activity*, below). Linear is
  never touched.
- **`source = linear` or `both`** → the **tool-driven query engine**
  (`query_engine.py`).

### The tool-driven engine (`linear` / `both`)

Instead of a handler per phrasing, the engine runs a **tool-use loop**: the bot's
own Claude call is given the read-only Linear tools plus the question, calls
whichever tools it needs, feeds the results back, and repeats until it has an
answer — capped at **5 tool iterations** (then it answers with what it has). It is
instructed to call tools rather than guess, prefer an explicit issue key when one
is given, say plainly when nothing matches, never invent issues/fields/links, and
keep replies Discord-short.

| Tool | Answers |
|------|---------|
| `search_issues(text, include_closed)` | subject lookups — full-text over title + description + comments |
| `get_issue(identifier)` | one issue in full — state, assignee, labels, priority, estimate, dueDate, cycle, project, parent, sub-issues, latest comment |
| `get_issue_history(identifier)` | "when did status change" / "what changed" — the change timeline with actor + timestamp |
| `list_issues(...)` | filtered lists — assignee / labels / state type / created-after / updated-after / due-date range / priority, sorted by `updatedAt` · `priority` · `dueDate` |
| `resolve_member(name)` / `list_team_members()` | name → Linear user id |
| `source_message_for_issue(identifier)` | originating Discord message(s) for a bot-tracked issue |
| `tracked_issue_for_message(message)` | the Linear issue filed/updated from a Discord message |

Questions it now handles with no new code:

```
@TriageBot status of DMs
@TriageBot when did NFT2-610 change status
@TriageBot what's due this week
@TriageBot what's assigned to Ravi sorted by priority
@TriageBot which discord message is behind NFT2-591
```

> **Matching spans more than titles.** `search_issues` uses Linear's `searchIssues`
> full-text index (title + description + comments), acronym/synonym-aware, so "DMs"
> and "direct messages" surface the same issues.
>
> **State-type quirk (this workspace).** "awaiting QA" and "Done" are typed
> `completed`; the engine keeps `include_closed=true` for status/subject lookups so
> live-but-technically-completed work still surfaces, and reports each issue by its
> literal state name rather than calling it "closed".

### Discord ↔ Linear linking

`db.py` stores a Discord-message-id → Linear-issue-UUID mapping for every issue the
bot files/updates; query mode reads it both ways:

- **`source_message_for_issue(NFT2-591)`** → the originating message(s): author,
  timestamp, a text snippet, and a reconstructed jump link
  (`https://discord.com/channels/<guild>/<channel>/<msg>`). A deleted/unreadable
  message degrades to a note, never an error.
- **`tracked_issue_for_message(...)`** → given a message id / jump link, or (as a
  reply) the replied-to message, the issue the bot filed from it — or
  `tracked: false`.

The engine replies with the **issue and the Discord jump link together**.
**Limitation:** links exist only for issues **the bot itself created or updated**
(the create / comment flows that reached approval); for a manually-filed issue the
engine says there's no stored link rather than guessing. There is **no heuristic
message-matching** — if added later, such matches would be labelled "possible
match", never presented as a stored link.

### Source scoping (`discord` / `linear` / `both`)

`parse_query` infers which system a question is about; a source-scoped reply only
touches — and only shows — that source:

| Phrasing cues                                              | `source`  | Route |
|------------------------------------------------------------|-----------|-------|
| "on discord", "in the channel", "posted", "mentioned/said" | `discord` | Discord scan only |
| "in linear", "assigned to", "ticket", "issue", "status of" | `linear`  | query engine |
| no explicit source                                         | `both`    | query engine |

```
@TriageBot what bugs did Harsh mention on discord today   → discord scan, Bug-filtered
@TriageBot what is Harsh assigned in linear               → engine (Linear)
@TriageBot what is Harsh working on                       → engine (Linear)
```

> **Note.** An unscoped person question ("what is Sid working on") parses as `both`
> and is answered by the engine from **Linear**. The blended Discord-activity
> summary is produced only for `discord`-scoped person questions (below).

### `person_activity` (Discord-scoped) — "what did `<person>` post?"

```
@TriageBot what did Harsh post on discord this week
@TriageBot what has Sid been saying in the channels
@TriageBot what bugs did Harsh mention on discord
```

Used for **`source = discord`** questions. Flow (`bot._handle_person_activity`):

1. **`query.resolve_person(name)`** maps the free-text name to a Linear user
   (matched against `list_team_members` by displayName / name / email, with
   first-name / partial fallback) **and** a Discord user (via `DISCORD_LINEAR_MAP`
   when the Linear user is mapped, else display-name match against recent posters
   / guild members). This shared identity layer is also what lets the engine's
   linking tools line up the right person.
   - **Ambiguous** (several plausible matches) → the bot asks which person and
     does nothing else — it never guesses.
   - **No match** → it says so plainly.
2. **Discord scan** — `scan_recent_messages(...)` walks `channel.history` across
   monitored channels only, matching by Discord ID (preferred) or display name,
   bounded by the lookback window and per-channel cap.
3. **Synthesis** — `classifier.summarize_person_activity` writes one concise
   **Recent Discord activity** summary (1–3 sentences + jump links to the most
   relevant messages). A `category` (e.g. `Bug`) biases it to matching messages.
   The model **summarises** — it never dumps raw logs or invents links; an empty
   scan is "nothing in Discord". On failure a deterministic fallback render is used.

The window defaults to `QUERY_DISCORD_LOOKBACK_DAYS` when the question gives no
time frame. A person's **Linear** assignments are answered by the engine instead
(ask "what is `<person>` assigned in linear").

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

### Hard guarantees

- Query mode — including edit re-triggers — is **read-only**. The engine is only
  ever given read tools; it never calls `create_issue`, `add_comment`, or
  `set_issue_status`, by construction.
- The engine tool loop is **bounded** (≤5 iterations); on the cap it answers with
  what it has rather than looping forever.
- Query mode runs **before** the report pipeline, so a question can never become a
  ticket; edits are never routed into the report pipeline at all.
- Tool errors are handled **inside** the loop (fed back as an error result), so a
  failed Linear read never crashes the reply — the model recovers or reports it.
- The engine is told never to invent issues, identifiers, links, or fields, and to
  say plainly when nothing matches.
- Reply-pings don't trigger queries — only explicit `<@bot_id>` mentions do.
- Anyone in the channel can query (no reporter allowlist for queries).

If the @-mention is in a monitored channel but the parser decides the message
isn't a question it can answer (`is_query=false`), the report path is given a
chance instead. In the approval channel that fall-through replies politely
with a help nudge instead of dropping silently.

## Architecture separation

Clean module roles:

- **`classifier.py`** — `Anthropic`-backed text/JSON producer. Prompts: report
  classifier, query **router** (`parse_query` → `is_query` + `source`), and the
  `person_activity` synthesiser (`summarize_person_activity`). **Never** touches
  Linear or Discord.
- **`query_engine.py`** — the tool-driven query engine: `Anthropic` tool-use loop
  over a set of read-only Linear tools (`search_issues`, `get_issue`,
  `get_issue_history`, `list_issues`, `resolve_member`, `list_team_members`), plus
  per-call `extra_tools` injected by `bot.py` for Discord↔Linear linking. Capped at
  5 iterations; read-only by construction.
- **`linear_client.py`** — all Linear reads/writes over GraphQL. No Linear MCP.
  Write/report helpers: `resolve_label_ids`, `resolve_assignee`, `create_issue`,
  `add_comment`, `set_issue_status`, `list_team_states`. Query-mode reads:
  `search_issues` / `find_issues_by_text` (full-text lookup), `get_issue` (full
  fields), `get_issue_history` (change timeline), `list_issues` /
  `list_issues_query` (flexible filter + sort), `resolve_member_id`,
  `list_team_members`, `active_issues_for_user`.
- **`query.py`** — read-only building blocks for the Discord-scoped path:
  `scan_recent_messages` (Discord history scan over monitored channels) and
  `resolve_person` (free-text name → Linear user + Discord user). Takes the
  Linear client and Discord client as parameters; never mutates either.
- **`db.py`** — SQLite state + the read-only `get_issue_for_message` /
  `get_messages_for_issue` linkage lookups the linking tools build on.
- **`bot.py`** — orchestrates Discord events, decides plans, posts embeds,
  handles ✅/❌, routes query mode (Discord scan vs engine), builds the linking
  tools, persists state via `db.py`.

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
- **Custom synonym/alias map** — the engine's `search_issues` already leans on
  Linear's full-text index (title + description + comments), which is
  acronym/synonym-aware (DMs ↔ "direct messages" resolves today). A bot-side alias
  map for domain jargon Linear's index *doesn't* connect would be a further step,
  but isn't needed for the common cases.
- **Heuristic issue↔message correlation** — linking is stored-mapping only (issues
  the bot filed/updated). Correlating an arbitrary Linear issue to recent Discord
  messages by keyword — clearly labelled "possible match" — is a deliberate
  non-goal for now; it would add a noisier content scan to the loop.
- **Per-channel category overrides** — easy to add in `bot._decide_plan`.
- **Metrics** — add Prometheus counters around classified / approved /
  rejected / commented / dup-matched.
