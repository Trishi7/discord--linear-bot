"""Tool-driven Linear query engine (READ-ONLY).

This is the BOT's own Anthropic call — NOT Claude Code. It replaces the old
fixed per-intent handlers (issue_status / issue_list / the Linear side of
person_activity) with a single tool-use loop: the model is handed a set of
read-only Linear tools and answers an open-ended question by calling them, so
new phrasings (due dates, priority, estimates, cycles, sub-issues, "what
changed", sorted lists, …) need no new handler.

Design:
  - Tools map 1:1 to read-only `LinearClient` methods. Everything is READ-ONLY;
    there is no create/comment/update tool here by construction.
  - The loop caps at MAX_TOOL_ITERATIONS; on cap it asks the model for a final
    answer with the tools removed, so a reply is always produced.
  - Tool errors are fed back as tool_result JSON with an "error" key — the model
    recovers or reports the failure rather than the loop crashing.
  - Reuses the existing LINEAR_API_KEY via `LinearClient`; NO MCP / OAuth.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic

from conventions import TEAM_CONVENTIONS
from persona import cos_preamble

log = logging.getLogger(__name__)

# Loop bounds. ~5 tool rounds is enough for resolve-member → filter → detail
# chains while capping cost/latency; each model turn is bounded by max_tokens.
MAX_TOOL_ITERATIONS = 5
MAX_TOKENS = 1024


# JSON-schema tool definitions handed to `client.messages.create(tools=...)`.
# Names match the dispatch table in `QueryEngine._dispatch`.
LINEAR_TOOLS = [
    {
        "name": "search_issues",
        "description": (
            "Full-text search the team's Linear issues by title/description/comments "
            "(case-insensitive, acronym-aware — 'DMs' and 'direct messages' both match). "
            "Use when the user names an issue by subject rather than by key. Returns a "
            "ranked list (open issues first) of {identifier, title, state_name, "
            "state_type, assignee_name, url, updatedAt}. NOTE: this team types some "
            "in-flight states (e.g. 'awaiting QA') as 'completed', so leave include_closed "
            "at its default (true) for status/subject lookups or you may hide live issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Keywords / subject to search for."},
                "include_closed": {
                    "type": "boolean",
                    "description": (
                        "Include completed/canceled issues. Default TRUE. Pass false ONLY "
                        "when the user explicitly wants open issues."
                    ),
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "get_issue",
        "description": (
            "Fetch ONE issue in full by its identifier (e.g. 'NFT2-610'). Returns "
            "title, description, state{name,type}, assignee, labels, priority, estimate, "
            "dueDate, cycle, project, parent, children (sub-issues), createdAt, updatedAt, "
            "started_at, url, the latest comment, AND the timeline: state_history "
            "([{state, type, entered_at, left_at}]) plus state_entered (first time each "
            "state was entered) — so you can say when it moved to In Progress / Implemented "
            "/ In Review. Prefer this whenever the user gives an explicit key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Issue key, e.g. 'NFT2-610'."}
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "get_issue_history",
        "description": (
            "Return the change history of ONE issue (state, assignee, priority, title, "
            "due-date, estimate transitions) as timestamped events with the actor. Use for "
            "'who changed what and when'. Returns a list of {at, actor, changes[]}, oldest "
            "first. (For a plain state timeline, get_issue's state_history is usually enough.)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Issue key, e.g. 'NFT2-610'."}
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "list_comments",
        "description": (
            "Return ALL comments on ONE issue as [{author, createdAt, body}] in date order "
            "(oldest first). Read the actual discussion to explain WHY something happened — "
            "blockers, 'waiting on API', 're-tested, still fails', hand-offs. Use this "
            "alongside get_issue's timeline for 'why was X delayed / what's the status of X'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Issue key, e.g. 'NFT2-610'."}
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "list_issues",
        "description": (
            "List/filter the team's issues. All filters are optional and AND-ed. Use "
            "assignee_id (resolve a name first via resolve_member or list_team_members), "
            "labels, state_types, date bounds, priority, and 'order' to sort. Returns compact "
            "{identifier, title, state, assignee, priority, dueDate, updatedAt, url}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "assignee_id": {
                    "type": "string",
                    "description": "Linear user id to filter by assignee. Resolve a name to an id first.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names, e.g. ['Bug'] — issue must carry one of them.",
                },
                "state_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["backlog", "unstarted", "started", "completed", "canceled", "triage"],
                    },
                    "description": "Workflow state types. 'started'=in progress, 'completed'=done, etc.",
                },
                "created_after": {"type": "string", "description": "ISO-8601 timestamp; issues created on/after."},
                "created_before": {"type": "string", "description": "ISO-8601 timestamp; issues created on/before."},
                "updated_after": {"type": "string", "description": "ISO-8601 timestamp; issues updated on/after."},
                "due_after": {"type": "string", "description": "Date YYYY-MM-DD; dueDate on/after (range start)."},
                "due_before": {"type": "string", "description": "Date YYYY-MM-DD; dueDate on/before (range end)."},
                "priority": {
                    "type": "string",
                    "enum": ["urgent", "high", "medium", "low", "none"],
                    "description": "Exact priority filter.",
                },
                "order": {
                    "type": "string",
                    "enum": ["updatedAt", "priority", "dueDate"],
                    "description": "Sort: updatedAt (default, newest first), priority (urgent first), dueDate (soonest first).",
                },
                "limit": {"type": "integer", "description": "Max rows (default 20)."},
            },
            "required": [],
        },
    },
    {
        "name": "resolve_member",
        "description": (
            "Resolve a free-text person name (e.g. 'Ravi', 'me' already substituted) to ONE "
            "Linear team member id. Returns {id, displayName} on a confident match, or "
            "{error, candidates} when nothing/many match. Call this before filtering "
            "list_issues by assignee."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Person name to resolve."}},
            "required": ["name"],
        },
    },
    {
        "name": "list_team_members",
        "description": "List all Linear team members as {id, name, displayName, email}. Use to look up who exists.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_projects",
        "description": (
            "List the team's Linear PROJECTS as [{name, target_date, lead, status, "
            "priority, progress_pct, summary, milestone_count, url}]. A project is a "
            "feature/launch (e.g. 'DMs & Group Chat (v1)', 'Onboarding') — bigger than an "
            "issue. Use this to see what projects exist, or to resolve a spoken feature "
            "name to a real project before answering. 'status' is the project's own state "
            "(e.g. Backlog / In Progress / Implemented); 'target_date' is the "
            "launch/release date when the project has no milestones."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_project",
        "description": (
            "Full detail for ONE project, resolved from a name or the team's shorthand "
            "('DMs', 'messaging', 'onboarding', 'pulse'). Returns the project meta "
            "(name, summary, description, url, start_date, target_date, lead, status, "
            "priority, progress_pct, health), its MILESTONES (each with target_date and "
            "issue_total / issue_completed), and issue_totals (total plus counts "
            "by_state_name and by_state_type). LEAD project-level answers with this: the "
            "release date is the LAUNCH milestone's target_date, or the project "
            "target_date when there are no milestones. If it returns {error, candidates}, "
            "the name didn't resolve to one project — pick from candidates or fall back to "
            "search_issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Project name or shorthand, e.g. 'DMs', 'onboarding'.",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_project_issues",
        "description": (
            "List the issues IN a project (resolved from a name/shorthand), each with "
            "identifier, title, state, labels, assignee, milestone, dueDate, updatedAt, "
            "priority, url. Use this for the drill-down AFTER a project-level summary, and "
            "for 'any FE/BE work left for X' — pass labels=['FE'] (or ['BE']) and "
            "state_types to filter server-side. Returns {project, target_date, issues[]} "
            "or {error, candidates} if the name doesn't resolve to one project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Project name or shorthand, e.g. 'DMs'.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names to filter by, e.g. ['FE'] or ['BE','Bug'].",
                },
                "state_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["backlog", "unstarted", "started", "completed", "canceled", "triage"],
                    },
                    "description": "Workflow state types to keep. Omit for all.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_milestones",
        "description": (
            "List a project's MILESTONES (resolved from a name/shorthand), each with "
            "target_date and completion (issue_total / issue_completed). Returns "
            "{project, target_date, milestones[]}; milestones is [] with a note when the "
            "project has none (common — then use the project target_date as the "
            "launch/release date). {error, candidates} if the name doesn't resolve."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Project name or shorthand, e.g. 'DMs'.",
                }
            },
            "required": ["name"],
        },
    },
]


def _system_prompt(*, requester_name: str, requester_linear_id: Optional[str], today: str) -> str:
    who = f"The person asking is **{requester_name}**"
    if requester_linear_id:
        who += f" (their Linear user id is {requester_linear_id})"
    who += (
        '. When they say "me", "my", "mine", or "I", they mean themselves — use that '
        "identity/id directly instead of resolving a name."
    )
    return cos_preamble() + TEAM_CONVENTIONS + "\n\n" + f"""You answer questions about the NFThing /
NFThing2.0 team's Linear issues, posted in Discord, following the TEAM CONVENTIONS
above. NFThing2.0 is the team's product; issues use keys like NFT2-123. Today's
date is {today} (UTC).

{who}

You have READ-ONLY tools to inspect Linear. You CANNOT create, edit, comment on,
or move anything — only read. Answer by CALLING TOOLS, never by guessing:

- If the user gives an issue key (e.g. NFT2-610), call get_issue (or
  get_issue_history for "when did X change / what changed") directly — don't search.
- If they name an issue by subject ("the DMs issue", "payout bug"), use
  search_issues (keep include_closed=true) and list every match briefly with its
  status rather than picking one. NOTE: this team's workflow types the states
  "awaiting QA" and "Done" as 'completed' and "In Progress"/"In Review" as
  'started' — so an issue in "awaiting QA" is still active work, not abandoned;
  report its literal state name, don't call it "closed".
- For group questions ("what's due this week", "assigned to Ravi", "open bugs"),
  use list_issues. Resolve any person name to an id with resolve_member first,
  then pass assignee_id. Compute date ranges from today's date above.
- If the question names a FEATURE / LAUNCH / area rather than one issue ("DMs",
  "direct messaging", "the DMs feature", "onboarding", "pulse", "wallet") — ESPECIALLY
  "when does X go live / ship", "status of X", "is X on track", "any FE/BE work for X" —
  treat X as a possible PROJECT first: see PROJECT-LEVEL QUESTIONS below. Only fall
  back to search_issues (keyword issue search) if X doesn't resolve to a project.
- For "sorted by priority / due date", pass the matching 'order' to list_issues.
- Dates & comments are first-class. For a single issue's status / timeline / "why
  delayed", see the ISSUE-THREAD QUESTIONS section below (read the comments and
  state history, not just the current fields). For group date filters — "overdue" /
  "due this week" / "created before X" — use list_issues' dueDate / created_before /
  created_after bounds. NEVER invent a date, comment, or event that isn't in the
  tool data.

PROJECT-LEVEL QUESTIONS — when the subject is a PROJECT (a feature/launch), answer at
the project level FIRST, then drill into issues. A project is bigger than an issue:
"DMs & Group Chat (v1)", "Onboarding", "Pulse — V2". Colloquial names resolve via the
tools' alias/fuzzy matching, so pass the user's own word ("DMs", "messaging").
- RESOLVE FIRST: call get_project(name) (or list_projects to see what exists / when a
  name is ambiguous). If it returns {error, candidates}, tell the user briefly which
  projects it could be, or pick the obvious one — don't silently fall back to a flat
  issue search when a project clearly matches.
- "when does X go live / ship / what's the release date": report the LAUNCH milestone's
  target_date if the project has milestones; OTHERWISE (most projects here have none)
  the project's own target_date IS the launch date. State it plainly with the weekday,
  e.g. "DMs v1 is targeted to go live Thursday 16 Jul." Then, if asked or relevant,
  separate LAUNCH-BLOCKING work from post-launch: an issue whose dueDate is on/before
  the target date (or whose scope the description marks as v1) is launch-blocking; work
  clearly scoped as later/post-launch being incomplete does NOT move the launch date —
  say so.
- "what's the status of X": use get_project's issue_totals + milestones to roll up BY
  MILESTONE (when present) and BY STATE, and LEAD WITH THE LAUNCH PICTURE, e.g. "13 of
  17 issues are dev-complete/awaiting QA, 2 in QA (In Review), 2 still In Progress;
  target Thu 16 Jul." Remember this team types "awaiting QA" and "Done" as 'completed'
  and "In Progress"/"In Review" as 'started' — an awaiting-QA issue is dev-done, not
  abandoned; report the literal state names.
- "is X on track / will it make the date": compare the INCOMPLETE launch issues (state
  not 'completed'/'canceled', dueDate on/before target) against the target_date and the
  days remaining from today; if a few urgent/high issues are still 'started' with the
  date imminent, flag the risk plainly. Don't over-claim — say what the counts show.
- "any FE/BE work (left) for X": call get_project_issues(name, labels=['FE'] or ['BE'],
  state_types=['backlog','unstarted','started'] for "left/remaining"). List what comes
  back in the one-line-per-issue shape.
- DRILL DOWN AFTER the project-level summary: use get_project_issues to list the child
  issues grouped by milestone (when present) or by state — the same compact
  one-line-per-issue OUTPUT FORMAT below. Lead with the summary; the list is the detail.
- NEVER invent a target date, milestone, or count — use only what the project tools
  return. If target_date is null, say the project has no target date set rather than
  guessing one.

Person status — "what is X working on / up to". GATHER from ALL sources, then lay
the answer out in the FIXED SECTIONS below — the three evidence sections in order,
then a REQUIRED Summary. GATHER:
1. Linear (authoritative "actively on"): resolve_member(X), then
   list_issues(assignee_id=<id>, state_types=["started"]) — In Progress / In Review
   (and Implemented if that state exists) — noting dueDate and updatedAt. This is
   the primary signal for what they're actively working on. ALSO call
   list_issues(assignee_id=<id>, state_types=["unstarted","backlog"]) for what's
   assigned but not yet started (the "Also Assigned" section).
2. Standup: read_standup (most recent) — the Next steps owned by X = what they
   committed to / were assigned today (reflect what was actually said, not just
   ticket state).
3. Discord: recent_discord_activity(X) — recent posts, ESPECIALLY ones with
   done_signal=true, which may mean an In-Progress ticket is actually finished.
4. Leave: who_is_on_leave(around today's date) — is/was X off in the window?

OUTPUT — keep these sections, IN THIS ORDER (unchanged from today), each intro'd by
a short **bold label** (no markdown headers):
- **Active / In Progress (Linear)** — one line per assigned started issue in the
  standard shape (identifier · assignee · title · priority · state) per the OUTPUT
  FORMAT rules below. If none, say so briefly.
- **Also Assigned (Todo/Backlog)** — the unstarted/backlog issues, same one-line
  shape. Omit this section entirely if there are none.
- **Standup** — what X committed to / was assigned in the most recent AM/PM notes.
  State which sync + date and add the one-line freshness ("Latest standup on file:
  AM sync 2026-07-08"). If X has no items in that note, say so.
- **Discord** — a 1–2 sentence summary of X's recent monitored-channel activity
  (deploys, blockers, done/fixed signals) with jump links to the 1–3 most relevant
  posts. If nothing was found, say "nothing in Discord this week" — never imply
  activity that isn't in the data.
- **Summary** — REQUIRED on EVERY person-activity answer; always LAST, AFTER all
  three sections above (it is an ADDITION to them, never a replacement). Write 2–4
  sentences of PLAIN PROSE — no bullets, no bold label lines, no headers inside it —
  that reconcile all three sources into one picture of what X is actually doing right
  now. It MUST:
  · LEAD with what X is actively on, weighing the sources: today's standup commitment
    is the strongest signal of intent; Linear "In Progress" is the system of record;
    recent Discord "done/fixed/deployed" messages are the freshest reality.
  · EXPLICITLY call out DISAGREEMENTS between sources rather than smoothing them
    over, e.g. "NFT2-660 is still In Progress in Linear, but Harsh said in Discord on
    Jul 8 that it's tested and passing, so the ticket is likely stale."
  · Mention leave/OOO if relevant (holiday channel) and any blockers surfaced in
    Linear comments.
  · Cite dates and sources inline (Linear / standup / Discord). Never invent facts
    not in the retrieved data; if a source returned nothing, SAY SO ("nothing in
    Discord this week") rather than implying activity.
  · Stay compact — this is Discord; the message-splitting and formatting rules apply.
Throughout, LABEL each fact's source inline (Linear / standup / Discord / leave) and
cite dates. You only REPORT — never move the ticket (a separate path does that).

Discord ↔ Linear linking (only if those tools are provided):
- When a question spans BOTH systems — "which Discord message/report is behind
  NFT2-591", "show the report for this issue", "link NFT2-591 to its Discord
  thread" — call source_message_for_issue with the identifier.
- When asked "is this (message/report) already tracked?" — usually as a reply to
  a Discord message — call tracked_issue_for_message (omit message_id to use the
  replied-to message).
- Always reply with BOTH the Linear issue AND the Discord jump link together
  when a link exists. If no stored link exists, say so plainly: the bot only
  records links for issues it created or updated itself.

STANDUP QUESTIONS (only if the standup tools are provided) — "what was discussed in
today's / yesterday's standup", "what happened in the AM/PM sync", "what did we
decide this morning", "standup summary", "action items from standup", "what did
<person> commit to today". READ-ONLY context, synced "Notes by Gemini" docs. NEVER
treat them as Linear data and NEVER create or move a ticket from standup content —
answering only:
- RESOLVE THE DATE from today's date above and pass it to read_standup as
  date=YYYY-MM-DD (do the arithmetic yourself; never invent a date):
    "today" / "this morning's standup" → today's date.
    "yesterday" / "last night" / "yesterday evening" → today − 1 day.
    a weekday ("Monday") → the most recent past date that fell on that weekday.
    an explicit date ("Jul 7", "7 Jul", "2026-07-07") → that calendar date.
    vague ("the last standup", "latest") → OMIT date entirely (most recent on file).
- RESOLVE THE SESSION and pass session=AM/PM only when the user names one:
    "this morning" / "kick-off" / "AM" → AM.
    "last night" / "evening" / "wrap-up" / "PM" → PM.
    no session named → OMIT session. The tool returns the most recent one for that
    date and lists 'other_sessions_available'; if that day also has the other sync,
    ADD a short line like "(the AM sync is also on file)".
- Use list_standups when the user is vague or asks "which standups do we have".
- ANSWER FROM THE DOC'S STRUCTURE, in this order: the Summary (one or two lines),
  then the Decisions/Aligned bullets, then the Next steps as owner → task. Attribute
  each decision to who made it WHERE THE NOTES SAY so (e.g. "Sid: ship FKTR Thu");
  if a bullet names no one, report it without inventing an owner. If a section is
  absent in the note, summarise what IS there — don't fabricate the missing section.
- If the user asks about a specific person ("what did Ravi commit to", "Ananda's
  action items"), FILTER Next steps to that owner (match owner_name; owner_linear
  confirms identity when present) and lead with those. Say so if that person has no
  items in the note.
- ALWAYS STATE WHICH STANDUP you're reading, e.g. "AM sync, 2026-07-08" — and add
  the one-line freshness ("Latest standup on file: AM sync 2026-07-08"). EVERY
  standup answer carries this freshness line.
- HANDLE MISSING DATA HONESTLY — never substitute a different day:
    · configured=false → standup access isn't set up; say exactly that, don't claim
      "nothing was discussed".
    · found=false → the requested standup (e.g. today's AM) isn't on file yet, even
      after the on-demand sync. SAY SO PLAINLY and name the most recent that IS on
      file (from 'freshness'/list_standups), e.g. "No AM sync for 2026-07-09 on file
      yet; the most recent is the PM sync from 2026-07-08 — want that instead?" NEVER
      answer from another day's note as if it were the one asked for, and never imply
      a standup didn't happen or invent its contents.
    · sync_ran=true but sync_ok=false → the refresh failed; answer from what's on
      disk and add that the sync failed so the data may be stale.
- next_steps carry owner_linear when the owner maps to a Linear user; use it to
  connect an action item to that person, but don't invent a mapping that's null.

ISSUE-THREAD QUESTIONS — "what is being discussed on NFT2-610", "what's happening
with the DMs issue", "why is this delayed". Explain the CONVERSATION and status of
one issue, not just its current fields:
- Resolve the issue first: get_issue on an explicit key, else search_issues on the
  subject ("the DMs issue") and pick the best match (if several plausibly match,
  say which you're reading). Then read its COMMENTS in date order (list_comments)
  and its state timeline (get_issue's state_history / state_entered; get_issue_history
  for who changed what).
- SUMMARISE THE THREAD: who said what and when (cite dates), what blockers or
  decisions emerged ("waiting on the payments API", "re-tested 8 Jul, still fails",
  hand-offs), and what the CURRENT STATE means per the conventions above —
  "Implemented" = dev-done, deployable to FKTR, NOT yet QA'd; "In Review" = under
  QA; "Done" = QA-signed-off. Report the literal state name; an issue in "awaiting
  QA" is active work, not closed.
- If the issue was raised from Discord and a link is stored, call
  source_message_for_issue and INCLUDE THE DISCORD JUMP LINK for the original
  report. If no stored link exists, say the bot only records links for issues it
  filed/updated itself — don't imply there's no Discord origin.
- For "why is this delayed": combine the STATE-HISTORY DATES + the COMMENTS + any
  LEAVE in the holiday channel (who_is_on_leave around the relevant dates), and
  explain with concrete dates, e.g. "created 6 Jul, In Progress 8 Jul, still not
  Implemented; a 9 Jul comment notes the blocked payments API; Ananda was on leave
  7–8 Jul." If the data doesn't support a reason, say "no explanation found in the
  comments/history" — NEVER speculate or invent a cause.

Archive snapshot (only if the tool is provided) — a FROZEN file of past Done
issues. Use search_archive ONLY as a fallback: if get_issue / search_issues can't
find an issue the user referenced (it may be archived), try search_archive. When
you use archive data you MUST append its 'provenance_label' (e.g. "(from archive
snapshot, through 2026-07-07)") and never present it as live Linear — the snapshot
knows nothing archived after its through-date.

Leave / holiday (only if the tool is provided): for "why was X delayed", "was X
around", or a stalled-work question, call who_is_on_leave (pass around_date when a
specific day matters). Use it to explain gaps ("Arun was on leave 2026-07-02"), but
don't over-claim causation — report what the leave notes actually say.

Rules:
- NEVER invent issues, identifiers, links, statuses, dates, or fields. Use only
  what the tools return. If a field isn't present, say it isn't set.
- If nothing matches, say so plainly and briefly — don't pad.
- When reporting status, use the lifecycle meaning from the conventions above:
  "Implemented" = dev-done, deployable to FKTR, NOT yet QA'd; "In Review" = under
  QA; "Done" = QA-signed-off. Never imply the bot changed a status here — this is
  read-only.
- OUTPUT FORMAT — keep it COMPACT and Discord-friendly; most answers should fit
  in ONE message (under ~1800 characters). Follow these exactly:
  - NO markdown headers (no #, ##, ###, ####). Use a short **bold label** or just
    plain text to introduce a section.
  - ONE LINE PER ISSUE, no bullet needed, in this shape:
    `[NFT2-660](url) — Harsh — 1:1 DMs · Urgent · In Progress`
    i.e. link · assignee · short title · priority · state — joined by " · ".
    Omit any field that isn't set rather than writing "none".
  - NO blank lines between items or sections — a single line break is enough.
  - Link issues as [NFT2-123](url) using the url from the tool result.
  - When you cite standup freshness, keep it to ONE compact line, e.g.
    "Latest standup on file: AM sync 2026-07-07 (synced 10:47)."
- If the answer would still run very long (more than ~15 issues / ~3 messages'
  worth), DON'T dump everything: lead with the most relevant items (most recent
  or highest priority) and end with a short line like "…and 12 more — narrow by
  assignee/label/state to see them."
- When a tool returns an error, briefly tell the user what failed; don't retry
  endlessly."""


class QueryEngine:
    """Runs a bounded tool-use loop against read-only Linear tools."""

    def __init__(self, api_key: str, model: str, linear) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._linear = linear

    async def answer(
        self,
        *,
        question: str,
        requester_name: str = "",
        requester_linear_id: Optional[str] = None,
        extra_tools: Optional[list[dict]] = None,
        history: Optional[list[dict]] = None,
    ) -> Optional[str]:
        """Answer `question` by looping model ⇄ tools. Returns the final reply
        text, or None on total failure (the caller can fall back to a nudge).

        `extra_tools` are per-call, caller-supplied read-only tools on top of the
        built-in Linear set — each is {"schema": <tool def>, "handler": async
        fn(input_dict) -> jsonable}. bot.py uses this to inject the Discord↔Linear
        linking tools (which need the DB + Discord client) without coupling the
        engine to Discord.

        `history` is the last few prior turns in this channel/thread as
        [{"question", "answer"}] (oldest first). They're replayed as user/
        assistant messages BEFORE the current question so a follow-up that omits
        the subject ("what about Ravi?") resolves against what was just asked.
        Short-term working context only — never persisted, never a ticket input."""
        extra_tools = extra_tools or []
        extra_handlers = {
            t["schema"]["name"]: t["handler"] for t in extra_tools
        }
        tools = LINEAR_TOOLS + [t["schema"] for t in extra_tools]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        system = _system_prompt(
            requester_name=requester_name or "(unknown)",
            requester_linear_id=requester_linear_id,
            today=today,
        )
        # Replay recent turns first so the model can resolve a follow-up that
        # leans on them; the current question always comes last.
        messages: list[dict] = []
        for turn in history or []:
            q = str((turn or {}).get("question") or "").strip()
            a = str((turn or {}).get("answer") or "").strip()
            if q and a:
                messages.append({"role": "user", "content": q})
                messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": question})
        log.info(
            "[engine] start question=%r requester=%r linear_id=%s history_turns=%d",
            question[:160], requester_name, requester_linear_id,
            len([t for t in (history or []) if (t or {}).get("question")]),
        )

        last_text = ""
        for i in range(MAX_TOOL_ITERATIONS):
            log.info("[engine] iteration %d/%d: calling model", i + 1, MAX_TOOL_ITERATIONS)
            try:
                resp = await asyncio.to_thread(
                    self._client.messages.create,
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            except Exception:
                log.exception("[engine] model call raised")
                return last_text or None

            text_now = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            if text_now:
                last_text = text_now

            if resp.stop_reason != "tool_use":
                log.info(
                    "[engine] iteration %d: final (stop=%s) len=%d",
                    i + 1, resp.stop_reason, len(last_text),
                )
                return last_text or None

            # Execute every tool_use block, feed results back.
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                log.info("[engine] tool_use %s input=%s", block.name, block.input)
                result = await self._dispatch(
                    block.name, block.input or {}, extra_handlers
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        # Hit the iteration cap — force a final answer with the tools removed.
        log.info("[engine] hit tool-iteration cap; requesting final answer without tools")
        messages.append(
            {
                "role": "user",
                "content": (
                    "You've reached the tool-call limit. Answer now, concisely, using "
                    "only what you've already gathered. If it's incomplete, say so."
                ),
            }
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
            )
            final = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            return final or last_text or None
        except Exception:
            log.exception("[engine] final no-tools call raised")
            return last_text or None

    async def _dispatch(self, name: str, tool_input: dict, extra_handlers: Optional[dict] = None):
        """Execute one tool, returning JSON-serialisable data. Caller-supplied
        `extra_handlers` (Discord↔Linear linking) win over the built-in Linear
        tools. All reads already swallow their own errors and return []/None;
        here we additionally wrap unexpected exceptions into an {"error": ...}
        object so the model can recover inside the loop."""
        try:
            if extra_handlers and name in extra_handlers:
                return await extra_handlers[name](tool_input)

            if name == "search_issues":
                text = str(tool_input.get("text") or "").strip()
                # Default TRUE: this team types 'awaiting QA' as completed, so an
                # open-only search would hide live issues in that state.
                include_closed = bool(tool_input.get("include_closed", True))
                if not text:
                    return {"error": "search_issues requires 'text'"}
                return await self._linear.find_issues_by_text(
                    text, include_closed=include_closed
                )

            if name == "get_issue":
                ident = str(tool_input.get("identifier") or "").strip()
                if not ident:
                    return {"error": "get_issue requires 'identifier'"}
                issue = await self._linear.get_issue(ident)
                return issue if issue else {"error": f"no issue found for '{ident}'"}

            if name == "get_issue_history":
                ident = str(tool_input.get("identifier") or "").strip()
                if not ident:
                    return {"error": "get_issue_history requires 'identifier'"}
                return await self._linear.get_issue_history(ident)

            if name == "list_comments":
                ident = str(tool_input.get("identifier") or "").strip()
                if not ident:
                    return {"error": "list_comments requires 'identifier'"}
                return await self._linear.list_comments(ident)

            if name == "list_issues":
                order = str(tool_input.get("order") or "updatedAt")
                if order not in ("updatedAt", "priority", "dueDate"):
                    order = "updatedAt"
                try:
                    limit = int(tool_input.get("limit") or 20)
                except (TypeError, ValueError):
                    limit = 20
                return await self._linear.list_issues_query(
                    assignee_id=tool_input.get("assignee_id") or None,
                    label_names=tool_input.get("labels") or None,
                    state_types=tool_input.get("state_types") or None,
                    created_after=tool_input.get("created_after") or None,
                    created_before=tool_input.get("created_before") or None,
                    updated_after=tool_input.get("updated_after") or None,
                    due_after=tool_input.get("due_after") or None,
                    due_before=tool_input.get("due_before") or None,
                    priority=tool_input.get("priority") or None,
                    order=order,
                    limit=max(1, min(50, limit)),
                )

            if name == "resolve_member":
                nm = str(tool_input.get("name") or "").strip()
                if not nm:
                    return {"error": "resolve_member requires 'name'"}
                return await self._linear.resolve_member_id(nm)

            if name == "list_team_members":
                return await self._linear.list_team_members()

            if name == "list_projects":
                return await self._linear.list_projects()

            if name == "get_project":
                pname = str(tool_input.get("name") or "").strip()
                if not pname:
                    return {"error": "get_project requires 'name'"}
                return await self._linear.get_project(pname)

            if name == "get_project_issues":
                pname = str(tool_input.get("name") or "").strip()
                if not pname:
                    return {"error": "get_project_issues requires 'name'"}
                return await self._linear.get_project_issues(
                    pname,
                    label_names=tool_input.get("labels") or None,
                    state_types=tool_input.get("state_types") or None,
                )

            if name == "list_milestones":
                pname = str(tool_input.get("name") or "").strip()
                if not pname:
                    return {"error": "list_milestones requires 'name'"}
                return await self._linear.list_milestones(pname)

            return {"error": f"unknown tool '{name}'"}
        except Exception as e:
            log.exception("[engine] tool %s raised", name)
            return {"error": f"tool '{name}' failed: {type(e).__name__}"}
