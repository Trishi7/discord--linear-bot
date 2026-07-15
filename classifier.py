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
from datetime import date as _date
from typing import Optional

from anthropic import Anthropic

import escalation
from conventions import TEAM_CONVENTIONS
from followups import fallback_nudge
from persona import (
    CLARIFY_PROMPT,
    COMMENT_ASSESS_PROMPT,
    DEADLINE_EXTRACT_PROMPT,
    ESCALATION_PROMPT,
    FOLLOWUP_NUDGE_PROMPT,
    ISSUE_GAP_PROMPT,
    SOCIAL_REPLY_PROMPT,
    cos_preamble,
    fallback_social_reply,
)

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

You ALSO tag what KIND of message it is (`message_kind`) so the caller can route
greetings and small talk to a human reply instead of bouncing them with help text.

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
  "message_kind": "greeting" | "question" | "report" | "unclear",
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
- message_kind — what SHAPE the message is, judged independently of whether you can
  map it onto one of the four intents:
    "greeting"  — a social opener or closer with nothing to look up: "hi", "hey
                  @bot", "gm", "good morning", "how are you", "thanks", "nice one",
                  "you're a legend". Small talk. NOT a question about the team.
    "question"  — the asker WANTS something looked up: any question about people,
                  issues, standups, leave, or the team's work — INCLUDING one whose
                  phrasing you don't recognise, one you can't map to an intent, and
                  one with no question mark ("sid's tickets", "anything blocking the
                  payout work"). Tag it "question" whenever the asker plausibly wants
                  an answer — the downstream engine holds every read tool and will
                  say honestly when it finds nothing. When torn between "question"
                  and "unclear", ALWAYS pick "question".
    "report"    — a NEW bug/feature/improvement being FILED, not a question about
                  one ("the upload button is broken", "we should add dark mode").
    "unclear"   — genuinely can't tell what is wanted, and it isn't a greeting or a
                  report. This is a LAST resort — use it rarely.
- is_query=true whenever the message is a QUESTION the bot could answer from Linear
  or the standup notes — i.e. ANY person_activity, issue_status, issue_list, OR
  standup question. Set is_query=false (intent="none") ONLY for things that are not
  answerable questions at all: greetings/thanks/chit-chat, a NEW bug/feature report
  being filed, or off-topic messages. When unsure whether a question is answerable,
  prefer is_query=true and let the downstream tools decide — do NOT gate a real
  question out. is_query and message_kind are INDEPENDENT: an oddly-phrased question
  you can't map to an intent is still message_kind="question" (with is_query=false,
  intent="none") — the caller routes it to the engine on that basis, so never call a
  real question a greeting to force it out.
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
- FOLLOW-UPS: if "Recent conversation" turns are supplied above the question, use
  them ONLY to resolve a message that is elliptical on its own — one that carries
  over the previous intent while swapping the subject. E.g. after "what is Arun
  working on?", a bare "what about Ravi?" / "and Ravi?" / "him?" inherits
  intent="person_activity" with person="Ravi" (keep the previous source). Likewise
  a bare follow-up after an issue_status/issue_list question inherits that intent.
  Only borrow the INTENT/scope, never invented specifics. If the new message stands
  on its own as a full question, ignore the history and parse it directly.
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


COMMITMENT_PROMPT = """You read one Discord message from an engineering team's channel
and decide ONE thing: did this person just commit to coming back with something?

A COMMITMENT is a promise of a FUTURE deliverable from the speaker — information,
an answer, a check, a result, or a SHIP/RELEASE/DEPLOY — that someone is now implicitly
waiting on:
  "I'll confirm the DM dates in an hour"        → yes: the DM dates, in ~60 min
  "will update after testing"                    → yes: the test result, no time given
  "let me check with Ravi and get back to you"   → yes: an answer from Ravi, no time given
  "I'll push the fix by EOD"                     → yes: the fix, by end of day
  "give me 30 mins, I'll have the numbers"       → yes: the numbers, in ~30 min
  "DMs going live tomorrow"                       → yes: the DMs go-live, tomorrow
  "I'll release it this afternoon"                → yes: the release, ~today
  "I'll deploy once QA signs off"                 → yes: the deploy, no fixed time
  "will be done by Friday"                        → yes: it being done, by Friday

NOT a commitment:
- Work in progress with nothing owed back: "on it", "looking into it", "wip".
  Someone doing their job is not someone who owes the channel an update.
- Something already delivered or done: "fixed", "deployed", "shared above", "done".
- An ask OF someone else: "can you confirm the DM dates?" — that's their promise to
  make, not the speaker's.
- Hypotheticals, plans, and intentions with no deliverable: "we should test this",
  "I might look at it tomorrow", "this will need a migration".
- Pleasantries: "will do", "sure", "ok", "noted", "👍" — an acknowledgement is not
  a deliverable. If you cannot name WHAT is owed, it is not a commitment.

"what": the thing being awaited, as a short noun phrase that can be quoted straight
back to them in a reminder — "the DM dates", "the results of the payment test",
"an answer from Ravi on the refund flow". NOT a restatement of their sentence, and
never in the first person. Empty string if there's nothing concrete being awaited.

"due_minutes": how long they gave THEMSELVES, in minutes, from now:
- "in an hour" → 60; "in 30 mins" → 30; "by EOD"/"today" → 480; "tonight" → 600;
  "by tomorrow"/"tomorrow" → 1440; "this week"/"by EOW" → 4320.
- null when they named NO time at all ("will update after testing"). Do not guess.

Be conservative: when in doubt, is_commitment=false. A missed promise is cheaper
than nagging someone about a promise they never made.

Output STRICT JSON only (no preamble, no markdown fences):
{
  "is_commitment": true | false,
  "what":          "short noun phrase of what is owed — \\"\\" if none",
  "due_minutes":   integer | null,
  "confidence":    0.0-1.0
}
"""


USER_TEMPLATE = """Channel: #{channel}
Reporter: {author}
Participants: {participants}

Conversation (chronological; latest messages may clarify or contradict earlier ones):
\"\"\"
{content}
\"\"\""""


_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# ISO calendar-date matchers for the deadline path: whole-string, and anywhere-in-text.
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_ANYWHERE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def _valid_iso(s: str) -> bool:
    """True when `s` is a real YYYY-MM-DD calendar date (rejects 2026-13-40)."""
    try:
        _date.fromisoformat(str(s))
        return True
    except (TypeError, ValueError):
        return False


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
        history: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """Parse a Discord question into a structured Linear query filter.

        Returns a normalised dict (see QUERY_SYSTEM_PROMPT for the schema) or
        None on any failure. Pure parser — never touches Linear.

        `history` is the last few prior turns in this channel as
        [{"question", "answer"}] (oldest first). It's shown to the parser ONLY so
        an elliptical follow-up ("what about Ravi?") can inherit the previous
        intent/scope; the parser never speaks to the user, so no persona here.
        """
        log.info(
            "[parse_query] step 1/3: prompting model=%s requester=%r history=%d text=%r",
            self._model,
            requester,
            len(history or []),
            text[:160],
        )
        history_block = ""
        turns = [t for t in (history or []) if (t or {}).get("question")]
        if turns:
            lines: list[str] = []
            # Only the most recent few turns matter for resolving a follow-up.
            for t in turns[-4:]:
                q = str(t.get("question") or "").strip()
                a = str(t.get("answer") or "").strip()
                if q:
                    lines.append(f"- Q: {q}")
                if a:
                    # One short line of the answer is enough to carry the subject.
                    first = a.splitlines()[0].strip()
                    lines.append(f"  A: {first[:200]}")
            history_block = (
                "Recent conversation in this channel (oldest first) — use ONLY to "
                "resolve an elliptical follow-up per the FOLLOW-UPS rule:\n"
                + "\n".join(lines)
                + "\n\n"
            )
        user_prompt = (
            history_block
            + f"Requester display name: {requester or '(unknown)'}\n\n"
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

        # message_kind drives ROUTING (greeting → warm reply, question → engine).
        # An unknown/missing value defaults to "question", never "unclear": the
        # engine answers honestly when nothing applies, so guessing "question" only
        # costs a lookup, while guessing "unclear" bounces a real asker with help
        # text — the exact failure this field exists to prevent.
        message_kind = str(parsed.get("message_kind") or "").strip().lower()
        if message_kind not in {"greeting", "question", "report", "unclear"}:
            log.debug(
                "[parse_query] message_kind %r invalid; defaulting to question",
                parsed.get("message_kind"),
            )
            message_kind = "question"
        # A parse that DID recognise an answerable question is a question, whatever
        # the model tagged — the two fields can't disagree in that direction.
        if is_query and message_kind != "question":
            log.debug(
                "[parse_query] is_query=True overrides message_kind=%r → question",
                message_kind,
            )
            message_kind = "question"

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
            "message_kind": message_kind,
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

    async def social_reply(
        self,
        *,
        kind: str,
        text: str = "",
        requester: Optional[str] = None,
    ) -> str:
        """The bot's voice on the NON-answer paths: a greeting/small-talk reply
        ("greeting") or the last-resort "I couldn't make progress on that" nudge
        ("unclear"). Written by the model in the Chief-of-Staff persona so these
        paths sound like the same colleague as the answer path, never a fixed
        "here are my commands" template.

        Always returns something sendable: on any API/parse failure it falls back
        to `persona.fallback_social_reply`, which is still first-person and warm.
        Read-only — this path never touches Linear, the DB, or Discord history.
        """
        kind = (kind or "unclear").strip().lower()
        if kind not in {"greeting", "unclear"}:
            kind = "unclear"
        log.info(
            "[social_reply] kind=%s requester=%r text=%r", kind, requester, text[:120]
        )

        user_prompt = (
            f"Message kind: {kind}\n"
            f"Who is speaking to you: {requester or '(unknown)'}\n\n"
            f"Their message:\n\"\"\"\n{text}\n\"\"\"\n\n"
            "Reply to them now, in your own voice, per the rules above."
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=200,
                # Reply path → speak as the Chief-of-Staff persona (voice only).
                system=cos_preamble() + SOCIAL_REPLY_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[social_reply] Anthropic call raised; using persona fallback")
            return fallback_social_reply(kind, requester or "")

        text_blocks = [
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ]
        reply = _JSON_FENCE.sub("", "\n".join(text_blocks)).strip()
        if not reply:
            log.warning("[social_reply] empty model reply; using persona fallback")
            return fallback_social_reply(kind, requester or "")
        log.info("[social_reply] DONE kind=%s len=%d", kind, len(reply))
        return reply

    async def assess_clarification(
        self,
        *,
        thread_text: str,
        plan: dict,
        channel: str,
        reporter: str,
        max_questions: int = 3,
    ) -> Optional[dict]:
        """Decide whether this about-to-be-filed report is missing something
        essential — and if so, phrase the FEW questions (up to `max_questions`,
        bundled into one message) to ask the reporter about it.

        Returns {"needs_clarification": bool, "missing": list[str], "questions":
        list[str], "confidence": float}, or None on any API/parse failure. None means
        "don't ask": the caller proceeds to the approval embed exactly as before, so a
        failure here can never stall or lose a report.

        Read/propose only — this decides what to SAY, never what to do.
        """
        log.info(
            "[clarify] step 1/3: assessing plan kind=%s category=%s title=%r",
            plan.get("kind"),
            plan.get("category"),
            plan.get("title"),
        )
        assignee = (
            plan.get("assignee_id")
            or plan.get("assignee_display")
            or "(nobody — no owner resolved)"
        )
        user_prompt = (
            f"Channel: #{channel}\n"
            f"Reporter: {reporter}\n\n"
            "The ticket you are about to file:\n"
            f"- Category: {plan.get('category', '?')}\n"
            f"- Title: {plan.get('title', '?')}\n"
            f"- Description: {plan.get('description', '') or '(none)'}\n"
            f"- Labels: {', '.join(plan.get('label_names') or []) or '(none)'}\n"
            f"- Owner: {assignee}\n"
            f"- Needs triage (unassigned by convention): "
            f"{plan.get('kind') == 'create_needs_triage'}\n\n"
            "The full Discord thread it came from:\n"
            f"\"\"\"\n{thread_text}\n\"\"\"\n\n"
            f"Ask AT MOST {max(1, int(max_questions))} question(s), bundled into one "
            "message. Is something essential missing? Decide now, per the rules above."
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=300,
                # The `question` is spoken to the reporter → persona voice.
                system=cos_preamble() + CLARIFY_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[clarify] Anthropic call raised; proceeding without asking")
            return None

        text_blocks = [
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ]
        if not text_blocks:
            log.warning("[clarify] no text blocks in response; proceeding without asking")
            return None
        parsed = _extract_json("\n".join(text_blocks))
        if not parsed:
            log.warning("[clarify] could not extract JSON; proceeding without asking")
            return None

        log.info("[clarify] step 2/3: raw verdict %r", parsed)
        needs = bool(parsed.get("needs_clarification", False))

        # Accept the list shape ("questions"/"missing" arrays) and tolerate a model that
        # still emits the old singular "question"/"missing".
        raw_qs = parsed.get("questions")
        if raw_qs is None and parsed.get("question"):
            raw_qs = [parsed.get("question")]
        questions = [str(q).strip() for q in (raw_qs or []) if str(q).strip()]
        questions = questions[: max(1, int(max_questions))]

        raw_missing = parsed.get("missing")
        if isinstance(raw_missing, str):
            raw_missing = [raw_missing]
        allowed = {"repro", "expected_actual", "target", "scope", "owner", "none"}
        missing = [
            m for m in (str(x).strip().lower() for x in (raw_missing or []))
            if m in allowed and m != "none"
        ]

        try:
            conf = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0

        # A "yes, ask" with no question is not actionable — treat it as "don't ask"
        # rather than inventing a question or sending an empty message.
        if needs and not questions:
            log.warning("[clarify] needs_clarification=true but no questions; not asking")
            needs = False

        out = {
            "needs_clarification": needs,
            "missing": missing,
            "questions": questions,
            "confidence": max(0.0, min(1.0, conf)),
        }
        log.info(
            "[clarify] step 3/3: DONE needs=%s missing=%s conf=%.2f questions=%d",
            out["needs_clarification"],
            out["missing"],
            out["confidence"],
            len(out["questions"]),
        )
        return out

    async def detect_commitment(
        self,
        *,
        text: str,
        author: str,
        channel: str,
    ) -> Optional[dict]:
        """Is this message someone promising to come back with something — and if
        so, WHAT, and by when?

        Returns {"is_commitment": bool, "what": str, "due_minutes": int|None,
        "confidence": float} or None on any API/parse failure. `what` is phrased as
        the thing being awaited ("the DM dates", "the results of the payment test")
        so it can be quoted straight back in a nudge. `due_minutes` is how long they
        gave themselves ("in an hour" → 60); None when they named no time.

        Pure extraction — no persona (nothing here is spoken) and no side effects.
        """
        log.info(
            "[commitment] step 1/3: author=%s channel=%s text=%r",
            author,
            channel,
            (text or "")[:120],
        )
        user_prompt = (
            f"Channel: #{channel}\n"
            f"Speaker: {author}\n\n"
            f"Their message:\n\"\"\"\n{text}\n\"\"\"\n\n"
            "Is this a commitment to come back with something? Decide now."
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=250,
                system=COMMITMENT_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[commitment] Anthropic call raised; not tracking this one")
            return None

        text_blocks = [
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ]
        if not text_blocks:
            log.warning("[commitment] no text blocks in response")
            return None
        parsed = _extract_json("\n".join(text_blocks))
        if not parsed:
            log.warning("[commitment] could not extract JSON")
            return None

        log.info("[commitment] step 2/3: raw verdict %r", parsed)
        is_commitment = bool(parsed.get("is_commitment", False))
        what = str(parsed.get("what", "") or "").strip()
        try:
            conf = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        due_raw = parsed.get("due_minutes")
        try:
            due_minutes = int(due_raw) if due_raw is not None else None
        except (TypeError, ValueError):
            log.debug("[commitment] due_minutes=%r unusable; treating as unstated", due_raw)
            due_minutes = None

        # Nothing to chase without a WHAT — we'd only be able to nudge "any update
        # on... something?", which is worse than staying quiet.
        if is_commitment and not what:
            log.info("[commitment] is_commitment=true but no 'what'; not tracking")
            is_commitment = False

        out = {
            "is_commitment": is_commitment,
            "what": what,
            "due_minutes": due_minutes,
            "confidence": max(0.0, min(1.0, conf)),
        }
        log.info(
            "[commitment] step 3/3: DONE is_commitment=%s what=%r due_minutes=%s conf=%.2f",
            out["is_commitment"],
            out["what"][:80],
            out["due_minutes"],
            out["confidence"],
        )
        return out

    async def followup_nudge(
        self,
        *,
        mention: str,
        what: str,
        when: str,
        jump_url: str = "",
    ) -> str:
        """The persona-voiced reminder for an aged open thread: "@Ravi — you
        mentioned the DM dates would be confirmed about an hour ago. Any update?"

        `mention` is pasted verbatim (a Discord <@id> token when we know their ID).
        Always returns something sendable — on any failure it falls back to
        `followups.fallback_nudge`, because a nudge that silently doesn't go out is
        the one failure mode this feature cannot have.

        This writes a REMINDER to a human. It performs no action on their behalf.
        """
        log.info(
            "[nudge] composing: mention=%s what=%r when=%s", mention, what[:80], when
        )
        item = {
            "person_id": None,
            "person_name": mention,
            "what": what,
            "promised_at": "",
            "jump_url": jump_url,
        }
        user_prompt = (
            f"Who promised (paste this mention token verbatim): {mention}\n"
            f"What they promised: {what}\n"
            f"When they promised it: {when}\n"
            f"Link to their message: {jump_url or '(none)'}\n\n"
            "Write the nudge now, in your own voice, per the rules above."
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=200,
                # Reply path → speak as the Chief-of-Staff persona (voice only).
                system=cos_preamble() + FOLLOWUP_NUDGE_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[nudge] Anthropic call raised; using deterministic fallback")
            return fallback_nudge(item)

        text_blocks = [
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ]
        nudge = _JSON_FENCE.sub("", "\n".join(text_blocks)).strip()
        if not nudge:
            log.warning("[nudge] empty model reply; using deterministic fallback")
            return fallback_nudge(item)

        # The model was told to paste the mention verbatim; if it paraphrased it
        # away, the person never gets pinged — so put it back on the front.
        if mention.startswith("<@") and mention not in nudge:
            log.info("[nudge] model dropped the mention token; prepending it")
            nudge = f"{mention} — {nudge}"

        log.info("[nudge] DONE len=%d", len(nudge))
        return nudge

    async def assess_issue_gap(self, *, issue: dict) -> Optional[dict]:
        """The MODEL-JUDGED half of the escalation ladder: is this launch-critical issue
        missing a repro, or too vague to build?

        (The other half — no due date, stuck In Progress, unassigned — is deterministic
        and needs no model; see `escalation.find_gaps`.)

        Returns {"gap": "missing_repro"|"vague_scope"|"none", "detail": str,
        "confidence": float}, or None on any API/parse failure — which the caller treats
        as "no gap", so a model outage means silence, never a bad nudge.
        """
        ident = issue.get("identifier") or "?"
        log.info("[issue_gap] step 1/3: assessing %s", ident)
        desc = (issue.get("description") or "").strip()
        user_prompt = (
            f"Issue: {ident}\n"
            f"Title: {issue.get('title') or '(none)'}\n"
            f"State: {issue.get('state') or '?'}\n"
            f"Labels: {', '.join(issue.get('labels') or []) or '(none)'}\n"
            f"Assignee: {issue.get('assignee') or '(unassigned)'}\n"
            f"Project: {issue.get('project') or '(none)'}\n\n"
            "Description:\n"
            f"\"\"\"\n{desc or '(empty)'}\n\"\"\"\n\n"
            "Is this missing something that blocks the work? Decide now."
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=250,
                system=cos_preamble() + ISSUE_GAP_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[issue_gap] Anthropic call raised; treating %s as no-gap", ident)
            return None

        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        if not text_blocks:
            log.warning("[issue_gap] no text blocks for %s", ident)
            return None
        parsed = _extract_json("\n".join(text_blocks))
        if not parsed:
            log.warning("[issue_gap] could not extract JSON for %s", ident)
            return None

        gap = str(parsed.get("gap", "none") or "none").strip().lower()
        if gap not in {"missing_repro", "vague_scope", "none"}:
            log.debug("[issue_gap] unknown gap %r for %s; normalising to none", gap, ident)
            gap = "none"
        try:
            conf = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        detail = str(parsed.get("detail", "") or "").strip()

        # A gap we can't describe isn't one we can ask about.
        if gap != "none" and not detail:
            log.info("[issue_gap] %s: gap=%s but no detail; treating as none", ident, gap)
            gap = "none"

        out = {"gap": gap, "detail": detail, "confidence": max(0.0, min(1.0, conf))}
        log.info(
            "[issue_gap] step 3/3: DONE %s gap=%s conf=%.2f detail=%r",
            ident, out["gap"], out["confidence"], out["detail"][:80],
        )
        return out

    async def assess_issue_comments(
        self, *, issue: dict, comments: list[dict], gap_kind: str, today: str
    ) -> Optional[dict]:
        """CHECK-BEFORE-TAGGING (point 2): read an issue's comments before pinging its
        assignee about `gap_kind`, and decide whether the comments already resolve the gap,
        reveal a blocker that belongs with the PMs, or neither.

        Returns {"resolves_gap": bool, "found_deadline": "YYYY-MM-DD"|"", "blocker": bool,
        "escalate_to_pm": bool, "reason": str, "evidence": str, "confidence": float}, or
        None on any API/parse failure — which the caller treats as "no signal, go ahead and
        tag" (a model outage must not silently swallow a nudge). Read-only.
        """
        ident = issue.get("identifier") or "?"
        log.info(
            "[comment_check] step 1/2: %s gap=%s comments=%d", ident, gap_kind, len(comments or [])
        )
        if not comments:
            # Nothing to read — no comment can resolve the gap or show a blocker.
            return {
                "resolves_gap": False, "found_deadline": "", "blocker": False,
                "escalate_to_pm": False, "reason": "", "evidence": "", "confidence": 1.0,
            }
        rendered = "\n".join(
            f"- [{c.get('createdAt') or '?'}] {c.get('author') or 'someone'}: "
            f"{(c.get('body') or '').strip()}"
            for c in comments
        )[:4000]
        user_prompt = (
            f"Today: {today}\n"
            f"The gap you were about to ask about: {gap_kind}\n"
            f"Issue: {ident} ({issue.get('title') or ''})\n"
            f"State: {issue.get('state') or '?'}\n"
            f"Assignee: {issue.get('assignee') or '(unassigned)'}\n\n"
            "Comments (oldest first):\n"
            f"{rendered}\n\n"
            "Decide the outcome now."
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=350,
                system=cos_preamble() + COMMENT_ASSESS_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[comment_check] Anthropic call raised for %s; treating as no-signal", ident)
            return None

        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        parsed = _extract_json("\n".join(text_blocks)) if text_blocks else None
        if not parsed:
            log.warning("[comment_check] could not parse verdict for %s", ident)
            return None

        def _b(key):
            return bool(parsed.get(key))

        deadline = str(parsed.get("found_deadline", "") or "").strip()
        if not _DATE_ONLY_RE.match(deadline):
            deadline = ""
        try:
            conf = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        out = {
            "resolves_gap": _b("resolves_gap") and bool(deadline) if gap_kind == escalation.MISSING_DUE_DATE else _b("resolves_gap"),
            "found_deadline": deadline,
            "blocker": _b("blocker"),
            "escalate_to_pm": _b("blocker") and _b("escalate_to_pm"),
            "reason": str(parsed.get("reason", "") or "").strip(),
            "evidence": str(parsed.get("evidence", "") or "").strip(),
            "confidence": max(0.0, min(1.0, conf)),
        }
        log.info(
            "[comment_check] step 2/2: DONE %s resolves=%s deadline=%r blocker=%s pm=%s conf=%.2f",
            ident, out["resolves_gap"], out["found_deadline"], out["blocker"],
            out["escalate_to_pm"], out["confidence"],
        )
        return out

    async def extract_deadline(self, *, text: str, today: str) -> Optional[dict]:
        """Extract a concrete deadline (resolved to an absolute YYYY-MM-DD) from a free-text
        `text` — a Discord reply, an issue comment, or a standup line — relative to `today`.

        Returns {"date": "YYYY-MM-DD"|"", "quote": str, "confidence": float}, or None on
        API/parse failure. An empty date means "no actionable date found" (vague/unrelated).
        Read-only.
        """
        body = (text or "").strip()
        if not body:
            return {"date": "", "quote": "", "confidence": 1.0}
        # Fast path: an explicit ISO date in the text needs no model call.
        iso = _DATE_ANYWHERE_RE.search(body)
        if iso and _valid_iso(iso.group(0)):
            return {"date": iso.group(0), "quote": iso.group(0), "confidence": 0.95}
        user_prompt = f"Today: {today}\n\nText:\n\"\"\"\n{body[:1500]}\n\"\"\"\n\nExtract the deadline now."
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=150,
                system=cos_preamble() + DEADLINE_EXTRACT_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[deadline] Anthropic call raised; no date")
            return None
        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        parsed = _extract_json("\n".join(text_blocks)) if text_blocks else None
        if not parsed:
            return None
        date_str = str(parsed.get("date", "") or "").strip()
        if not (_DATE_ONLY_RE.match(date_str) and _valid_iso(date_str)):
            date_str = ""
        try:
            conf = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        return {
            "date": date_str,
            "quote": str(parsed.get("quote", "") or "").strip(),
            "confidence": max(0.0, min(1.0, conf)),
        }

    async def compose_escalation(
        self,
        *,
        level: str,
        mentions: str,
        finding: dict,
    ) -> str:
        """The persona-voiced nudge for ONE finding — level "assignee" (ask the owner
        for the missing piece) or "pm" (state the situation, hand the PMs the call).

        `mentions` is the already-resolved audience string ("<@1> <@2>", or a plain name
        when someone isn't mapped to a Discord id) — the classifier never resolves people
        and never decides who to tag.

        Always returns something sendable: on any failure it falls back to
        `escalation.fallback_message`. This composes a MESSAGE ASKING A HUMAN — it is
        never a Linear write, and the prompt forbids claiming otherwise.
        """
        level = (level or escalation.LEVEL_ASSIGNEE).strip().lower()
        if level not in {escalation.LEVEL_ASSIGNEE, escalation.LEVEL_PM}:
            level = escalation.LEVEL_ASSIGNEE
        ident = finding.get("issue_id") or "?"
        log.info("[escalate] composing level=%s for %s (%s)", level, ident, finding.get("kind"))

        launch = escalation.describe_launch(finding.get("launch"))
        user_prompt = (
            f"Level: {level}\n"
            f"Who to address (paste these tokens verbatim): {mentions}\n"
            f"Issue: {ident} ({finding.get('issue_title') or ''})\n"
            f"State: {finding.get('issue_state') or '?'}\n"
            f"Assignee: {finding.get('assignee') or '(unassigned)'}\n"
            f"The situation: this issue {finding.get('detail') or 'needs attention'}\n"
            f"Launch pressure: {launch or '(none — do not invent one)'}\n"
            f"Link: {finding.get('issue_url') or '(none)'}\n\n"
            "Write the message now, in your own voice, per the rules above."
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=250,
                system=cos_preamble() + ESCALATION_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception:
            log.exception("[escalate] Anthropic call raised; using deterministic fallback")
            return escalation.fallback_message(finding, mentions)

        text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        msg = _JSON_FENCE.sub("", "\n".join(text_blocks)).strip()
        if not msg:
            log.warning("[escalate] empty model reply; using deterministic fallback")
            return escalation.fallback_message(finding, mentions)

        # The model was told to paste the mention tokens verbatim. If it dropped one,
        # the person it was FOR never sees it — so put the audience back on the front.
        missing = [t for t in mentions.split() if t.startswith("<@") and t not in msg]
        if missing:
            log.info("[escalate] model dropped mention token(s) %s; prepending", missing)
            msg = f"{' '.join(missing)} {msg}"

        log.info("[escalate] DONE level=%s %s len=%d", level, ident, len(msg))
        return msg

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
                # Reply path → speak as the Chief-of-Staff persona (voice only).
                system=cos_preamble() + PERSON_ACTIVITY_SYNTHESIS_PROMPT,
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
