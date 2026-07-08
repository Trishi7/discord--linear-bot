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

    return missing
