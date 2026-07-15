"""The two-level escalation ladder — who to ask, and when.

A Chief of Staff doesn't just answer questions; she chases the things that will hurt
if nobody chases them. This module decides WHAT is worth chasing on the team's open
Linear issues, and — the important part — WHO to ask:

  LEVEL 1 → THE ASSIGNEE, for a DATA GAP on their own issue. They can just answer it:
            "@Ravi NFT2-591 (DMs 1:1 FE) has no due date and DMs launches Thu —
             when will this be ready?"
      - `missing_due_date` — a launch-milestone issue with no due date as the project's
        target date closes in. This is the highest-value trigger there is: an undated
        issue on a launching project is how a launch slips quietly.
      - `missing_repro` / `vague_scope` — a bug nobody can reproduce, or an issue too
        vague to build. (Judged by the model — see `Classifier.assess_issue_gap`.)

  LEVEL 2 → THE PMs (ESCALATION_USER_IDS: Trishi, Kushal), for a call ABOVE AN IC. The
            assignee is not the right person to ask, because the answer isn't theirs:
      - `stuck_in_progress` — In Progress ≥ STALE_IN_PROGRESS_DAYS with no state change
        AND no comment explaining the holdup. Nobody said why; that's a PM call.
      - `launch_unassigned` — a launch-critical issue with NO assignee. There is no IC
        to ask; somebody has to decide who owns it.
      - `blocker` — the comments show the issue is stuck for a REASON that needs a
        decision/prioritisation call: a stated blocker, an unclear description the
        assignee flagged, or a need for knowledge-transfer from a colleague. Pinging the
        IC won't help, so we surface the reason to the PMs. (Judged from the comments —
        see `Classifier.assess_issue_comments`, wired in bot.py.)

NOTE (routing rule): a plain MISSING DUE DATE is ALWAYS a level-1 ask to the assignee —
it NEVER escalates to the PMs. Only genuine "big problem" cases (stuck-and-silent, a
comment-evidenced blocker needing a decision, an unowned launch-critical issue) go up.

HARD RULE, both levels: this path NUDGES ONLY. Nothing here writes to Linear — no
create, no comment, no assign, no transition. Its only output is a Discord message
asking a human. That is the whole point: she escalates to people, she doesn't
unilaterally fix the ticket.

Purity: this module holds no clients and performs no I/O. It takes already-fetched
issue dicts and returns findings, which makes the ladder's logic directly testable.
"""
import logging
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Levels — who gets asked.
LEVEL_ASSIGNEE = "assignee"
LEVEL_PM = "pm"

# Finding kinds. The kind is part of the rate-limit key, so the same person can be
# asked about a due date AND (later) about a missing repro on the same issue, but never
# twice about the SAME thing inside the cooldown window.
MISSING_DUE_DATE = "missing_due_date"
MISSING_REPRO = "missing_repro"
VAGUE_SCOPE = "vague_scope"
STUCK_IN_PROGRESS = "stuck_in_progress"
LAUNCH_UNASSIGNED = "launch_unassigned"
BLOCKER = "blocker"

# Linear workflow state types that count as "actively being worked".
_STARTED_TYPES = {"started"}


def is_active(issue: dict, active_names: set) -> bool:
    """Is this issue in one of the ACTIVE workflow states we're allowed to chase — matched
    by state NAME (case-insensitive), because In Progress / Implemented / awaiting QA /
    In Review share the "started"/"completed" TYPE and only the name tells them apart?
    `active_names` is a set of lower-cased names (from COS_ACTIVE_STATE_NAMES). Empty set →
    False (chase nothing), never "everything"."""
    if not active_names:
        return False
    return str(issue.get("state") or "").strip().lower() in active_names

# Project states we won't chase — a completed/cancelled project has no launch to protect.
_DEAD_PROJECT_STATES = {"completed", "canceled", "cancelled"}


def _parse_date(value) -> Optional[date]:
    """A Linear date ("2026-07-16") or ISO timestamp → a date. None when absent or
    unparseable, which every caller treats as "unknown", never as "now"."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        log.debug("[escalation] unparseable date %r", value)
        return None


def _days_since(value, *, today: date) -> Optional[int]:
    d = _parse_date(value)
    return None if d is None else (today - d).days


def days_until_launch(issue: dict, *, today: date) -> Optional[int]:
    """Days until this issue's PROJECT target date. Negative = the target date has
    already passed (which is *more* urgent, not less). None when the issue has no
    project, or the project has no target date — we never invent a deadline."""
    if str(issue.get("project_state") or "").lower() in _DEAD_PROJECT_STATES:
        return None
    target = _parse_date(issue.get("project_target_date"))
    return None if target is None else (target - today).days


def is_launch_critical(issue: dict, *, window_days: int, today: date) -> bool:
    """Is this issue on a project whose launch is imminent (or overdue)? This is the
    gate on the highest-value nudges — we chase hardest exactly where a slip costs
    most, and stay quiet on a project that isn't landing for months."""
    d = days_until_launch(issue, today=today)
    return d is not None and d <= window_days


def _launch_context(issue: dict, *, today: date) -> Optional[dict]:
    """The "…and DMs launches Thu" clause — the project, its target date, and how many
    days away it is, for the message. None when the issue has no launch pressure."""
    d = days_until_launch(issue, today=today)
    if d is None:
        return None
    return {
        "project": issue.get("project"),
        "target_date": issue.get("project_target_date"),
        "days_until": d,
    }


def _finding(issue: dict, *, level: str, kind: str, detail: str, today: date) -> dict:
    return {
        "level": level,
        "kind": kind,
        "detail": detail,
        "issue_id": issue.get("identifier") or issue.get("id"),
        "issue_title": issue.get("title") or "",
        "issue_url": issue.get("url") or "",
        "issue_state": issue.get("state") or "",
        "assignee": issue.get("assignee") or "",
        "assignee_email": issue.get("assignee_email") or "",
        "launch": _launch_context(issue, today=today),
    }


def model_finding(
    issue: dict, *, kind: str, detail: str, today: Optional[date] = None
) -> dict:
    """Wrap a MODEL-judged gap (missing repro / vague scope — see
    `Classifier.assess_issue_gap`) in the same finding shape `find_gaps` produces, so
    both halves of the ladder flow through one code path from here on.

    Always level 1: a missing repro or a fuzzy scope is a question for the person who
    owns the issue, not a decision for a PM."""
    return _finding(
        issue,
        level=LEVEL_ASSIGNEE,
        kind=kind,
        detail=detail,
        today=today or datetime.now(timezone.utc).date(),
    )


def pm_blocker_finding(issue: dict, *, detail: str, today: Optional[date] = None) -> dict:
    """Wrap a COMMENT-EVIDENCED blocker (a stated blocker / a flagged-unclear description /
    a knowledge-transfer need — see `Classifier.assess_issue_comments`) as a LEVEL-2 PM
    finding. Used when the comments show that pinging the assignee won't help: the reason
    is surfaced to the PMs, who own the decision/prioritisation call. `detail` is the
    short reason to state (e.g. "is blocked on the payments API, per Ravi's 9 Jul comment")."""
    return _finding(
        issue,
        level=LEVEL_PM,
        kind=BLOCKER,
        detail=detail,
        today=today or datetime.now(timezone.utc).date(),
    )


def find_gaps(
    issues: list[dict],
    *,
    launch_window_days: int,
    stale_in_progress_days: int,
    today: Optional[date] = None,
) -> list[dict]:
    """The DETERMINISTIC half of the ladder — the gaps that need no model to see.

    Returns findings ordered by urgency (soonest/most-overdue launch first), so that
    when the per-sweep cap bites, what survives is what matters most. At most ONE
    finding per issue: an issue that is both undated and stuck should produce the
    single most important ask, not two pings about the same ticket.

    The model-judged gaps (missing repro, vague scope) are added by the caller, which
    is what keeps this function pure and testable.
    """
    today = today or datetime.now(timezone.utc).date()
    findings: list[dict] = []

    for issue in issues or []:
        state_type = str(issue.get("state_type") or "").lower()
        launch_critical = is_launch_critical(
            issue, window_days=launch_window_days, today=today
        )
        has_assignee = bool(issue.get("assignee"))

        # LEVEL 2 — stuck. In Progress too long, no state change, and crucially NO
        # COMMENT explaining why. A comment means someone already said what's going on,
        # and a CoS who nudges anyway is just noise.
        if state_type in _STARTED_TYPES:
            days_started = _days_since(issue.get("started_at"), today=today)
            comment = issue.get("latest_comment") or {}
            days_since_comment = _days_since(comment.get("created_at"), today=today)
            unexplained = (
                days_since_comment is None or days_since_comment >= stale_in_progress_days
            )
            if (
                days_started is not None
                and days_started >= stale_in_progress_days
                and unexplained
            ):
                explanation = (
                    "no comments explaining the holdup"
                    if days_since_comment is None
                    else f"no update in {days_since_comment} days"
                )
                findings.append(
                    _finding(
                        issue,
                        level=LEVEL_PM,
                        kind=STUCK_IN_PROGRESS,
                        detail=(
                            f"has been In Progress {days_started} days with "
                            f"{explanation}"
                        ),
                        today=today,
                    )
                )
                continue

        # Everything below is launch pressure. Off a launching project, an undated
        # backlog issue is normal life, not a problem — we don't chase it.
        if not launch_critical:
            continue

        # LEVEL 2 — a launch-critical issue with nobody on it. There's no IC to ask:
        # who owns this is a PM decision.
        if not has_assignee:
            findings.append(
                _finding(
                    issue,
                    level=LEVEL_PM,
                    kind=LAUNCH_UNASSIGNED,
                    detail="is unassigned with the launch this close — nobody owns it",
                    today=today,
                )
            )
            continue

        # LEVEL 1 — the flagship trigger: a launch-milestone issue with no due date.
        # The assignee can answer this in one line, so we ask THEM, not the PMs.
        if not issue.get("dueDate"):
            findings.append(
                _finding(
                    issue,
                    level=LEVEL_ASSIGNEE,
                    kind=MISSING_DUE_DATE,
                    detail="has no due date",
                    today=today,
                )
            )

    def _urgency(f: dict) -> tuple:
        launch = f.get("launch") or {}
        days = launch.get("days_until")
        # Launch-linked first (soonest/most overdue first), then everything else.
        return (0, days) if days is not None else (1, 0)

    findings.sort(key=_urgency)
    log.info(
        "[escalation] %d deterministic finding(s): %s",
        len(findings),
        [f"{f['issue_id']}:{f['kind']}" for f in findings],
    )
    return findings


def describe_launch(launch: Optional[dict]) -> str:
    """"and DMs launches in 2 days" — the pressure clause, in words. "" when the issue
    carries no launch context, so the message simply omits it."""
    if not launch or not launch.get("project"):
        return ""
    days = launch.get("days_until")
    name = launch["project"]
    if days is None:
        return ""
    if days < 0:
        return f"{name} was due to launch {abs(days)} day(s) ago"
    if days == 0:
        return f"{name} launches TODAY"
    if days == 1:
        return f"{name} launches tomorrow"
    return f"{name} launches in {days} days"


# What each gap actually needs from the person being asked. Used for the deterministic
# fallback text, and given to the model as the "what's unclear" it must state.
_ASK = {
    MISSING_DUE_DATE: "When will this be ready?",
    MISSING_REPRO: "What are the steps to reproduce it?",
    VAGUE_SCOPE: "What exactly is in scope here?",
    STUCK_IN_PROGRESS: "Needs a call.",
    LAUNCH_UNASSIGNED: "Who should own this?",
    BLOCKER: "Needs a call.",
}


def fallback_message(finding: dict, mentions: str) -> str:
    """The deterministic nudge, used only when the model call fails. Still states the
    SITUATION and WHAT'S UNCLEAR — a nudge that doesn't say what it wants is worthless.

    `mentions` is the already-resolved audience ("<@1> <@2>", or a plain name when the
    person isn't mapped to a Discord id) — this module never resolves people itself.
    """
    issue = f"{finding.get('issue_id', '?')}"
    title = finding.get("issue_title") or ""
    if title:
        issue = f"{issue} ({title})"
    launch = describe_launch(finding.get("launch"))
    tail = f" — {launch}" if launch else ""
    ask = _ASK.get(finding.get("kind", ""), "can you take a look?")
    detail = finding.get("detail") or "needs attention"
    url = finding.get("issue_url") or ""
    link = f" {url}" if url else ""
    return f"{mentions} {issue} {detail}{tail}. {ask}{link}"
