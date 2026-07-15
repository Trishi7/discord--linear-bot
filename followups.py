"""Open threads — the commitments the Chief of Staff is waiting on.

A real CoS remembers what people said they'd come back with. When someone posts
"I'll confirm the DM date in an hour" or "will update after testing", that's an
OPEN THREAD: a promise with an implied due time and nobody tracking it. This
module holds the pieces for spotting those and for nudging on them:

  - `looks_like_commitment()` — a cheap keyword prefilter so the vast majority of
    channel chatter never reaches the LLM. Only what survives it is worth a call.
  - `due_at_from()` — turns the model's "how long did they give themselves"
    estimate into an absolute UTC due time, clamped to sane bounds.
  - `fallback_nudge()` — the deterministic reminder text, used when the model call
    for the persona-voiced nudge fails. The nudge must still go out.

STRICTLY READ/PROPOSE ONLY. An open thread's only possible effect is a REMINDER
addressed to a human in the channel it came from. It never creates or edits a
Linear issue, never changes a status, and is never an input to ticket creation.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# UTC 'YYYY-MM-DD HH:MM:SS' — the same shape SQLite's CURRENT_TIMESTAMP writes,
# so stored timestamps sort and compare correctly as plain strings.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# Bounds on a promise's due time. A "give me 2 minutes" is not worth tracking as
# an open thread, and anything beyond a week is a plan, not a promise.
MIN_DUE_MINUTES = 15
MAX_DUE_MINUTES = 7 * 24 * 60


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_ts(dt: datetime) -> str:
    """A datetime → the UTC string form used in the DB."""
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def from_ts(ts: str) -> datetime:
    """A stored UTC string → an aware datetime. Falls back to `now` on a malformed
    value so a bad row can never crash the sweeper."""
    try:
        return datetime.strptime(str(ts), TS_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        log.warning("[followups] unparseable timestamp %r; treating as now", ts)
        return now_utc()


# First-person future commitment markers. Deliberately BROAD (cheap to run, and a
# false positive only costs one small LLM call, which then rejects it) but still
# anchored on someone speaking about their OWN next action — "i'll", "we'll",
# "will get", "let me check", "by tomorrow", "once the deploy lands".
_COMMITMENT_PATTERNS = [
    r"\bi(?:'| a)?ll\b",              # I'll / Ill / I will (contraction forms)
    r"\bwe(?:'| wi)?ll\b",            # we'll / we will
    r"\bi (?:will|can|shall)\b",
    r"\bwe (?:will|can|shall)\b",
    r"\bwill (?:confirm|update|check|share|send|get|let you know|revert|look)\b",
    r"\b(?:let me|lemme) (?:check|confirm|look|see|find out|get)\b",
    r"\b(?:give me|gimme) (?:a|an|\d+)\b",
    r"\bget back to (?:you|u|ya)\b",
    r"\bkeep you posted\b",
    r"\brevert(?:ing)? (?:on|with|back)\b",   # Indian-English "I'll revert on this"
    r"\bon it\b",
    r"\bwill do\b",
    r"\bshortly\b",
    # Go-live / delivery commitments — "going live", "I'll release/ship/push/deploy",
    # "will be done by …". A launch date owed to the channel is a commitment too.
    r"\bgoing live\b",
    r"\bgo(?:es)? live\b",
    r"\bwill (?:be )?(?:live|released?|shipped?|deployed?|done|ready)\b",
    r"\b(?:i|we)(?:'ll| will| can)? ?(?:release|ship|push|deploy|launch|roll(?:ing)? out)\b",
    r"\b(?:release|ship|deploy|launch)(?:ing)? (?:it|this|the|by|today|tomorrow|tonight)\b",
    r"\bby (?:eod|eow|tomorrow|tonight|today|monday|tuesday|wednesday|thursday|friday)\b",
    r"\bin (?:a|an|\d+)\s*(?:min|mins|minute|minutes|hour|hours|hr|hrs|day|days)\b",
    r"\b(?:after|once) (?:i|we|the|testing|the test|deploy|deployment|qa)\b",
]
_COMMITMENT_RE = re.compile("|".join(_COMMITMENT_PATTERNS), re.IGNORECASE)

# A promise needs enough words to carry a WHAT. "ok will do" alone is an ack, not
# something we can meaningfully chase.
_MIN_COMMITMENT_CHARS = 12


def looks_like_commitment(text: str) -> bool:
    """Cheap gate before the LLM: could this message plausibly be someone promising
    to come back with something? Tuned to over-accept — the model makes the real
    call, and a message that never reaches it can never become an open thread."""
    t = (text or "").strip()
    if len(t) < _MIN_COMMITMENT_CHARS:
        return False
    return bool(_COMMITMENT_RE.search(t))


def due_at_from(due_minutes, *, default_minutes: int, promised_at: datetime) -> datetime:
    """When we may first nudge about a promise made at `promised_at`.

    `due_minutes` is the model's read of the deadline the person gave themselves
    ("in an hour" → 60, "by EOD" → whatever's left of the day). None/unusable —
    they promised without a time ("will update after testing") — falls back to
    `default_minutes` (COS_FOLLOWUP_DEFAULT_DUE_MINUTES). Clamped to
    [MIN_DUE_MINUTES, MAX_DUE_MINUTES] so a bad estimate can't schedule a nudge 90
    seconds or 3 months from now."""
    try:
        minutes = int(due_minutes)
    except (TypeError, ValueError):
        minutes = int(default_minutes)
    if minutes <= 0:
        minutes = int(default_minutes)
    minutes = max(MIN_DUE_MINUTES, min(MAX_DUE_MINUTES, minutes))
    return promised_at + timedelta(minutes=minutes)


def humanize_age(since: datetime, *, until: datetime = None) -> str:
    """"about an hour ago", "3 days ago" — how we refer to when the promise was
    made. Approximate on purpose: the nudge is a human reminder, not a stopwatch."""
    delta = (until or now_utc()) - since
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 90:
        return "about an hour ago" if minutes >= 40 else f"{max(1, minutes)} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return f"about {hours} hours ago"
    days = hours // 24
    return "yesterday" if days == 1 else f"{days} days ago"


def mention_or_name(person_id, person_name: str) -> str:
    """Address the person by Discord @-mention when we know their ID (the nudge is
    FOR them, so it should actually reach them), else by the name they posted under."""
    if person_id:
        return f"<@{person_id}>"
    return person_name or "there"


def fallback_nudge(item: dict) -> str:
    """The reminder text when the persona-voiced model call fails. Still first
    person, still a question — never a status line the person can ignore."""
    who = mention_or_name(item.get("person_id"), item.get("person_name", ""))
    what = (item.get("what") or "").strip() or "something you were going to come back on"
    when = humanize_age(from_ts(item.get("promised_at", "")))
    jump = item.get("jump_url") or ""
    tail = f" ({jump})" if jump else ""
    return f"{who} — you mentioned {what} {when}. Any update?{tail}"
