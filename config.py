"""Configuration loaded from environment variables."""
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def _int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _int_set(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


def _lower_str_set(name: str, default: str = "") -> set[str]:
    raw = os.getenv(name, default)
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _str_list(name: str, default: str = "") -> list[str]:
    """Comma-separated env value → list of stripped strings (original casing kept)."""
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    log.warning("%s=%r is not a recognised boolean; using default %s", name, raw, default)
    return default


def _json_object(name: str, default: dict) -> dict:
    raw = os.getenv(name, "").strip()
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Could not parse %s as JSON (%s); using default", name, e)
        return dict(default)
    if not isinstance(parsed, dict):
        log.warning(
            "%s must be a JSON object; got %s. Using default.",
            name,
            type(parsed).__name__,
        )
        return dict(default)
    return parsed


# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
MONITORED_CHANNEL_IDS = _int_list("MONITORED_CHANNEL_IDS")
# Approval channel — where ✅/❌ approval embeds are posted and handled. NOTHING
# else happens here (no query answering).
APPROVAL_CHANNEL_ID = int(os.getenv("APPROVAL_CHANNEL_ID", "0") or "0")
# Query channel — a dedicated channel for asking the bot questions. Query mode
# runs here and replies here; no ticket creation, no approvals. In this channel
# the @-mention requirement is DROPPED — every non-bot human message is treated
# as a potential query. 0/unset → query mode falls back to the previous
# behaviour (answering @-mention questions in the approval channel).
QUERY_CHANNEL_ID = int(os.getenv("QUERY_CHANNEL_ID", "0") or "0")


def query_channel_id() -> int:
    """The channel the bot answers questions in: the dedicated QUERY_CHANNEL_ID
    when set, else the approval channel (backwards-compatible fallback)."""
    return QUERY_CHANNEL_ID or APPROVAL_CHANNEL_ID


def is_dedicated_query_channel(channel_id: int) -> bool:
    """True only when a dedicated QUERY_CHANNEL_ID is configured AND this is it.
    A dedicated query channel drops the @-mention requirement; the approval-
    channel fallback does not."""
    return bool(QUERY_CHANNEL_ID) and channel_id == QUERY_CHANNEL_ID


def is_query_only_channel(channel_id: int) -> bool:
    """True for channels where the bot ONLY answers questions and NEVER runs
    triage/ticket creation: the dedicated query channel, or — when none is set —
    the approval channel (fallback)."""
    return channel_id == query_channel_id()

# Reporter allowlist — only messages from these users get classified.
# IDs win over names; names are the fallback for users whose Discord ID we don't know yet.
ALLOWED_REPORTER_IDS: set[int] = _int_set("ALLOWED_REPORTER_IDS")
ALLOWED_REPORTER_NAMES: set[str] = _lower_str_set(
    "ALLOWED_REPORTER_NAMES", default="Sid,Harsh,Trishi"
)

# Discord user ID → Linear user (email or UUID), used for assignment / @-mention mapping.
DISCORD_LINEAR_MAP: dict = _json_object("DISCORD_LINEAR_MAP", default={})


def _build_linear_to_discord() -> dict[str, int]:
    """REVERSE of DISCORD_LINEAR_MAP: Linear identity (email or UUID, lower-cased) →
    Discord user id. Built once at import.

    The forward map answers "who in Linear do I assign this Discord user's report to".
    The Chief-of-Staff nudge path needs the opposite: it starts from a LINEAR issue's
    assignee and has to work out who to @-mention in Discord.

    Keys are lower-cased so an email that differs only in case still matches. A
    malformed entry (non-numeric Discord id, empty Linear value) is skipped with a
    warning rather than crashing startup."""
    out: dict[str, int] = {}
    for discord_id, linear_ident in (DISCORD_LINEAR_MAP or {}).items():
        if not linear_ident or not str(linear_ident).strip():
            continue
        try:
            did = int(str(discord_id).strip())
        except (TypeError, ValueError):
            log.warning(
                "DISCORD_LINEAR_MAP key %r is not a numeric Discord id; skipping",
                discord_id,
            )
            continue
        key = str(linear_ident).strip().lower()
        if key in out and out[key] != did:
            log.warning(
                "DISCORD_LINEAR_MAP maps Linear user %r to two Discord ids (%s, %s); "
                "keeping the first",
                linear_ident, out[key], did,
            )
            continue
        out[key] = did
    return out


LINEAR_TO_DISCORD: dict[str, int] = _build_linear_to_discord()


def discord_id_for_linear(*identities: str) -> int | None:
    """The Discord user id for a Linear person, given any of their known identities
    (email and/or UUID — pass whatever the issue gave you). Returns None when we have
    no mapping, which the nudge path treats as "name them in plain text, don't ping".

    This is the ONLY gate on who may be @-mentioned by a nudge: an unmapped person is
    never pinged, so a stray Linear account can't be tagged into a Discord channel."""
    for ident in identities:
        if not ident:
            continue
        found = LINEAR_TO_DISCORD.get(str(ident).strip().lower())
        if found:
            return found
    return None

# Classifier
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "claude-sonnet-4-6")

# Linear
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY", "")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID", "")

# Tuning
MIN_MESSAGE_LENGTH = int(os.getenv("MIN_MESSAGE_LENGTH", "20"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.6"))
# Seconds to wait before classifying a new message, giving follow-up
# clarifications (frontend vs backend, "nvm, working now", etc.) a chance to
# arrive and be included in the thread context. Set to 0 to disable.
CLASSIFY_DELAY_SECONDS = float(os.getenv("CLASSIFY_DELAY_SECONDS", "0"))
DB_PATH = os.getenv("DB_PATH", "./bot_state.db")

# Query mode — person-centric activity lookups ("what is Sid working on?").
# How far back to scan Discord for a person-activity query.
QUERY_DISCORD_LOOKBACK_DAYS = int(os.getenv("QUERY_DISCORD_LOOKBACK_DAYS", "14"))
# Hard cap on messages scanned per monitored channel per query, to bound API
# cost / latency.
QUERY_MAX_MESSAGES_PER_CHANNEL = int(os.getenv("QUERY_MAX_MESSAGES_PER_CHANNEL", "400"))

# Chief-of-Staff persona — the bot's VOICE only (see persona.py). When true, the
# named first-person persona is prepended to every reply-path system prompt; when
# false, prompts fall back to their original neutral voice. Default true.
COS_PERSONA_ENABLED = _bool("COS_PERSONA_ENABLED", default=True)

# Short-term query-mode memory (memory.py) — how many recent (question, answer)
# turns to keep per channel/thread so a follow-up ("what about Ravi?") carries
# its intent forward, and how long before those turns expire.
QUERY_MEMORY_TURNS = int(os.getenv("QUERY_MEMORY_TURNS", "5"))
QUERY_MEMORY_TTL_MINUTES = int(os.getenv("QUERY_MEMORY_TTL_MINUTES", "10"))

# Standup notes — READ-ONLY context for QUERY MODE only; NEVER an input to ticket
# creation. A separate rclone process syncs Gemini notes into a local folder; the
# bot only reads local files and holds NO Google credential.
# Path to that local folder (e.g. ./standups). Empty → standup features no-op.
STANDUP_DIR = os.getenv("STANDUP_DIR", "").strip()
# Optional shell command to force a sync on demand (e.g. "rclone copy ..."), run
# before reading when a question is clearly about a recent/today standup. Empty →
# read whatever is already on disk.
STANDUP_SYNC_CMD = os.getenv("STANDUP_SYNC_CMD", "").strip()

# Archive snapshot — a FROZEN local markdown file of past Done issues, used as a
# READ-ONLY query-mode fallback when live Linear can't return an issue (archived /
# not found). NEVER an input to ticket creation. Empty → archive features no-op.
ARCHIVE_FILE = os.getenv("ARCHIVE_FILE", "").strip()

# Holiday / leave channel — a Discord channel where people post OOO / on-leave
# notes. Read-only context for query mode ("why was X delayed"); this channel is
# NOT monitored for triage and never produces a ticket. 0/unset → disabled.
HOLIDAY_CHANNEL_ID = int(os.getenv("HOLIDAY_CHANNEL_ID", "0") or "0")

# CLARIFYING QUESTIONS (clarify.py) — when a report is missing something a ticket
# genuinely can't do without (a bug with no repro / no expected-vs-actual, scope too
# vague to action, no owner and none implied), the bot asks ONE focused question in
# the source channel INSTEAD of posting a half-complete approval embed. The plan is
# PARKED, not filed; it goes to the normal ✅/❌ approval gate once the reporter
# answers. READ/PROPOSE ONLY — this path can delay and enrich a proposal, but it can
# never create, comment on, or transition anything by itself.
COS_CLARIFY_ENABLED = _bool("COS_CLARIFY_ENABLED", default=True)
# How long to wait for an answer before proposing the PARKED plan as-is. A report is
# never dropped just because nobody replied — the ticket still gets proposed, minus
# the detail we asked for.
COS_CLARIFY_TIMEOUT_MINUTES = int(os.getenv("COS_CLARIFY_TIMEOUT_MINUTES", "120"))
# Don't stall a report on a question the model itself isn't sure about — below this
# confidence the gap is ignored and the plan goes straight to approval.
COS_CLARIFY_MIN_CONFIDENCE = float(os.getenv("COS_CLARIFY_MIN_CONFIDENCE", "0.6"))
# When a report genuinely needs more than one thing, she bundles up to this many
# questions into ONE message (a short numbered list) rather than firing several
# messages. 1 keeps the strict single-question behaviour.
COS_CLARIFY_MAX_QUESTIONS = int(os.getenv("COS_CLARIFY_MAX_QUESTIONS", "3"))
# Reporters often post their screenshot/recording a moment AFTER the text. Before
# asking, she waits this many seconds and re-reads the thread; if a follow-up
# attachment or message arrived she re-assesses (and may no longer need to ask), so
# she never asks for something that was about to land. 0 disables the wait.
COS_CLARIFY_ATTACHMENT_WAIT_SECONDS = int(
    os.getenv("COS_CLARIFY_ATTACHMENT_WAIT_SECONDS", "45")
)
# How long an unanswered clarifying question sits before the sweeper nudges the
# reporter once (subject to the shared nudge window/attempts below). Kept well under
# COS_CLARIFY_TIMEOUT_MINUTES so the reminder lands before the plan is proposed as-is.
COS_CLARIFY_NUDGE_AFTER_MINUTES = int(os.getenv("COS_CLARIFY_NUDGE_AFTER_MINUTES", "60"))

# PROACTIVE FOLLOW-UP / OPEN THREADS (followups.py) — the bot notices when someone
# promises to come back with something ("I'll confirm the DM dates in an hour"),
# tracks it, and nudges them in-channel once it ages past due.
# HARD RULE: follow-up NEVER creates or modifies a ticket. Its only possible output
# is a Discord message reminding a human. It touches no Linear API, and an open
# thread is never an input to ticket creation.
COS_FOLLOWUP_ENABLED = _bool("COS_FOLLOWUP_ENABLED", default=True)
# How often the background sweeper looks for aged open threads (and for clarifying
# questions that have timed out).
COS_FOLLOWUP_CHECK_INTERVAL_MINUTES = int(
    os.getenv("COS_FOLLOWUP_CHECK_INTERVAL_MINUTES", "30")
)
# Due time for a promise made with NO stated deadline ("will update after testing").
# When they DID state one ("in an hour"), that wins.
COS_FOLLOWUP_DEFAULT_DUE_MINUTES = int(
    os.getenv("COS_FOLLOWUP_DEFAULT_DUE_MINUTES", "180")
)
# How many times one open thread may be nudged before the bot gives up on it and
# marks it stale. Keeps a forgotten promise from becoming a recurring nag.
COS_FOLLOWUP_MAX_REMINDERS = int(os.getenv("COS_FOLLOWUP_MAX_REMINDERS", "2"))
# Minimum gap between two nudges about the SAME open thread.
COS_FOLLOWUP_REMINDER_COOLDOWN_MINUTES = int(
    os.getenv("COS_FOLLOWUP_REMINDER_COOLDOWN_MINUTES", "180")
)
# Only track a commitment the model is at least this sure about — a false nudge
# ("any update on that thing you never promised?") is worse than a missed one.
COS_FOLLOWUP_MIN_CONFIDENCE = float(os.getenv("COS_FOLLOWUP_MIN_CONFIDENCE", "0.6"))

# UNIFIED NUDGE POLICY — governs BOTH nudge sources (tracked commitments / open
# threads AND unanswered clarifying questions). The same person is nudged about the
# same item AT MOST once per COS_NUDGE_WINDOW_HOURS, and at most COS_NUDGE_MAX_ATTEMPTS
# times total before she stops, records the non-response, and tells the PMs ONCE.
# These act as the guardrail on top of the legacy per-open-thread cooldown/max above:
# the EFFECTIVE cooldown is the LONGER of the two and the EFFECTIVE cap is the SMALLER,
# so turning this on never makes her MORE chatty than before.
COS_NUDGE_WINDOW_HOURS = int(os.getenv("COS_NUDGE_WINDOW_HOURS", "24"))
COS_NUDGE_MAX_ATTEMPTS = int(os.getenv("COS_NUDGE_MAX_ATTEMPTS", "2"))

# TWO-LEVEL ESCALATION LADDER (escalation.py) — the bot audits OPEN Linear issues for
# gaps a Chief of Staff would chase, and asks a HUMAN about them in Discord.
#
# HARD RULE, both levels: she NUDGES ONLY. This path performs NO Linear writes — it
# never creates, comments on, assigns, or transitions an issue. Its only output is a
# Discord message @-mentioning a person. (Real Linear writes stay gated Day-3+ work.)
#
#   LEVEL 1 — the assignee, for a DATA GAP on their own issue (a missing due date on a
#             launch-milestone issue, a bug with no repro, scope too vague to build).
#   LEVEL 2 — the PMs, for a call ABOVE AN IC (stuck In Progress with no explanation, a
#             launch at risk, conflicting sources, a prioritisation decision).
COS_TAG_ASSIGNEE_ENABLED = _bool("COS_TAG_ASSIGNEE_ENABLED", default=True)
COS_ESCALATE_ENABLED = _bool("COS_ESCALATE_ENABLED", default=True)

# The PMs (Trishi, Kushal) — the ONLY people a level-2 escalation may @-mention.
# Comma-separated Discord user IDs. Empty → escalation no-ops (nobody to escalate to),
# logged as a warning at startup.
ESCALATION_USER_IDS: set[int] = _int_set("ESCALATION_USER_IDS")

# An issue sitting in a `started` state this many days — with NO state change and NO
# comment explaining the holdup — is stuck, and stuck is a PM call, not an IC nudge.
STALE_IN_PROGRESS_DAYS = int(os.getenv("STALE_IN_PROGRESS_DAYS", "3"))

# RATE LIMIT (she is now posting UNPROMPTED @-mentions — this is the main guardrail).
# The same person is never tagged about the same issue for the same reason more than
# once per this window. Nudges are also de-duped within a single sweep.
COS_TAG_COOLDOWN_HOURS = int(os.getenv("COS_TAG_COOLDOWN_HOURS", "24"))
# Hard cap on nudges posted per sweep, so a newly-configured bot meeting a messy
# backlog can't carpet-bomb the channel on its first pass.
COS_MAX_NUDGES_PER_SWEEP = int(os.getenv("COS_MAX_NUDGES_PER_SWEEP", "3"))

# A project's target date this close (or already past) makes its issues LAUNCH-CRITICAL:
# an undated or unexplained issue in that window is what she chases hardest. This is the
# DMs-launch trigger — "NFT2-591 has no due date and DMs launches Thu".
COS_LAUNCH_WINDOW_DAYS = int(os.getenv("COS_LAUNCH_WINDOW_DAYS", "7"))

# Where the nudges/escalations are posted. Must be a channel the team actually reads.
# Empty → the first monitored channel (see cos_nudge_channel_id()).
COS_NUDGE_CHANNEL_ID = int(os.getenv("COS_NUDGE_CHANNEL_ID", "0") or "0")

# Hard cap on issues pulled per audit pass, bounding Linear + LLM cost.
COS_AUDIT_MAX_ISSUES = int(os.getenv("COS_AUDIT_MAX_ISSUES", "50"))

# ACTIVE-ONLY SCOPE — the escalation/tagging audit ONLY considers issues whose workflow
# state NAME is one of these: In Progress, Implemented (a.k.a. "awaiting QA" — dev-done,
# not yet tested), or In Review. Backlog / Todo / Done / Canceled are ignored for the
# tagging behaviour. Matched by NAME (case-insensitive) on purpose: these states share
# the "started"/"completed" TYPE, so type-matching can't distinguish them. Comma-separated
# — override to your workspace's exact state names. The default lists BOTH "Implemented"
# and "awaiting QA" because the dev-done state is named differently across workspaces.
COS_ACTIVE_STATE_NAMES: list[str] = _str_list(
    "COS_ACTIVE_STATE_NAMES", default="In Progress,Implemented,awaiting QA,In Review"
)

# CHECK COMMENTS BEFORE TAGGING — before pinging an assignee, read the issue's comments.
# If a recent comment already answers what she'd ask (e.g. a committed deadline) she does
# NOT tag; if the comments reveal a blocker / needs-a-decision / knowledge-transfer gap,
# a ping to the IC won't help, so she surfaces the reason and escalates to the PMs instead.
# READ-ONLY (the reading part). Default ON.
COS_CHECK_COMMENTS_ENABLED = _bool("COS_CHECK_COMMENTS_ENABLED", default=True)

# CLOSE THE LOOP — the FIRST scoped Linear WRITE this bot performs. After she tags an
# assignee about a missing deadline, she watches for the answer (their Discord REPLY to her
# tag, a new issue COMMENT, or the STANDUP notes), parses a date, and updates the ticket.
# She may ONLY: (a) add a comment, and (b) set/update the DUE DATE — never a status change,
# never a reassignment. When she sets a due date she also comments noting the date AND its
# source. OFF by default — this is the only write, so it stays opt-in.
COS_UPDATE_DEADLINE_ENABLED = _bool("COS_UPDATE_DEADLINE_ENABLED", default=False)

# DRY-RUN guard for the write above. When true (the default, even once the feature is
# enabled), she LOGS what she WOULD do — "would set NFT2-591 due 2026-07-18 (source:
# Discord reply)" — and writes NOTHING to Linear, so the path can be tested safely. Set
# false only when you want real writes.
COS_UPDATE_DEADLINE_DRY_RUN = _bool("COS_UPDATE_DEADLINE_DRY_RUN", default=True)

# How long a deadline watch waits for an answer before it's given up on (marked expired),
# so a question nobody ever answers doesn't linger forever getting re-checked each sweep.
COS_DEADLINE_WATCH_EXPIRE_DAYS = int(os.getenv("COS_DEADLINE_WATCH_EXPIRE_DAYS", "7"))


def cos_nudge_channel_id() -> int:
    """The channel nudges/escalations are posted in: COS_NUDGE_CHANNEL_ID when set,
    else the first monitored channel (where the team is already reporting). 0 when
    nothing is configured — the audit then no-ops rather than guessing a channel."""
    if COS_NUDGE_CHANNEL_ID:
        return COS_NUDGE_CHANNEL_ID
    return MONITORED_CHANNEL_IDS[0] if MONITORED_CHANNEL_IDS else 0


# If False, the bot creates Linear issues directly without the ✅/❌ approval step.
REQUIRE_APPROVAL = _bool("REQUIRE_APPROVAL", default=True)


def validate() -> list[str]:
    """Return a list of missing required env vars (empty if all good)."""
    required = {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "LINEAR_API_KEY": LINEAR_API_KEY,
        "LINEAR_TEAM_ID": LINEAR_TEAM_ID,
        "APPROVAL_CHANNEL_ID": APPROVAL_CHANNEL_ID,
    }
    missing = [k for k, v in required.items() if not v]
    if not MONITORED_CHANNEL_IDS:
        missing.append("MONITORED_CHANNEL_IDS")

    # HARD RULE: the query channel must NOT be monitored for triage. If it
    # accidentally overlaps a monitored channel, warn and let the bot treat it
    # as query-only (skip triage there) rather than filing tickets from it.
    if QUERY_CHANNEL_ID and QUERY_CHANNEL_ID in MONITORED_CHANNEL_IDS:
        log.warning(
            "QUERY_CHANNEL_ID=%s is also in MONITORED_CHANNEL_IDS — the query "
            "channel must not be triaged. Treating it as query-only (no "
            "classification/ticket creation will run there).",
            QUERY_CHANNEL_ID,
        )
    if QUERY_CHANNEL_ID and QUERY_CHANNEL_ID == APPROVAL_CHANNEL_ID:
        log.warning(
            "QUERY_CHANNEL_ID=%s equals APPROVAL_CHANNEL_ID — approval embeds and "
            "query replies will share one channel. Use separate channels to keep "
            "the roles distinct.",
            QUERY_CHANNEL_ID,
        )

    if not ALLOWED_REPORTER_IDS and not ALLOWED_REPORTER_NAMES:
        log.warning(
            "ALLOWED_REPORTER_IDS and ALLOWED_REPORTER_NAMES are both empty — "
            "the bot will act on NOBODY. Set at least one to enable triage."
        )

    # The escalation ladder can only speak to people it can reach.
    if COS_ESCALATE_ENABLED and not ESCALATION_USER_IDS:
        log.warning(
            "COS_ESCALATE_ENABLED=true but ESCALATION_USER_IDS is empty — there is "
            "nobody to escalate to, so level-2 escalations will be skipped. Set it to "
            "the PMs' Discord ids (Trishi, Kushal)."
        )
    if COS_TAG_ASSIGNEE_ENABLED and not LINEAR_TO_DISCORD:
        log.warning(
            "COS_TAG_ASSIGNEE_ENABLED=true but DISCORD_LINEAR_MAP is empty — no Linear "
            "assignee can be reverse-resolved to a Discord user, so assignees will be "
            "named in plain text and never @-mentioned."
        )
    if (COS_TAG_ASSIGNEE_ENABLED or COS_ESCALATE_ENABLED) and not cos_nudge_channel_id():
        log.warning(
            "The escalation ladder is enabled but there is no channel to post in "
            "(COS_NUDGE_CHANNEL_ID unset and MONITORED_CHANNEL_IDS empty) — the gap "
            "audit will no-op."
        )

    if (COS_TAG_ASSIGNEE_ENABLED or COS_ESCALATE_ENABLED) and not COS_ACTIVE_STATE_NAMES:
        log.warning(
            "COS_ACTIVE_STATE_NAMES is empty — the gap audit scopes to active issues by "
            "state NAME, so with no names it will chase NOTHING. Set it to your workspace's "
            "In Progress / Implemented (awaiting QA) / In Review state names."
        )

    # The one write path: loud about whether it is armed, because it is the only thing here
    # that changes Linear.
    if COS_UPDATE_DEADLINE_ENABLED and not COS_UPDATE_DEADLINE_DRY_RUN:
        log.warning(
            "COS_UPDATE_DEADLINE_ENABLED=true and COS_UPDATE_DEADLINE_DRY_RUN=false — the "
            "bot WILL write to Linear (add a comment + set the due date) when it resolves a "
            "deadline. This is the only Linear write it performs. Set DRY_RUN=true to test "
            "first (it will log the intended write instead)."
        )
    elif COS_UPDATE_DEADLINE_ENABLED:
        log.info(
            "COS_UPDATE_DEADLINE_ENABLED=true in DRY-RUN mode — deadline resolutions will be "
            "logged, not written. Set COS_UPDATE_DEADLINE_DRY_RUN=false to arm real writes."
        )

    return missing
