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
  estimate, dueDate, createdAt, started_at, cycle, project, parent, sub-issues,
  latest comment, **plus the state timeline** (`state_history` + `state_entered`).
- **`get_issue_history`** — the status / assignee / priority / due-date change-event
  log with actor ("who changed what and when").
- **`list_comments`** — all comments on an issue in date order (to read what
  actually happened: blockers, "waiting on API", "re-tested, fails").
- **`list_issues`** — filter by assignee / labels / state type / created-after /
  created-before / updated-after / due-date range / priority, sorted by
  `updatedAt`, `priority`, or `dueDate` (powers "overdue" / "due this week").
- **`resolve_member` / `list_team_members`** — map a name to a Linear user.
- **`recent_discord_activity`** — a person's recent monitored-channel posts (each
  flagged `done_signal`), for the reasoned "what is X working on" cross-check.
- **`source_message_for_issue` / `tracked_issue_for_message`** — Discord↔Linear
  linking (see below).
- **`list_standups` / `read_standup`** — read-only access to synced Gemini
  standup notes (see below); enabled only when `STANDUP_DIR` is set.
- **`search_archive`** — fallback lookup in the frozen Done-issues snapshot when
  live Linear can't find an issue; enabled only when `ARCHIVE_FILE` is set.
- **`who_is_on_leave`** — reads the holiday/leave channel for OOO context;
  enabled only when `HOLIDAY_CHANNEL_ID` is set.

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

**Read-only context sources (query mode only).** Beyond Linear, the engine can pull
in three local/Discord context sources — all **query-context only, never an input
to ticket creation**:

- **Standup notes** (`STANDUP_DIR`) and the **holiday/leave channel**
  (`HOLIDAY_CHANNEL_ID`) are read only to *answer questions*; the leave channel is
  **not monitored for triage** and never spawns a ticket.
- The **archive** (`ARCHIVE_FILE`) is a **frozen snapshot** — every archive-sourced
  answer is labelled *"(from archive snapshot, through &lt;consolidation date&gt;)"* and
  the engine knows nothing archived after that date.

Answers built on these **cite the source inline (Linear / standup / Discord / leave /
archive) and the dates** they're drawn from. Each source **no-ops gracefully** when
its config var is unset.

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
├── conventions.py      # TEAM_CONVENTIONS — team playbook, injected into both LLM prompts
├── db.py               # SQLite state store (+ read-only message↔issue linkage)
├── classifier.py       # report classifier + query router (parse_query) + activity synthesiser
├── linear_client.py    # Linear GraphQL client (labels, states, members, issues, history, comments)
├── query_engine.py     # tool-driven read-only Linear query engine (tool-use loop)
├── standup.py          # read-only parser for rclone-synced Gemini standup notes
├── archive.py          # read-only index of the frozen Done-issues snapshot (fallback)
├── query.py            # read-only person resolution + Discord activity scan + leave scan
└── bot.py              # Discord bot: on_message, on_raw_message_edit, on_raw_reaction_add, query mode
```

## Team conventions (`conventions.py`)

The NFThing / Membrane team's Linear conventions — roster, category rules, the
label set, priority mapping, the status lifecycle, and assignment rules — live in
a single string, `TEAM_CONVENTIONS` in **`conventions.py`**. It is **prepended to
both the classifier prompt and the query-engine prompt**, so classification,
ticket creation, and query answers all follow how the team actually runs Linear.
Edit the playbook there; every prompt picks it up. Highlights encoded below:
the **six-label set**, the **Bug + system-label** rule, and the **never-set-Done**
guard.

## Categories & labels

The classifier returns **strict JSON** with the following category vocabulary
(feature-vs-improvement decision test: *if it needs a new Linear Project it's a
feature; if it fits as a change to existing work it's an improvement*):

| Category      | Meaning                                                                     | Linear label applied |
|---------------|-----------------------------------------------------------------------------|----------------------|
| `bug`         | A defect — broken / not behaving as expected.                               | `Bug` (+ system label) |
| `feature`     | A **new** screen/flow/capability that doesn't exist yet (new endpoints / design sprint). | `Feature`  |
| `improvement` | A change to something that **already exists** (existing APIs / minor additions). | `Improvement`  |
| `noise`       | Chit-chat, acks, questions, plain status — nothing to do.                   | _(no ticket)_        |

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
Linear. (The playbook's `[FrontEnd]`/`[Backend]`/`[ML]`/`[Testing]` don't exist:
FrontEnd→`FE`, Backend→`BE`, and ML/Testing are skipped.)

**Bug + system-label rule.** A `Bug` **always** pairs a system label showing where
the fix lives — `BE` (logic/API), `FE` (frontend behaviour/cosmetic), or `UI` (a
state never designed). If the classifier doesn't supply one, the bot won't invent
it: the issue is forced to **needs-triage** (unassigned, flagged) for the PM to
add the label, rather than guessing.

## Reporter allowlist

Only messages from configured reporters are eligible for the report path.
Anyone can use **query mode** — in the dedicated query channel (`QUERY_CHANNEL_ID`,
no @-mention needed) or by @-mentioning the bot in a monitored channel.

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

Convention overrides: `needs_triage` forces unassigned; and a **bug is never
assigned to Harsh (QA)** — if he's the primary mention on a bug it's left
unassigned for PM triage. Missing-state/design bugs are meant for Arun, then
Ananda (surfaced in the description for the PM to route).

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
| `true`  | Proposed action is posted as an embed to `APPROVAL_CHANNEL_ID`. The embed states the action explicitly (e.g. **"Create (needs triage, unassigned)"**, **"Comment + move NFT2-123 → In Progress (never Done)"**). ✅ executes it, ❌ discards. |
| `false` | The action runs immediately. A short confirmation (`✅ Created [NFT2-123](url) — title`, `💬 Commented on [NFT2-123](url) and moved to **In Progress**`, or `… — status change left to the PM`) is posted to the approval channel. |

## Create vs. update (dedup)

**One Discord message never creates two tickets.** The bot decides between
creating a new issue and commenting on an existing one using two signals:

1. **Thread linkage** — if the message replies to / is in the same thread as
   an already-processed message that has a stored `linear_issue_id`:
   - `is_new_issue == false` → **comment** on that issue (author, timestamp,
     message text, attachment links). If `status_signal` is `resolved` or
     `in_progress` → also attempt a conservative, name-matched transition
     (`cannot_reproduce` stays comment-only; see below).
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

## Status transitions (by NAME — conservative, never Done)

The team's lifecycle is `Backlog → Todo → In Progress → Implemented → In Review →
Done | Canceled`, and **`In Progress`, `Implemented`, and `In Review` all share
the `started` type** — so the bot matches the target state by **NAME**, not type.
Transitions are deliberately conservative:

| `status_signal`      | Target state NAME | If absent          | Notes |
|----------------------|-------------------|--------------------|-------|
| `resolved`           | `Implemented`     | **comment-only**   | "at most Implemented" — dev-done, not tested |
| `in_progress`        | `In Progress`     | comment-only       | |
| `cannot_reproduce`   | _(none)_          | comment-only       | cancellation is a PM decision needing a reason |

**The bot MUST NEVER set Done or Released.** `set_issue_status` refuses to move an
issue into any `completed`/`canceled`-type state (or one named `done`/`released`)
regardless of the mapping — `Done` requires Harsh's written QA sign-off, a human
gate. Since the live workspace currently has **no `Implemented` state**, a
`resolved` signal today falls back to **comment-only** (which is also the
preferred behaviour: comment and leave the status change to the PM). Transitions
only apply when commenting on the parent thread issue — a dup-by-search `comment`
is never auto-transitioned. The approval embed / confirmation states the actual
outcome ("moved to **In Progress**" vs "status change left to the PM").

## Query mode (read-only)

Ask in the **dedicated query channel** (`QUERY_CHANNEL_ID`) — where every non-bot
human message is treated as a potential question, no @-mention required — or
**@-mention** the bot in a monitored channel. If `QUERY_CHANNEL_ID` is unset,
query mode falls back to answering @-mention questions in the **approval
channel** (previous behaviour). A lightweight LLM call (`classifier.parse_query`)
decides two things: is this a question at all (`is_query`), and its **`source`**
scope (`discord` | `linear` | `both`). Routing then follows the source:

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
| `get_issue(identifier)` | one issue in full — state, assignee, labels, priority, estimate, dueDate, createdAt, **started_at**, cycle, project, parent, sub-issues, latest comment, **plus the state timeline** (`state_history` [{state, entered_at, left_at}] + `state_entered`) so it can say when it moved to In Progress / Implemented / In Review |
| `get_issue_history(identifier)` | "who changed what and when" — the change-event log (state/assignee/priority/…) with actor + timestamp |
| `list_comments(identifier)` | all comments in date order (`[{author, createdAt, body}]`) — to read what actually happened (blockers, "waiting on API", "re-tested, fails") |
| `list_issues(...)` | filtered lists — assignee / labels / state type / created-after / **created-before** / updated-after / due-date range / priority, sorted by `updatedAt` · `priority` · `dueDate` (powers "overdue", "due this week", "created before X") |
| `resolve_member(name)` / `list_team_members()` | name → Linear user id |
| `recent_discord_activity(name)` | one person's recent monitored-channel posts, each flagged `done_signal` — to cross-check whether an In-Progress ticket is actually finished |
| `source_message_for_issue(identifier)` | originating Discord message(s) for a bot-tracked issue |
| `tracked_issue_for_message(message)` | the Linear issue filed/updated from a Discord message |

Questions it now handles with no new code:

```
@TriageBot status of DMs
@TriageBot when did NFT2-610 change status
@TriageBot what's due this week
@TriageBot what's assigned to Ravi sorted by priority
@TriageBot which discord message is behind NFT2-591
@TriageBot why isn't NFT2-675 done yet? / what's the timeline of NFT2-610
@TriageBot what's overdue / created before June
```

For a "why was X delayed / what's the timeline" question the engine reads
**get_issue's state timeline** (createdAt, started_at, dueDate, `state_history`)
**and `list_comments`** (what people actually said) **and**, if a person/delay is
implicated, `who_is_on_leave` — then explains citing concrete dates ("created 6 Jul,
In Progress 6 Jul, still not Implemented; a comment on 6 Jul says releasing after
testing"), never inventing an event that isn't in the data.

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

### Person status — "what is X working on" (reasoned, cross-source)

An unscoped person question (`source = both`) is answered by the engine as **one
reasoned answer**, not a list dump — it orchestrates all four sources:

1. **Linear** (authoritative "actively on"): `resolve_member(X)` →
   `list_issues(assignee_id, state_types=["started"])` — In Progress / In Review —
   with due dates and last-updated.
2. **Standup**: `read_standup` — the Next steps owned by X (what they committed to
   today), so the answer reflects what was actually said, not just ticket state.
3. **Discord**: `recent_discord_activity(X)` — recent posts, especially
   `done_signal` ones that may mean an In-Progress ticket is actually finished.
4. **Leave**: `who_is_on_leave` — whether X is/was off in the window.

It then **synthesises**: leads with what X is actively on (Linear In Progress +
today's standup commitment), notes anything X signalled **done** in Discord that
Linear hasn't caught up on, and flags leave — **labelling each fact's source
inline (Linear / standup / Discord / leave) and citing dates**. If sources
disagree (ticket says In Progress but Discord says "done"), it **surfaces the
discrepancy** rather than picking silently. **Read-only** — it reports; the
separate Discord create/update path is what actually moves `NFT2-xxx`. (A
Discord-*scoped* "what did X post" question still uses the lighter
`person_activity` scan path below.)

### Standup notes (read-only, query-only)

A **separate rclone process** syncs "Notes by Gemini" standup docs from Google
Drive into a local folder (`STANDUP_DIR`); the bot only **reads local files** and
holds **no Google credential**. Standup data is a query-mode context source **only —
it is never an input to ticket creation**. Two read-only tools (`standup.py`):

- **`list_standups(days=14)`** → recent notes as `[{date, session (AM/PM), title,
  path}]`. It targets files whose name/title carries the "Notes by Gemini" marker
  **and** a date; the shorter "Notes - …" stubs are ignored.
- **`read_standup(date?, session?)`** → the note's **Summary**, **Decisions/Aligned**
  bullets, and **Next steps** parsed into `[{owner_name, task, owner_linear}]` by
  splitting on the leading `[Name]` tag (owner names are mapped to Linear users via
  the same resolver used elsewhere — full-name invitees like "Shriraksha M" fall
  back to the first name). Omit `date` for the most recent; `raw` text is the fallback.

The parser is **format-tolerant** (`.docx` via stdlib `zipfile`, plus `.txt` / `.md`
/ `.html`) so it works regardless of how rclone exports Google Docs, and it
**no-ops gracefully** when `STANDUP_DIR` is unset/empty/missing.

- **Sync-on-demand:** when a question is clearly about a recent/today standup
  ("this morning", "today's sync", "standup", "what did we decide today") **and**
  `STANDUP_SYNC_CMD` is set, the bot runs that command (subprocess, short timeout)
  **before** reading, so a just-finished standup isn't missed. Otherwise it reads
  what's on disk.
- **Freshness:** any reply that uses standup data states what's on file (e.g.
  *"Latest standup on file: AM sync 2026-07-07 (synced 10:47)"*). If today's isn't
  present after an on-demand sync, the engine says so rather than implying none
  happened.

### Archive snapshot (read-only fallback)

`ARCHIVE_FILE` points at a **frozen** markdown file of past Done issues
(`archive.py` loads + indexes it **once at startup**). It's a **fallback**: when
live Linear can't return an issue the user referenced (archived / not found), the
engine calls **`search_archive(query)`** — by identifier (`NFT2-123`) or keywords —
and gets `[{identifier, title, labels, priority, owner, completed_date, url}]`.

The parser is **format-tolerant** (markdown table **or** per-issue sections, both
anchored on `NFT2-<n>` ids; URLs that contain an id don't split an entry). Every
archive-sourced answer **must carry the provenance label** the tool returns —
*"(from archive snapshot, through <date>)"* (the through-date is an explicit header
date, else the latest `completed_date`) — so it's **never mistaken for live data**;
the snapshot knows nothing archived after that date. No-ops when `ARCHIVE_FILE` is
unset/missing.

### Holiday / leave channel (read-only context)

`HOLIDAY_CHANNEL_ID` names a Discord channel where people post OOO / on-leave notes.
**`who_is_on_leave(days=45, around_date?)`** (`query.who_is_on_leave`) scans it
read-only and extracts `[{person, dates, note, posted_at, jump_url}]` from freeform
messages — tolerant of phrasing, pulling explicit dates (`2026-07-02`, "4th July")
and today/tomorrow relative to the post, and naming the subject ("Arun is on leave"
→ Arun, else the poster). Used to explain delays ("Arun was on leave 2026-07-02, so
nothing moved"). This channel is **NOT monitored for triage** — reads only, never a
ticket. No-ops when `HOLIDAY_CHANNEL_ID` is unset.

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
chance instead. In a **query-only channel** (the dedicated query channel, or the
approval channel in fallback mode) there is no report path, so that fall-through
replies politely with a help nudge instead of dropping silently. The query
channel is never triaged — if it accidentally overlaps `MONITORED_CHANNEL_IDS`,
startup logs a warning and the bot treats it as query-only (no ticket creation).

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
channel → Copy Channel ID. The bot uses **three distinct channel roles** — keep
them separate:

- One or more **monitored** channel IDs (`MONITORED_CHANNEL_IDS`) — where
  reporters post; messages here get triaged into Linear tickets.
- One **approval** channel ID (`APPROVAL_CHANNEL_ID`, private staff-only) — where
  ✅/❌ approval embeds are posted and handled. Nothing else happens here.
- One **query** channel ID (`QUERY_CHANNEL_ID`) — where anyone asks the bot
  questions (read-only). No @-mention needed and no ticket creation. Must **not**
  be one of the monitored channels. Optional — if omitted, queries are answered
  via @-mention in the approval channel (previous behaviour).

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
| `QUERY_CHANNEL_ID`        | _(empty)_              | Dedicated read-only query channel (no @-mention needed). Empty → queries answered via @-mention in the approval channel. Must not be a monitored channel. |
| `QUERY_DISCORD_LOOKBACK_DAYS` | 14                 | Days back to scan Discord for a `person_activity` query.                |
| `QUERY_MAX_MESSAGES_PER_CHANNEL` | 400             | Hard cap on messages scanned per monitored channel per query.           |
| `STANDUP_DIR`             | _(empty)_              | Local folder rclone syncs Gemini standup notes into. Empty → standups disabled. |
| `STANDUP_SYNC_CMD`        | _(empty)_              | Optional shell sync command run before reading a "today/this morning" standup query. |
| `ARCHIVE_FILE`            | _(empty)_              | Frozen markdown snapshot of Done issues; read-only fallback when live Linear can't find an issue. |
| `HOLIDAY_CHANNEL_ID`      | _(empty)_              | Discord channel of OOO/leave posts, read for delay context. NOT triaged. |

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
