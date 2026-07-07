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
            "url, and the latest comment. Prefer this whenever the user gives an explicit key."
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
            "due-date, estimate transitions) as timestamped events with the actor. This is "
            "the ONLY way to answer 'when did the status change' / 'what changed'. Returns a "
            "list of {at, actor, changes[]}, oldest first."
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
]


def _system_prompt(*, requester_name: str, requester_linear_id: Optional[str], today: str) -> str:
    who = f"The person asking is **{requester_name}**"
    if requester_linear_id:
        who += f" (their Linear user id is {requester_linear_id})"
    who += (
        '. When they say "me", "my", "mine", or "I", they mean themselves — use that '
        "identity/id directly instead of resolving a name."
    )
    return f"""You answer questions about the NFThing / NFThing2.0 team's Linear
issues, posted in Discord. NFThing2.0 is the team's product; issues use keys like
NFT2-123. Today's date is {today} (UTC).

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
- For "sorted by priority / due date", pass the matching 'order' to list_issues.

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

Rules:
- NEVER invent issues, identifiers, links, statuses, dates, or fields. Use only
  what the tools return. If a field isn't present, say it isn't set.
- If nothing matches, say so plainly and briefly — don't pad.
- Keep replies concise and Discord-friendly (GitHub-flavoured markdown). Link
  issues as [NFT2-123](url) using the url from the tool result. For lists, one
  bullet per issue. Stay well under 1500 characters.
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
    ) -> Optional[str]:
        """Answer `question` by looping model ⇄ tools. Returns the final reply
        text, or None on total failure (the caller can fall back to a nudge).

        `extra_tools` are per-call, caller-supplied read-only tools on top of the
        built-in Linear set — each is {"schema": <tool def>, "handler": async
        fn(input_dict) -> jsonable}. bot.py uses this to inject the Discord↔Linear
        linking tools (which need the DB + Discord client) without coupling the
        engine to Discord."""
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
        messages: list[dict] = [{"role": "user", "content": question}]
        log.info(
            "[engine] start question=%r requester=%r linear_id=%s",
            question[:160], requester_name, requester_linear_id,
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

            return {"error": f"unknown tool '{name}'"}
        except Exception as e:
            log.exception("[engine] tool %s raised", name)
            return {"error": f"tool '{name}' failed: {type(e).__name__}"}
