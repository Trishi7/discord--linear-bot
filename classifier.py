"""LLM-based message classifier.

Given a Discord message, returns either None (no verdict / API error) or a dict:
  {
    "category":            "bug" | "feature" | "improvement" | "noise",
    "needs_triage":        bool,
    "is_new_issue":        bool,
    "status_signal":       "none" | "resolved" | "in_progress" | "cannot_reproduce",
    "area_labels":         list[str]  (subset of ["BE","FE","UI"]),
    "mentioned_assignees": list[str]  (Discord display names, in mention order),
    "title":               "<=80 chars",
    "description":         "...",
    "priority":            "low" | "medium" | "high" | "urgent",
    "confidence":          0.0–1.0,
  }

The caller decides what to do with low-confidence, noise, or needs_triage results.
"""
import asyncio
import json
import logging
import re
from typing import Optional

from anthropic import Anthropic

from conventions import TEAM_CONVENTIONS

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You triage incoming Discord conversations for the NFThing / membrane
engineering team into Linear.

Input may be a single message or a short thread — the report plus any
ancestors (the message being replied to) and follow-ups (clarifications,
corrections, frontend-vs-backend pinpointing, "nvm, my mistake", etc.).
Treat the whole thread as one report. Later messages may override earlier
ones — weigh the latest clarifications.

Reporters often attach screenshots or recordings. Image attachments are
inlined for you to inspect; videos and other files appear by filename and
URL only — use them as evidence when judging the issue and writing the
description.

Output STRICT JSON only (no preamble, no markdown fences) with this schema:
{
  "category":            "bug" | "feature" | "improvement" | "noise",
  "needs_triage":        true | false,
  "is_new_issue":        true | false,
  "status_signal":       "none" | "resolved" | "in_progress" | "cannot_reproduce",
  "area_labels":         array, subset of ["BE","FE","UI"],
  "mentioned_assignees": array of strings (Discord display names, in the order they are mentioned),
  "title":               "short imperative title, max 80 chars",
  "description":         "1-3 sentence summary. For bugs, include reproduction steps and expected vs actual if mentioned. Restate clearly; don't invent details.",
  "priority":            "low" | "medium" | "high" | "urgent",
  "confidence":          0.0-1.0
}

Category definitions (see TEAM CONVENTIONS above for the authoritative version):
- "feature":     a NEW screen/flow/capability that does not exist yet — needs new endpoints or a design sprint; driven by a business/PRD/advertiser decision.
- "improvement": a change to something that ALREADY exists — uses existing APIs or minor additions; triggered by analytics, user feedback, or a bug.
- "bug":         a defect — broken or not behaving as expected.
- "noise":      chit-chat, acks, questions, plain status updates — nothing to do.
Decision test: if it needs a NEW Linear Project it's a feature; if it fits as a change to existing work it's an improvement.

needs_triage:
- If a message is plausibly actionable but you're unsure which category fits, pick your best-guess category AND set needs_triage=true.
- Low confidence biases toward needs_triage=true, NOT toward "noise".
- Only use "noise" when there is genuinely nothing to do.

is_new_issue:
- For a reply that is about the SAME thing as its parent (a clarification, "it's fixed", a repro detail) → false.
- Only true if the reply raises a SEPARATE new item.
- Non-replies → always true.

status_signal (set to non-"none" ONLY when clearly stated in the thread):
- "resolved":         "fixed", "deployed", "done", "shipped".
- "in_progress":      "on it", "WIP", "looking into it".
- "cannot_reproduce": "can't reproduce", "not a bug", "works for me".
- Otherwise → "none".

area_labels (the system label showing WHERE a fix lives):
- "BE" = logic/API error; "FE" = wrong frontend behaviour or cosmetic (spacing/colour/font/layout); "UI" = a state never designed / edge case missing from design.
- For a BUG, ALWAYS include the single best system label (BE/FE/UI). If you genuinely can't tell, pick your best guess AND set needs_triage=true.
- For non-bugs, include a system label only when the message clearly points there; otherwise [].

mentioned_assignees:
- Discord display names or @-handles called out as owners/targets, preserved in mention order. [] if none.

Priority guidance (severity → Linear priority):
- "urgent" (P0 Blocking): crash, user can't complete a flow, data loss, payment/withdrawal failure.
- "high"   (P1 Major):    core flow broken but a workaround exists.
- "low"    (P2 Minor):    visual glitch, copy error, non-blocking edge case.
- "medium": use when it's clearly actionable but you're unsure whether it's P1 or P2.

Confidence guidance:
- Be honest. Short or ambiguous messages get low confidence even if you can pattern-match a category.
- Low confidence does NOT mean "noise" — pair low confidence with needs_triage=true.
"""


QUERY_SYSTEM_PROMPT = """You parse Discord questions for the NFThing / membrane
engineering team into structured filters. You ONLY parse — you never fetch,
create, or modify anything.

There are FOUR kinds of question:
- person_activity: "what is <person> working on / up to / handling these days",
  "what's <person> been doing" — a status check on ONE teammate. This blends the
  person's Linear assignments with their recent Discord activity.
- issue_status: a status check on ONE specific ISSUE — "what's the status of X",
  "where are we on X", "update on the X issue", "any progress on the X". Set
  `subject` to the free-text description of that issue (e.g. "DMs", "birthday
  feature", "payout bug"). If the asker names an explicit issue KEY (e.g.
  "NFT-123", "status of NFT2-45"), set `identifier` to that key and leave
  `subject`="".
- issue_list: anything about the team's Linear ISSUES as a GROUP — lists, filters,
  searches. E.g. "list my open bugs", "issues Sid raised in the last 2 weeks",
  "what's open with the Bug label".
- standup: anything about the team's STANDUP / SYNC notes — "what was discussed in
  today's / yesterday's standup", "what happened in the AM/PM sync", "what did we
  decide this morning", "standup summary", "action items from standup", "what did
  <person> commit to today". A status check on a MEETING NOTE, not on Linear.
  When the question also names a person ("what did Ravi commit to today"), it is
  STILL a standup question (intent="standup"), not person_activity.

Output STRICT JSON only (no preamble, no fences) with this schema:
{
  "is_query":     true | false,
  "intent":       "person_activity" | "issue_status" | "issue_list" | "standup" | "none",
  "source":       "discord" | "linear" | "both",  // which system to look at; "both" if unscoped
  "person":       string,            // person_activity: the teammate's name; "" otherwise
  "subject":      string,            // issue_status: free-text description of the ONE issue; "" otherwise
  "identifier":   string,            // issue_status/issue_list: e.g. "NFT-123"; "" if not referenced
  "detail":       "none" | "more" | "description",  // issue_status: how much of the issue to show (see rules)
  "search_term":  string,            // issue_list: free-text keywords for fuzzy title search; "" if n/a
  "reporter":     string,            // issue_list: "me" if the asker references themselves; a name otherwise; "" if none
  "labels":       array of strings,  // category filter — subset of ["Bug","Feature","Improvement","BE","FE","UI"]; [] if not specified
  "states":       array of strings,  // subset of ["open","closed","in_progress","done","cancelled"]; [] = any state
  "window_days":  integer            // 0 = no time window
}

Rules:
- is_query=true whenever the message is a QUESTION the bot could answer from Linear
  or the standup notes — i.e. ANY person_activity, issue_status, issue_list, OR
  standup question. Set is_query=false (intent="none") ONLY for things that are not
  answerable questions at all: greetings/thanks/chit-chat, a NEW bug/feature report
  being filed, or off-topic messages. When unsure whether a question is answerable,
  prefer is_query=true and let the downstream tools decide — do NOT gate a real
  question out.
- intent="person_activity" when the question asks what a specific PERSON is working
  on / up to / handling / has been doing / mentioned / said. Set `person` to that
  name (preserve casing); leave issue-filter fields (identifier/search_term/
  reporter) at "". "what am I working on" / "what's on my plate" → person="me".
- intent="issue_status" when the question asks for the state/progress/update of
  ONE specific issue ("what's the status of the DMs issue", "where are we on the
  payout bug", "update on NFT-123"). If an explicit key is present set
  `identifier` and leave `subject`=""; otherwise set `subject` to the issue's
  free-text description and leave `identifier`="". Leave `person`="".
- detail (issue_status only; "none" for every other intent):
    "description" — the asker explicitly wants the issue's DESCRIPTION / body
                    text: "description of NFT-123", "what does NFT-123 say",
                    "read me NFT-123", "what's written in the DMs issue".
    "more"        — a general request for more than the one-line status:
                    "tell me more about NFT-123", "details of NFT-123", "give me
                    the full picture on the payout bug".
    "none"        — a plain status check ("status of NFT-123", "where are we on
                    the DMs issue"). Default when unsure.
- intent="issue_list" for questions about issues as a GROUP (lists, filters,
  searches across many issues). Use reporter/labels/states/window_days/
  search_term as applicable. Leave `person`="" and `subject`="".
- intent="standup" for questions about the STANDUP / SYNC notes (see the standup
  kind above). Key words: "standup", "stand-up", "sync", "kick-off", "wrap-up",
  "AM sync", "PM sync", "this morning's meeting", "what did we decide/discuss".
  If a person is named, still use intent="standup" and set `person` to that name so
  the downstream reader can filter the action items to them. window_days: "today"=1,
  "yesterday"=1 (the reader resolves the exact date from wording).
- source: which system the question is scoped to. Infer from wording:
    "on discord", "in the channel", "posted", "mentioned" / "said" / "wrote"  → "discord"
    "in linear", "assigned to", "ticket", "issue", "status of", "label"        → "linear"
    "standup", "sync", "kick-off", "wrap-up" (i.e. intent="standup")           → "both"
    no explicit source                                                         → "both"
  When BOTH a Discord verb and a Linear noun appear, prefer the explicit scope the
  asker is standing in (e.g. "what bugs did X mention on discord" → "discord").
  A standup question is NEVER "discord" — always use "both" so it reaches the reader.
- "I" / "my" / "me" → reporter="me" (issue_list). A named person → reporter="<Name>".
- Time phrases → window_days: "today"=1, "this week"=7, "last 2 weeks"=14,
  "this month"=30, no time reference → 0.
- State phrases → states (may be several):
    "open" / "still open" / "not done"               → ["open"]
    "closed" / "done" / "finished" / "completed"     → ["done"]
    "cancelled" / "wontfix"                          → ["cancelled"]
    "in progress" / "WIP" / "being worked on"        → ["in_progress"]
    none mentioned                                   → []
- labels: a CATEGORY filter, valid for BOTH intents. Only "Bug" / "Feature" /
  "Improvement" / "BE" / "FE" / "UI" are valid. Map "bug"/"bugs"→"Bug",
  "feature(s)"→"Feature", "frontend"→"FE", "backend"→"BE", "ui"/"UX"→"UI". Drop
  anything else. E.g. "what BUGS did Harsh mention" → person_activity, person=
  "Harsh", labels=["Bug"] so the answer narrows to bug reports.
- search_term: any free-text keywords from the question not covered by other
  fields. "" if not applicable.
- Don't invent details. Default fields rather than guess.
"""


PERSON_ACTIVITY_SYNTHESIS_PROMPT = """You write a SHORT status summary of what one
teammate is working on, for the NFThing / membrane engineering team, as a Discord
reply.

The data was ALREADY gathered for you. The payload's `source` field tells you WHICH
sources were consulted — and you must include ONLY the section(s) for those sources:
  - "discord" → ONLY the Discord section. Do NOT add a Linear section at all.
  - "linear"  → ONLY the Linear section. Do NOT add a Discord section at all.
  - "both"    → both sections.
You ONLY summarise what is in the data. Never fetch anything, and never invent
issues, identifiers, links, statuses, or activity.

If a `category` is present (e.g. "Bug"), narrow the whole answer to that category:
mention only matching issues / messages, and reflect it in the header (e.g.
"**<Person> — recent bug reports**").

Write COMPACT, Discord-friendly GitHub-flavoured markdown — a chat reply, not a
report. NO markdown headers (no #, ##, ###). Use short **bold labels**. Keep the
whole thing under ~1800 characters and prefer ONE message. First line is a short
bold label, e.g. **<Person> — working on** (adjust wording to source/category).

Linear section (ONLY when source is "linear" or "both"):
**Linear:**
- ONE LINE PER ISSUE, no blank lines, in this shape:
  [IDENTIFIER](url) — <short title> · <priority> · <status>
  Omit a field that isn't set rather than writing "none".
- If there are no Linear issues, write exactly: _nothing in Linear_

Discord section (ONLY when source is "discord" or "both"):
**Discord (last N days):**
- A 1–2 sentence natural-language summary of what they've been posting (deploys,
  blockers, questions, PRs, decisions). Weave in jump links to the 1–3 most
  relevant messages inline, e.g. "shipped the payout fix ([msg](<jump_url>))".
- Do NOT dump raw message logs. Summarise.
- If there are no Discord messages, write exactly: _nothing in Discord_

Hard rules:
- NEVER show a section for a source that is not in `source`. A Discord-only answer
  must contain no Linear block whatsoever, and vice versa.
- NO blank lines between items or sections — a single line break is enough.
- Use ONLY identifiers, titles, statuses, and URLs present in the Linear data.
  Never fabricate a link or an issue.
- Use ONLY jump_url values present in the Discord data. Never invent a link.
- If there are many Linear issues, lead with the most relevant (highest priority
  / most recently updated), list at most ~12, and end with "…and N more".
- If a coverage note is provided in the data, add it as a short final italic line.
- Output the reply text only — no preamble, no code fences.
"""


USER_TEMPLATE = """Channel: #{channel}
Reporter: {author}
Participants: {participants}

Conversation (chronological; latest messages may clarify or contradict earlier ones):
\"\"\"
{content}
\"\"\""""


_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort: strip code fences and parse. Returns None on failure."""
    cleaned = _JSON_FENCE.sub("", text).strip()
    # If the model wrapped JSON in prose, grab the first {...} block.
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("Classifier returned non-JSON: %s; raw=%r", e, text[:200])
        return None


class Classifier:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    async def classify(
        self,
        *,
        content: str,
        author: str,
        channel: str,
        image_urls: Optional[list[str]] = None,
        participants: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """Classify a thread. Returns the verdict dict or None on any failure."""
        log.info(
            "[classify] step 1/5: building prompt — channel=%s author=%s content_len=%d images=%d participants=%d",
            channel,
            author,
            len(content),
            len(image_urls or []),
            len(participants or []),
        )
        prompt = USER_TEMPLATE.format(
            channel=channel,
            author=author,
            content=content,
            participants=", ".join(participants) if participants else author,
        )

        user_content: list[dict] = []
        for url in image_urls or []:
            user_content.append(
                {"type": "image", "source": {"type": "url", "url": url}}
            )
        user_content.append({"type": "text", "text": prompt})

        log.info(
            "[classify] step 2/5: calling Anthropic model=%s blocks=%d",
            self._model,
            len(user_content),
        )
        try:
            # Anthropic SDK is sync; offload to a thread to avoid blocking the
            # event loop while the model thinks.
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=600,
                system=TEAM_CONVENTIONS + "\n\n" + SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception:
            log.exception("[classify] step 2/5 FAILED: Anthropic API call raised")
            return None
        log.info(
            "[classify] step 3/5: got response stop_reason=%s usage=%s",
            getattr(resp, "stop_reason", None),
            getattr(resp, "usage", None),
        )

        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        if not text_blocks:
            log.warning("[classify] step 3/5 FAILED: no text blocks in response")
            return None
        log.debug("[classify] raw text blocks: %r", text_blocks)

        log.info("[classify] step 4/5: parsing JSON from model output")
        verdict = _extract_json("\n".join(text_blocks))
        if not verdict:
            log.warning("[classify] step 4/5 FAILED: could not extract JSON")
            return None

        # Normalise and validate
        log.info("[classify] step 5/5: normalising verdict %r", verdict)
        cat = verdict.get("category")
        if cat not in {"bug", "feature", "improvement", "noise"}:
            log.warning("Classifier returned unknown category %r", cat)
            return None
        pri = verdict.get("priority", "medium")
        if pri not in {"low", "medium", "high", "urgent"}:
            log.debug("[classify] priority %r invalid; defaulting to medium", pri)
            pri = "medium"
        try:
            conf = float(verdict.get("confidence", 0))
        except (TypeError, ValueError):
            log.debug("[classify] confidence %r not a float; using 0.0", verdict.get("confidence"))
            conf = 0.0

        needs_triage = bool(verdict.get("needs_triage", False))
        # Default true: a missing field most safely means "treat as a fresh report".
        is_new_issue = bool(verdict.get("is_new_issue", True))

        status_signal = verdict.get("status_signal", "none")
        if status_signal not in {"none", "resolved", "in_progress", "cannot_reproduce"}:
            log.debug("[classify] status_signal %r invalid; defaulting to none", status_signal)
            status_signal = "none"

        raw_areas = verdict.get("area_labels") or []
        if not isinstance(raw_areas, list):
            log.debug("[classify] area_labels %r is not a list; defaulting to []", raw_areas)
            raw_areas = []
        allowed_areas = {"BE", "FE", "UI"}
        # Preserve order, drop unknowns and dupes.
        area_labels: list[str] = []
        for a in raw_areas:
            if isinstance(a, str) and a in allowed_areas and a not in area_labels:
                area_labels.append(a)

        raw_mentioned = verdict.get("mentioned_assignees") or []
        if not isinstance(raw_mentioned, list):
            log.debug(
                "[classify] mentioned_assignees %r is not a list; defaulting to []",
                raw_mentioned,
            )
            raw_mentioned = []
        mentioned_assignees = [
            str(m).strip() for m in raw_mentioned if str(m).strip()
        ]

        normalised = {
            "category": cat,
            "needs_triage": needs_triage,
            "is_new_issue": is_new_issue,
            "status_signal": status_signal,
            "area_labels": area_labels,
            "mentioned_assignees": mentioned_assignees,
            "title": str(verdict.get("title", "")).strip()[:80] or "(untitled)",
            "description": str(verdict.get("description", "")).strip(),
            "priority": pri,
            "confidence": max(0.0, min(1.0, conf)),
        }
        log.info(
            "[classify] DONE: category=%s needs_triage=%s is_new_issue=%s "
            "status_signal=%s areas=%s mentions=%d priority=%s confidence=%.2f title=%r",
            normalised["category"],
            normalised["needs_triage"],
            normalised["is_new_issue"],
            normalised["status_signal"],
            normalised["area_labels"],
            len(normalised["mentioned_assignees"]),
            normalised["priority"],
            normalised["confidence"],
            normalised["title"],
        )
        return normalised

    async def parse_query(
        self,
        *,
        text: str,
        requester: Optional[str] = None,
    ) -> Optional[dict]:
        """Parse a Discord question into a structured Linear query filter.

        Returns a normalised dict (see QUERY_SYSTEM_PROMPT for the schema) or
        None on any failure. Pure parser — never touches Linear.
        """
        log.info(
            "[parse_query] step 1/3: prompting model=%s requester=%r text=%r",
            self._model,
            requester,
            text[:160],
        )
        user_prompt = (
            f"Requester display name: {requester or '(unknown)'}\n\n"
            f"Question:\n\"\"\"\n{text}\n\"\"\""
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=400,
                system=QUERY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[parse_query] Anthropic call raised")
            return None
        log.info(
            "[parse_query] step 2/3: response stop_reason=%s usage=%s",
            getattr(resp, "stop_reason", None),
            getattr(resp, "usage", None),
        )

        text_blocks = [
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ]
        if not text_blocks:
            log.warning("[parse_query] no text blocks in response")
            return None

        log.info("[parse_query] step 3/3: parsing JSON")
        parsed = _extract_json("\n".join(text_blocks))
        if not parsed:
            log.warning("[parse_query] could not extract JSON")
            return None

        is_query = bool(parsed.get("is_query", False))
        intent = parsed.get("intent", "none")
        if intent not in {"person_activity", "issue_status", "issue_list", "standup", "none"}:
            intent = "none"

        source = str(parsed.get("source") or "both").strip().lower()
        if source not in {"discord", "linear", "both"}:
            source = "both"

        person = str(parsed.get("person") or "").strip()
        subject = str(parsed.get("subject") or "").strip()
        identifier = str(parsed.get("identifier") or "").strip()
        search_term = str(parsed.get("search_term") or "").strip()
        reporter = str(parsed.get("reporter") or "").strip()

        detail = str(parsed.get("detail") or "none").strip().lower()
        if detail not in {"none", "more", "description"}:
            detail = "none"

        raw_labels = parsed.get("labels") or []
        if not isinstance(raw_labels, list):
            raw_labels = []
        allowed = {"Bug", "Feature", "Improvement", "BE", "FE", "UI"}
        labels: list[str] = []
        for lab in raw_labels:
            if isinstance(lab, str) and lab in allowed and lab not in labels:
                labels.append(lab)

        raw_states = parsed.get("states") or []
        if not isinstance(raw_states, list):
            raw_states = []
        allowed_states = {"open", "closed", "in_progress", "done", "cancelled"}
        states: list[str] = []
        for s in raw_states:
            if isinstance(s, str) and s in allowed_states and s not in states:
                states.append(s)

        try:
            window_days = int(parsed.get("window_days", 0))
        except (TypeError, ValueError):
            window_days = 0
        window_days = max(0, window_days)

        normalised = {
            "is_query": is_query,
            "intent": intent,
            "source": source,
            "person": person,
            "subject": subject,
            "identifier": identifier,
            "detail": detail,
            "search_term": search_term,
            "reporter": reporter,
            "labels": labels,
            "states": states,
            "window_days": window_days,
        }
        log.info("[parse_query] DONE: %s", normalised)
        return normalised

    async def summarize_person_activity(
        self,
        *,
        person: str,
        window_days: int,
        linear_issues: list[dict],
        discord_messages: list[dict],
        source: str = "both",
        category: str = "",
        coverage_note: str = "",
    ) -> Optional[str]:
        """Synthesise ONE concise reply describing what `person` is working on,
        from the data already gathered. `source` ("discord"|"linear"|"both")
        controls which section(s) the reply includes; `category` (e.g. "Bug")
        narrows it. Read-only — summarises only; never fetches or invents.
        Returns the reply text, or None on any API/parse failure so the caller
        can fall back to a deterministic render.
        """
        source = (source or "both").strip().lower()
        if source not in {"discord", "linear", "both"}:
            source = "both"
        log.info(
            "[summarize] step 1/3: person=%r window=%d source=%s category=%r linear=%d discord=%d",
            person,
            window_days,
            source,
            category,
            len(linear_issues or []),
            len(discord_messages or []),
        )

        # Compact, bounded payload so the model can't be flooded with raw logs.
        slim_issues = [
            {
                "identifier": i.get("identifier"),
                "title": i.get("title"),
                "status": i.get("state_name") or i.get("state"),
                "url": i.get("url"),
                "updatedAt": i.get("updatedAt") or i.get("updated_at"),
                "priority": i.get("priority"),
            }
            for i in (linear_issues or [])[:25]
        ]
        slim_msgs = []
        for m in (discord_messages or [])[:30]:
            text = (m.get("text") or "").strip()
            if len(text) > 300:
                text = text[:297] + "…"
            ts = m.get("timestamp")
            slim_msgs.append(
                {
                    "channel": m.get("channel"),
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "text": text,
                    "jump_url": m.get("jump_url"),
                    "attachments": len(m.get("attachment_urls") or []),
                }
            )

        payload = {
            "person": person,
            "window_days": window_days,
            "source": source,
            "category": category or None,
            # Only surface the data for the sources actually in scope, so the
            # model can't be tempted to render a section it shouldn't.
            "linear_issues": slim_issues if source in ("linear", "both") else [],
            "discord_messages": slim_msgs if source in ("discord", "both") else [],
            "coverage_note": coverage_note,
        }
        user_prompt = (
            "Summarise this teammate's current work from the data below. Remember: "
            "summarise only, invent nothing, and use only the links present here.\n\n"
            f"```json\n{json.dumps(payload, default=str, ensure_ascii=False)}\n```"
        )

        log.info("[summarize] step 2/3: calling Anthropic model=%s", self._model)
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=700,
                system=PERSON_ACTIVITY_SYNTHESIS_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[summarize] Anthropic call raised; returning None")
            return None

        text_blocks = [
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ]
        if not text_blocks:
            log.warning("[summarize] no text blocks in response; returning None")
            return None

        reply = "\n".join(text_blocks).strip()
        # Strip an accidental wrapping code fence if the model added one.
        reply = _JSON_FENCE.sub("", reply).strip()
        log.info("[summarize] step 3/3: DONE len=%d", len(reply))
        return reply or None
