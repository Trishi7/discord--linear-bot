"""Discord triage bot. Listens for messages in monitored channels, classifies
them with the LLM, and reconciles each one with Linear by picking one of:

  - noise / non-allowed reporter → do nothing,
  - create a new issue,
  - comment on an existing thread issue (optionally + status transition),
  - comment on a clearly-matching open issue found via search (dup).

Behind config.REQUIRE_APPROVAL (default True), the proposed action is posted to
APPROVAL_CHANNEL_ID as an embed; ✅ executes it, ❌ discards. With
REQUIRE_APPROVAL=False the action runs immediately and a short confirmation is
posted to the same channel instead.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

import archive as archive_mod
import clarify
import config
import escalation
import followups
import persona
import query
import standup
from classifier import Classifier
from db import DB
from linear_client import LinearClient, LinearError, SIGNAL_TO_STATE_NAME
from memory import ConversationMemory
from query_engine import QueryEngine

log = logging.getLogger(__name__)

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"
# ✅ on a follow-up nudge closes that open thread ("handled, stop asking"). Same
# glyph as approval, but a different channel and a different table — the two can
# never collide (a nudge lives in a monitored channel, an approval embed doesn't).
CLOSE_EMOJI = APPROVE_EMOJI

def _iso_after(iso_ts: Optional[str], cutoff: datetime) -> bool:
    """True when a Linear ISO timestamp (e.g. '2026-07-14T14:08:52.198Z') is at/after
    `cutoff` (an aware datetime). Unparseable/empty → False, so a bad value never counts
    as 'after we asked'. Used to keep only comments left since a deadline was requested."""
    if not iso_ts:
        return False
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _thread_signature(thread: list) -> tuple:
    """A cheap (message-count, total-attachment-count) fingerprint of a thread, so the
    clarify path can tell whether a follow-up message or a screenshot/recording landed
    during the brief attachment-wait before it decides to ask."""
    msgs = list(thread or [])
    attachments = sum(len(getattr(m, "attachments", []) or []) for m in msgs)
    return (len(msgs), attachments)


CATEGORY_ICON = {"bug": "🐛", "feature": "✨", "improvement": "🔧"}
CATEGORY_COLOR = {
    "bug": 0xE03E3E,
    "feature": 0x3E8FE0,
    "improvement": 0xE0A23E,
}
_COMMENT_COLOR = 0x888888
_TRIAGE_COLOR = 0xE0A23E

# Classifier category → Linear label NAME. "noise" never reaches a create path.
_CATEGORY_LABEL_NAME = {
    "bug": "Bug",
    "feature": "Feature",
    "improvement": "Improvement",
}

# Team convention: a Bug ALWAYS carries a system label showing where the fix lives.
_SYSTEM_LABELS = {"BE", "FE", "UI"}

# Team convention: NEVER assign a bug to QA (Harsh). Lower-cased mention names.
_NEVER_BUG_ASSIGNEE_NAMES = {"harsh"}

# Linear workflow state TYPEs we consider "open" for duplicate detection.
_OPEN_STATE_TYPES = {"backlog", "unstarted", "started", "triage"}

# Cap on how many issues the Discord person-activity fallback render lists.
QUERY_LIST_LIMIT = 10

# Discord hard-caps a single message at 2000 chars and SILENTLY DROPS the rest,
# so query-mode replies are split across messages at ~1900 to leave headroom.
QUERY_REPLY_CHUNK = 1900
# Safety cap on how many messages one query reply may fan out into. Beyond this
# we clip with a note rather than flood the channel — the engine/classifier
# prompts are tuned to summarise, so hitting this means an answer stayed long.
QUERY_REPLY_MAX_MESSAGES = 4

# Cap on how far we walk the reply chain / how much channel history we scan
# when gathering messages associated with one.
CONTEXT_REPLY_DEPTH = 25
CONTEXT_HISTORY_LIMIT = 200
# Cap on images sent to the classifier per request — keeps payloads sane.
MAX_IMAGES_PER_CLASSIFY = 8

# Cross-message dedup: how far back to look for a matching pending approval /
# tracked issue in the same channel, and how "clear" a title match must be to
# suppress a second embed or redirect a create into a comment. The threshold is
# deliberately HIGH — per team convention a wrong comment/merge is worse than a
# near-duplicate ticket, so we only act on an obvious match and otherwise create.
DEDUP_WINDOW_HOURS = 24
TITLE_CLEAR_MATCH = 0.66

# Low-signal words dropped before comparing report titles by key terms. Stored in
# STEMMED form (see _stem) so e.g. "fails"/"failing"/"failed" all collapse here.
_TITLE_STOPWORDS = {
    "the", "a", "an", "is", "are", "wa", "were", "be", "to", "of", "in", "on",
    "for", "and", "or", "with", "when", "not", "no", "it", "thi", "that", "then",
    "do", "doe", "cant", "cannot", "wont", "isnt", "after", "before", "from",
    "issue", "bug", "error", "problem", "fail", "broken", "pleas", "help", "wrong",
}


def _stem(tok: str) -> str:
    """Very light suffix stripping so morphological variants unify
    (uploading→upload, crashes→crash, DMs→dm). Not linguistically correct — just
    enough to match how the same bug gets worded two different ways."""
    for suf in ("ing", "ed", "es", "s"):
        if len(tok) > len(suf) + 1 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def _normalize_title(s: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace — a stable key for
    comparing two report titles."""
    s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return " ".join(s.split())


def _title_key_terms(s: str) -> set:
    """Significant STEMMED tokens of a title (drop stopwords + 1-char tokens).
    Two-char domain tokens like 'ui'/'be'/'fe'/'dm' are kept."""
    out = set()
    for raw in _normalize_title(s).split():
        if len(raw) < 2:
            continue
        t = _stem(raw)
        if t and t not in _TITLE_STOPWORDS:
            out.add(t)
    return out


def _title_match_score(a: str, b: str) -> float:
    """0..1 similarity between two report titles. 1.0 on normalised equality;
    otherwise the max of Jaccard overlap and (weighted) containment of the smaller
    key-term set in the larger — so "DM images fail to upload" and "images not
    uploading in DMs" score highly while unrelated titles score low. A single
    shared term can never clear the bar (containment counts only at >= 2 overlap),
    so incidental topic words ('Pulse', 'button') don't fuse unrelated reports."""
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = _title_key_terms(a), _title_key_terms(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    jaccard = inter / len(ta | tb)
    smaller = min(len(ta), len(tb))
    # Containment rewards "A's terms are mostly ⊆ B's", but only with real overlap
    # (>= 2 shared terms) AND a non-trivial smaller set (>= 3 terms) — otherwise a
    # generic two-word title ("Login page") would fuse into any longer title that
    # happens to contain both words.
    if inter >= 2 and smaller >= 3:
        return max(jaccard, (inter / smaller) * 0.9)
    return jaccard


def _titles_clearly_match(a: str, b: str) -> bool:
    """True only on an OBVIOUS same-report match (>= TITLE_CLEAR_MATCH). Kept
    conservative on purpose: uncertainty must fall through to 'create', never to
    a wrong merge/comment."""
    return _title_match_score(a, b) >= TITLE_CLEAR_MATCH


def _display(user) -> str:
    return (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or str(user)
    )


# Interrogative / imperative openers that mark a message as a question or command
# the read-only engine should attempt. Used only as a LAST-RESORT guard so a real
# question never falls through to the generic help text because the parser
# under-classified it (e.g. a phrasing it didn't recognise as a standup query).
_QUESTION_LEAD_RE = re.compile(
    r"^\s*(what|who|whose|whom|when|where|why|which|how|is|are|was|were|do|does|"
    r"did|can|could|should|would|will|has|have|had|show|list|tell|give|find|"
    r"lookup|look\s+up|search|status|update|remind|any)\b",
    re.IGNORECASE,
)


def _looks_like_question(text: str) -> bool:
    """Heuristic: does this read like a question/command worth handing to the
    read-only engine? Deliberately liberal — the engine answers honestly ("I
    couldn't find…") when nothing applies, so a false positive is cheap while a
    false negative wrongly bounces the user with the help text."""
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t:
        return True
    return bool(_QUESTION_LEAD_RE.match(t))


def _is_reporter_allowed(author) -> bool:
    """A) Reporter allowlist: pass if author.id is in ALLOWED_REPORTER_IDS or
    the (case-insensitive) display name is in ALLOWED_REPORTER_NAMES."""
    if getattr(author, "id", None) in config.ALLOWED_REPORTER_IDS:
        return True
    name = _display(author).strip().lower()
    if name and name in config.ALLOWED_REPORTER_NAMES:
        return True
    return False


def _format_thread(
    messages: list[discord.Message],
) -> tuple[str, list[str]]:
    """Render the thread as text. Also return image URLs for inline vision."""
    lines: list[str] = []
    image_urls: list[str] = []
    msg_index = {m.id: m for m in messages}

    for m in messages:
        ts = m.created_at.strftime("%Y-%m-%d %H:%M")
        author_name = _display(m.author)
        handle = str(m.author)

        reply_ref = ""
        ref_id = getattr(m.reference, "message_id", None) if m.reference else None
        if ref_id:
            parent = msg_index.get(ref_id)
            reply_ref = (
                f" (reply to {_display(parent.author)})"
                if parent is not None
                else f" (reply to msg {ref_id})"
            )

        mention_parts = [f"@{_display(u)}" for u in m.mentions]
        mention_parts += [f"@{r.name}" for r in getattr(m, "role_mentions", [])]
        mentions_str = (
            f" — mentions: {', '.join(mention_parts)}" if mention_parts else ""
        )

        attach_lines: list[str] = []
        for a in m.attachments:
            ctype = (a.content_type or "").lower()
            kind = ctype.split("/", 1)[0] if "/" in ctype else "file"
            attach_lines.append(f"  - {kind}: {a.filename} <{a.url}>")
            if ctype.startswith("image/") and len(image_urls) < MAX_IMAGES_PER_CLASSIFY:
                image_urls.append(a.url)

        body = m.content.strip() if m.content else "(no text)"
        block = f"[{ts}] {author_name} ({handle}){reply_ref}{mentions_str}:\n{body}"
        if attach_lines:
            block += "\nAttachments:\n" + "\n".join(attach_lines)
        lines.append(block)

    return "\n\n".join(lines), image_urls


def _gather_thread_attachments(
    messages: list[discord.Message],
) -> list[discord.Attachment]:
    out: list[discord.Attachment] = []
    for m in messages:
        out.extend(m.attachments)
    return out


def _format_comment_body(message: discord.Message, *, needs_triage: bool = False) -> str:
    """Render a Discord follow-up as a Linear comment: author, timestamp, text,
    attachment links, optional triage note."""
    ts = message.created_at.strftime("%Y-%m-%d %H:%M UTC")
    author = _display(message.author)
    text = (message.content or "").strip() or "_(no text)_"

    lines: list[str] = [f"**@{author}** at {ts}:", "", text]

    if message.attachments:
        lines.append("")
        lines.append("**Attachments:**")
        for a in message.attachments:
            ctype = (a.content_type or "").lower()
            kind = ctype.split("/", 1)[0] if "/" in ctype else "file"
            lines.append(f"- {kind}: [{a.filename}]({a.url})")

    if needs_triage:
        lines.append("")
        lines.append("⚠️ _Classifier flagged this as needs-triage — please verify._")

    if message.jump_url:
        lines.append("")
        lines.append(f"_From Discord: {message.jump_url}_")

    return "\n".join(lines)


def _split_for_discord(body: str, limit: int = QUERY_REPLY_CHUNK) -> list[str]:
    """Split `body` into Discord-sendable chunks of at most `limit` chars,
    breaking ONLY on line boundaries so we never cut mid-line or mid-sentence.
    A single line longer than `limit` (a giant URL or an unbroken paragraph) is
    hard-wrapped as a last resort. Always returns at least one chunk."""
    body = body or ""
    if len(body) <= limit:
        return [body]

    chunks: list[str] = []
    current = ""
    for line in body.split("\n"):
        # An oversize line can't share a chunk: flush what we have, then emit it
        # in full-width slices, carrying any remainder into `current`.
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                piece = line[i : i + limit]
                if len(piece) == limit:
                    chunks.append(piece)
                else:
                    current = piece
            continue
        # +1 accounts for the "\n" we re-insert between lines.
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class TriageBot(discord.Client):
    def __init__(self) -> None:
        log.info("[bot.init] Setting up Discord intents (message_content, reactions)")
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)

        log.info("[bot.init] Opening SQLite DB at %s", config.DB_PATH)
        self.db = DB(config.DB_PATH)
        log.info("[bot.init] Constructing Classifier (model=%s)", config.CLASSIFIER_MODEL)
        self.classifier = Classifier(config.ANTHROPIC_API_KEY, config.CLASSIFIER_MODEL)
        log.info("[bot.init] Constructing LinearClient (team=%s)", config.LINEAR_TEAM_ID)
        self.linear = LinearClient(config.LINEAR_API_KEY, config.LINEAR_TEAM_ID)
        log.info("[bot.init] Constructing QueryEngine (model=%s)", config.CLASSIFIER_MODEL)
        self.query_engine = QueryEngine(
            config.ANTHROPIC_API_KEY, config.CLASSIFIER_MODEL, self.linear
        )
        # Archive snapshot: loaded + indexed ONCE at startup (read-only fallback).
        log.info("[bot.init] Loading archive snapshot (file=%r)", config.ARCHIVE_FILE)
        self.archive = archive_mod.Archive(config.ARCHIVE_FILE)
        # Short-term per-channel query memory so follow-ups carry their intent.
        log.info(
            "[bot.init] Constructing ConversationMemory (turns=%d, ttl=%dm)",
            config.QUERY_MEMORY_TURNS,
            config.QUERY_MEMORY_TTL_MINUTES,
        )
        self.memory = ConversationMemory(
            max_turns=config.QUERY_MEMORY_TURNS,
            ttl_minutes=config.QUERY_MEMORY_TTL_MINUTES,
        )
        # Background sweeper (started in setup_hook) — nudges aged open threads and
        # proposes clarifications nobody answered. Held so it isn't GC'd mid-flight.
        self._sweeper: Optional[asyncio.Task] = None
        log.info(
            "[bot.init] TriageBot ready (require_approval=%s, clarify=%s, followup=%s, "
            "tag_assignee=%s, escalate=%s → %d PM(s), %d Linear→Discord mapping(s), "
            "allowlist: %d ids + %d names)",
            config.REQUIRE_APPROVAL,
            config.COS_CLARIFY_ENABLED,
            config.COS_FOLLOWUP_ENABLED,
            config.COS_TAG_ASSIGNEE_ENABLED,
            config.COS_ESCALATE_ENABLED,
            len(config.ESCALATION_USER_IDS),
            len(config.LINEAR_TO_DISCORD),
            len(config.ALLOWED_REPORTER_IDS),
            len(config.ALLOWED_REPORTER_NAMES),
        )

    async def setup_hook(self) -> None:
        """Start the periodic sweeper once, on the bot's own event loop. It services
        every proactive path — open threads, timed-out clarifications, and the Linear
        gap audit — so it runs whenever ANY of them is enabled."""
        if not (
            config.COS_FOLLOWUP_ENABLED
            or config.COS_CLARIFY_ENABLED
            or config.COS_TAG_ASSIGNEE_ENABLED
            or config.COS_ESCALATE_ENABLED
            or config.COS_UPDATE_DEADLINE_ENABLED
        ):
            log.info("[sweep] every proactive path is disabled; no sweeper")
            return
        self._sweeper = self.loop.create_task(self._sweep_loop())
        log.info(
            "[sweep] sweeper scheduled (every %d min)",
            config.COS_FOLLOWUP_CHECK_INTERVAL_MINUTES,
        )

    async def on_ready(self) -> None:
        query_channel = config.query_channel_id()
        query_desc = (
            f"{query_channel} (dedicated)"
            if config.QUERY_CHANNEL_ID
            else f"{query_channel} (approval-channel fallback)"
        )
        log.info(
            "Logged in as %s. Monitoring %d channels, approvals→%s, queries→%s. require_approval=%s",
            self.user,
            len(config.MONITORED_CHANNEL_IDS),
            config.APPROVAL_CHANNEL_ID,
            query_desc,
            config.REQUIRE_APPROVAL,
        )

    # -- message ingest --------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        # Drop bot authors first — covers self-messages too, so query mode
        # can never recurse on the bot's own replies.
        if message.author.bot:
            log.debug("on_message msg=%s dropped: bot author (%s)", message.id, message.author)
            return

        # QUERY MODE — must run BEFORE the report pipeline so a question can
        # never become a ticket. Trigger: any human message in the dedicated
        # query channel, or an explicit @-mention of self in a monitored channel
        # (or the approval channel in fallback mode). Replies in the same
        # channel. Read-only — never creates or modifies anything.
        #
        # NOTE: query mode deliberately runs BEFORE the reporter allowlist — any
        # user may ask the bot a question. `on_raw_message_edit` reuses the exact
        # same gate (`_is_query_trigger`) so an edited question re-fires for the
        # same set of messages that would have triggered a query when first sent.
        if self._is_query_trigger(message):
            handled = await self._handle_query(message)
            if handled:
                return
            # Fell through: `_handle_query` judged this a REPORT (a bug/feature being
            # filed at the bot), not a greeting and not a question. A monitored
            # channel takes it into the report pipeline below. A query-only channel
            # has no report path — so we answer in the persona's voice rather than
            # dumping a fixed "Try `...`" template. `_handle_query` already handles
            # greetings and unclear messages itself, so reaching this in a query-only
            # channel is rare (a parse failure, or a report posted here).
            if config.is_query_only_channel(message.channel.id):
                log.info(
                    "[route] msg=%s type=report/unhandled in query-only channel "
                    "→ route=capability (persona, last resort)",
                    message.id,
                )
                await self._send_social_reply(message, "unclear")
                return

        # HARD RULE: the query channel is query-only. Even if it accidentally
        # overlaps a monitored channel, classification / ticket creation NEVER
        # runs on messages here.
        if config.is_query_only_channel(message.channel.id):
            log.debug(
                "on_message msg=%s dropped: query-only channel %s (no triage)",
                message.id,
                message.channel.id,
            )
            return

        # B) Drop non-monitored channels at debug-level so the terminal stays
        # readable. Query/approval channels end here unless a query handled them.
        if message.channel.id not in config.MONITORED_CHANNEL_IDS:
            log.debug(
                "on_message msg=%s dropped: channel %s not monitored",
                message.id,
                message.channel.id,
            )
            return

        channel_name = getattr(message.channel, "name", "?")
        log.debug(
            "on_message msg=%s channel=#%s(%s) author=%s len=%d attachments=%d",
            message.id,
            channel_name,
            message.channel.id,
            message.author,
            len(message.content or ""),
            len(message.attachments),
        )

        # CHIEF-OF-STAFF PATHS — these run BEFORE the reporter allowlist on purpose.
        # The allowlist governs whose reports become TICKETS; it has nothing to say
        # about who may answer a question the bot asked, or who may promise the
        # channel an update. Anyone on the team can do both.

        # 1) Did this close an open thread we were waiting on? (a reply to a nudge,
        # or to the promise itself). Doesn't consume the message — a follow-up can
        # also be a report, so we close and fall through to triage.
        await self._maybe_close_open_thread(message)

        # 2) Is this the answer to a clarifying question we asked? If so it's
        # consumed here: we re-plan the whole thread WITH the answer and propose
        # the ticket, rather than triaging the answer as a report of its own.
        if await self._maybe_resume_clarification(message):
            log.info("[on_message] msg=%s consumed as a clarification answer", message.id)
            return

        # 3) Did someone just promise to come back with something? Track it so it
        # can be surfaced back to them later. Never consumes the message.
        await self._maybe_track_commitment(message)

        # A) Reporter allowlist — debug-only drop.
        if not _is_reporter_allowed(message.author):
            log.debug(
                "msg=%s dropped: reporter id=%s name=%r not in allowlist",
                message.id,
                getattr(message.author, "id", None),
                _display(message.author),
            )
            return

        log.info(
            "msg=%s received #%s(%s) author=%s(%s) len=%d attachments=%d",
            message.id,
            channel_name,
            message.channel.id,
            _display(message.author),
            getattr(message.author, "id", "?"),
            len(message.content or ""),
            len(message.attachments),
        )

        # Length floor (cheap pre-filter, attachments exempt it).
        if (
            len(message.content) < config.MIN_MESSAGE_LENGTH
            and not message.attachments
        ):
            log.info(
                "msg=%s dropped: content too short (len=%d < %d) and no attachments",
                message.id,
                len(message.content),
                config.MIN_MESSAGE_LENGTH,
            )
            return

        # Dedup — same Discord message id, never twice.
        if self.db.already_processed(message.id):
            log.info("msg=%s dropped: already processed (pre-delay)", message.id)
            return

        # Optional delay for follow-up clarifications to land in the same thread.
        if config.CLASSIFY_DELAY_SECONDS > 0:
            await asyncio.sleep(config.CLASSIFY_DELAY_SECONDS)
            if self.db.already_processed(message.id):
                log.info(
                    "msg=%s dropped: already processed after %.1fs delay",
                    message.id,
                    config.CLASSIFY_DELAY_SECONDS,
                )
                return
            try:
                message = await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.Forbidden) as e:
                log.info(
                    "msg=%s dropped: re-fetch after delay failed (%s)",
                    message.id,
                    type(e).__name__,
                )
                return

        log.info("[on_message] msg=%s collecting thread context...", message.id)
        thread = await self._collect_thread_context(message)
        log.info("[on_message] msg=%s thread context: %d messages", message.id, len(thread))

        thread_text, image_urls = _format_thread(thread)
        reporter = _display(thread[0].author) if thread else _display(message.author)
        participants = sorted({_display(m.author) for m in thread})

        log.info(
            "Classifying msg=%s reporter=%s channel=#%s thread=%d images=%d",
            message.id,
            reporter,
            channel_name,
            len(thread),
            len(image_urls),
        )

        verdict = await self.classifier.classify(
            content=thread_text,
            author=reporter,
            channel=channel_name,
            image_urls=image_urls,
            participants=participants,
        )
        if verdict is None:
            log.info("msg=%s dropped: classifier returned None", message.id)
            return
        log.info(
            "[on_message] msg=%s verdict: cat=%s needs_triage=%s is_new=%s signal=%s areas=%s mentions=%d pri=%s conf=%.2f title=%r",
            message.id,
            verdict["category"],
            verdict["needs_triage"],
            verdict["is_new_issue"],
            verdict["status_signal"],
            verdict["area_labels"],
            len(verdict["mentioned_assignees"]),
            verdict["priority"],
            verdict["confidence"],
            verdict["title"],
        )

        # C) noise → do nothing.
        if verdict["category"] == "noise":
            log.info("msg=%s → noise; dropping (no embed, no ticket)", message.id)
            return

        # C) Confidence floor — bypassed when needs_triage=true so we never
        # silently drop a "plausibly actionable but uncertain" item.
        if (
            verdict["confidence"] < config.MIN_CONFIDENCE
            and not verdict["needs_triage"]
        ):
            log.info(
                "msg=%s → %s but confidence %.2f < %.2f and not needs_triage; dropping",
                message.id,
                verdict["category"],
                verdict["confidence"],
                config.MIN_CONFIDENCE,
            )
            return

        log.info("[on_message] msg=%s deciding plan...", message.id)
        plan = await self._decide_plan(message=message, thread=thread, verdict=verdict)
        if plan is None:
            log.warning("msg=%s _decide_plan returned None; dropping", message.id)
            return
        log.info(
            "[on_message] msg=%s plan: kind=%s target=%s assignee=%s labels=%s",
            message.id,
            plan["kind"],
            plan.get("target_issue_identifier")
            or plan.get("target_issue_id")
            or plan.get("title"),
            plan.get("assignee_id") or plan.get("assignee_display") or "(unassigned)",
            plan.get("label_names") or [],
        )

        # E.5) CLARIFY — would this ticket be half-complete? If so, ask the reporter the
        # few things genuinely missing (bundled into ONE message) instead of proposing it,
        # and park the plan until the answer lands (or the ask times out and the sweeper
        # proposes it flagged needs-triage). Nothing is filed here.
        if await self._maybe_ask_clarification(message, thread, plan):
            log.info(
                "[on_message] msg=%s clarification asked; plan parked (no embed yet)",
                message.id,
            )
            return

        # F) Approval gate.
        await self._propose_plan(message, plan)

        log.info("[on_message] msg=%s DONE", message.id)

    async def _propose_plan(
        self, message: discord.Message, plan: dict
    ) -> Optional[int]:
        """F) The approval gate — the ONLY way a plan ever reaches Linear. Shared by
        the normal triage path and the two Chief-of-Staff paths that defer a proposal
        (a clarification that got answered, and one that timed out unanswered), so a
        deferred report is proposed on exactly the same terms as an immediate one —
        same dedup, same embed, same ✅/❌.

        Returns the id this report is now tracked under — the approval embed's id
        (or the canonical one it was merged into), or, when REQUIRE_APPROVAL is off,
        the source message id that `_execute_immediately` files it under. None if the
        proposal couldn't be posted. Callers use it to link a follow-on message (the
        answer to a clarifying question) to the same report."""
        if config.REQUIRE_APPROVAL:
            # STAGE-1 DEDUP: fold a duplicate report into an existing pending
            # approval instead of posting a second embed (a create plan only).
            merged_into = await self._maybe_merge_into_pending(message, plan)
            if merged_into is not None:
                log.info(
                    "[propose] msg=%s merged into pending approval=%s; no new embed",
                    message.id, merged_into,
                )
                return int(merged_into)
            approval_msg = await self._post_for_approval(message, plan)
            return approval_msg.id if approval_msg else None

        await self._execute_immediately(message, plan)
        # The auto-execute path files the row under the source message id.
        return message.id

    async def on_raw_message_edit(
        self, payload: discord.RawMessageUpdateEvent
    ) -> None:
        """Reconsider an EDITED message — QUERY MODE ONLY, never the report
        pipeline. If the edited text reads as a question addressed to the bot,
        we re-run the same query path on the NEW text and post a FRESH reply
        (a new message — we never edit the old answer). If the edit is an
        ordinary message (a report or anything else), we do NOTHING: editing a
        bug report must not create a second ticket or touch the create/comment/
        status pipeline.

        Raw (not on_message_edit) so edits to messages the bot hasn't cached
        still fire. Reuses the exact on_message query gate (`_is_query_trigger`)
        and the existing `_handle_query` path — no duplicated logic."""
        # Cheap early-out before any network fetch: only the channels query mode
        # listens on (monitored channels OR the query channel).
        if not self._listens_for_query(payload.channel_id):
            return

        # Resolve the channel — prefer the cache, but fall back to REST: a cache
        # miss here was silently swallowing edits and dropping the re-trigger.
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.DiscordException as e:
                log.info(
                    "[edit] msg=%s channel %s unresolvable (%s); skip",
                    payload.message_id, payload.channel_id, type(e).__name__,
                )
                return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.DiscordException as e:
            log.info(
                "[edit] msg=%s fetch after edit failed (%s); skip",
                payload.message_id, type(e).__name__,
            )
            return

        # Skip non-content edits (embeds resolving, pins, etc.) — nothing new to
        # reconsider when we can prove the text is unchanged.
        cached = payload.cached_message
        if cached is not None and cached.content == message.content:
            log.debug("[edit] msg=%s content unchanged; skip", message.id)
            return

        log.info(
            "[edit] detected edit msg=%s author=%s(%s) channel=#%s len=%d",
            message.id,
            _display(message.author),
            getattr(message.author, "id", "?"),
            getattr(message.channel, "name", "?"),
            len(message.content or ""),
        )

        # QUERY MODE ONLY — same gate as on_message. If the edited text isn't a
        # question addressed to the bot, do NOTHING: edits are NEVER routed into
        # the create/comment/status pipeline (editing a report can't spawn a
        # second ticket).
        if not self._is_query_trigger(message):
            log.debug(
                "[edit] msg=%s not a bot query; ignoring (no report path for edits)",
                message.id,
            )
            return

        log.info("[edit] msg=%s re-running QUERY MODE on edited text", message.id)
        handled = await self._handle_query(message)
        if not handled:
            log.info(
                "[edit] msg=%s edited text parsed as not-a-Linear-question; no reply",
                message.id,
            )

    # -- thread / context (unchanged) ------------------------------------

    async def _collect_thread_context(
        self, source: discord.Message
    ) -> list[discord.Message]:
        """Gather the tagged message plus everything associated with it:
        reply-chain ancestors, descendants (replies to anything in the set),
        and — when the source is in a Discord Thread — its siblings.
        """
        by_id: dict[int, discord.Message] = {source.id: source}
        channel = source.channel

        current = source
        for _ in range(CONTEXT_REPLY_DEPTH):
            ref = current.reference
            ref_id = getattr(ref, "message_id", None) if ref else None
            if not ref_id or ref_id in by_id:
                break
            try:
                parent = await channel.fetch_message(ref_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.debug("Stopped walking reply chain at %s: %s", ref_id, e)
                break
            by_id[parent.id] = parent
            current = parent

        try:
            if isinstance(channel, discord.Thread):
                async for msg in channel.history(
                    limit=CONTEXT_HISTORY_LIMIT, oldest_first=True
                ):
                    by_id.setdefault(msg.id, msg)
            else:
                async for msg in channel.history(limit=CONTEXT_HISTORY_LIMIT):
                    ref = msg.reference
                    ref_id = getattr(ref, "message_id", None) if ref else None
                    if ref_id and ref_id in by_id:
                        by_id.setdefault(msg.id, msg)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.debug("Could not scan channel history for related msgs: %s", e)

        return sorted(by_id.values(), key=lambda m: m.created_at)

    # -- planning --------------------------------------------------------

    async def _decide_plan(
        self,
        *,
        message: discord.Message,
        thread: list[discord.Message],
        verdict: dict,
    ) -> Optional[dict]:
        """D) Pick create vs comment, build a fully-baked plan dict ready for
        approval/execution. Plan is JSON-serialisable so it can round-trip
        through `processed.classification_json`."""
        is_new_issue = bool(verdict.get("is_new_issue", True))
        needs_triage = bool(verdict.get("needs_triage", False))
        status_signal = verdict.get("status_signal", "none")
        category = verdict["category"]

        log.info(
            "[plan] step 1/3: is_new=%s needs_triage=%s signal=%s",
            is_new_issue,
            needs_triage,
            status_signal,
        )

        base = {
            "title": verdict["title"],
            "category": category,
            "priority": verdict["priority"],
            "confidence": verdict["confidence"],
            "needs_triage": needs_triage,
            "is_new_issue": is_new_issue,
            "status_signal": status_signal,
            "source_message_id": message.id,
            "source_channel_id": message.channel.id,
            "source_jump_url": message.jump_url or "",
            "reporter_name": _display(message.author),
            "reporter_id": getattr(message.author, "id", 0),
        }

        # Path A: COMMENT on the issue linked to an earlier thread message.
        # Only when classifier says this is the SAME thing as its parent.
        log.info("[plan] step 2/3: looking for parent linkage in thread")
        parent = self._find_parent_linkage(thread, message)
        if parent and not is_new_issue:
            log.info(
                "[plan] parent linkage hit → comment on linear=%s (signal=%s)",
                parent["linear_issue_id"],
                status_signal,
            )
            comment_body = _format_comment_body(message, needs_triage=needs_triage)
            # Only "resolved"/"in_progress" carry a status move (→ Implemented /
            # In Progress). "cannot_reproduce" and "none" stay comment-only — per
            # convention, cancellation is a PM decision that needs a reason.
            kind = (
                "comment_transition"
                if status_signal in ("resolved", "in_progress")
                else "comment"
            )
            parent_title = (parent.get("classification") or {}).get("title", "")
            return {
                **base,
                "kind": kind,
                "target_issue_id": parent["linear_issue_id"],
                "target_issue_identifier": None,
                "target_issue_title": parent_title,
                "target_issue_url": None,
                "comment_body": comment_body,
            }

        # Path A2: SAME-CHANNEL follow-up that isn't a Discord reply. A clear title
        # match to an issue a recent same-channel message already tracks means this
        # is another mention of it — comment instead of opening a duplicate. A clear
        # match overrides the classifier's is_new_issue guess (a plain follow-up is
        # usually mis-read as "new").
        chan_issue = await self._find_channel_linkage(message, verdict)
        if chan_issue:
            log.info(
                "[plan] channel linkage hit → comment on %s '%s'",
                chan_issue.get("identifier"), chan_issue.get("title"),
            )
            comment_body = _format_comment_body(message, needs_triage=needs_triage)
            return {
                **base,
                "kind": "comment_dup",
                "target_issue_id": chan_issue["id"],
                "target_issue_identifier": chan_issue.get("identifier"),
                "target_issue_title": chan_issue.get("title"),
                "target_issue_url": chan_issue.get("url"),
                "comment_body": comment_body,
            }

        # Path B: heading to create — check for an open duplicate first.
        log.info("[plan] step 3/3: no parent linkage applied; checking for open dup")
        dup = await self._find_open_duplicate(verdict["title"])
        if dup:
            log.info(
                "[plan] clear-match dup → comment_dup target=%s '%s'",
                dup.get("identifier"),
                dup.get("title"),
            )
            comment_body = _format_comment_body(message, needs_triage=needs_triage)
            return {
                **base,
                "kind": "comment_dup",
                "target_issue_id": dup["id"],
                "target_issue_identifier": dup.get("identifier"),
                "target_issue_title": dup.get("title"),
                "target_issue_url": dup.get("url"),
                "comment_body": comment_body,
            }

        # Path C: CREATE.
        log.info("[plan] no dup → CREATE (needs_triage=%s)", needs_triage)

        # E) Labels first: category → label name + area_labels, in that order.
        # Only the six real labels can result (category map + BE/FE/UI areas).
        label_names: list[str] = []
        cat_label = _CATEGORY_LABEL_NAME.get(category)
        if cat_label:
            label_names.append(cat_label)
        for area in verdict.get("area_labels") or []:
            if area and area not in label_names:
                label_names.append(area)

        # Convention: a Bug ALWAYS pairs a system label (BE/FE/UI) showing where
        # the fix lives. If the classifier didn't supply one we can't invent it,
        # so route to PM triage (unassigned + flagged) rather than guess.
        if category == "bug" and not (_SYSTEM_LABELS & set(label_names)):
            log.info("[plan] bug lacks a BE/FE/UI system label → forcing needs_triage")
            needs_triage = True

        mentioned_names = list(verdict.get("mentioned_assignees") or [])
        assignee_id, intended_name, extras = await self._resolve_assignee_for_plan(
            thread=thread,
            mentioned_names=mentioned_names,
            needs_triage=needs_triage,
            category=category,
        )

        description = self._build_new_issue_description(
            verdict=verdict,
            reporter=_display(message.author),
            source_jump=message.jump_url,
            source_text=message.content or "",
            attachments=_gather_thread_attachments(thread),
            thread=thread,
            needs_triage=needs_triage,
            intended_assignee=intended_name,
            extras=extras,
            category=category,
        )

        return {
            **base,
            "needs_triage": needs_triage,
            "kind": "create_needs_triage" if needs_triage else "create",
            "description": description,
            "label_names": label_names,
            "assignee_id": assignee_id,
            "assignee_display": intended_name,
        }

    def _find_parent_linkage(
        self,
        thread: list[discord.Message],
        message: discord.Message,
    ) -> Optional[dict]:
        """Most recent earlier message in the thread that has a stored Linear
        issue id, or None."""
        candidates = [
            m for m in thread
            if m.id != message.id and m.created_at < message.created_at
        ]
        candidates.sort(key=lambda m: m.created_at, reverse=True)
        for m in candidates:
            try:
                linkage = self.db.get_linkage_for_message(m.id)
            except Exception:
                log.exception("[plan] get_linkage_for_message failed for %s", m.id)
                continue
            if linkage:
                return linkage
        return None

    async def _find_open_duplicate(self, title: str) -> Optional[dict]:
        """Conservative duplicate detector: search Linear (open issues first) and
        return the single best OPEN issue whose title CLEARLY matches `title`
        (>= TITLE_CLEAR_MATCH, not just exact equality — the same bug is often
        worded differently by different reporters). Falls through to "no dup" on
        any error or ambiguity — we'd rather create a near-duplicate than dump a
        real new issue onto an unrelated old one."""
        if not _normalize_title(title):
            return None

        # search_issues already swallows its own errors → []
        hits = await self.linear.search_issues(title)
        if not hits:
            return None

        try:
            states = await self.linear.list_team_states()
        except Exception:
            log.exception(
                "[duplicate] list_team_states raised; can't classify open/closed; skipping dup",
            )
            return None
        open_names = {s["name"] for s in states if s.get("type") in _OPEN_STATE_TYPES}
        if not open_names:
            log.debug("[duplicate] team has no open-typed states; skipping dup")
            return None

        best: Optional[dict] = None
        best_score = 0.0
        for hit in hits:
            if hit.get("state") not in open_names:
                continue
            score = _title_match_score(title, hit.get("title") or "")
            if score >= TITLE_CLEAR_MATCH and score > best_score:
                best, best_score = hit, score
        if best is not None:
            log.info(
                "[duplicate] clear match (score=%.2f): %s '%s' state=%s",
                best_score, best.get("identifier"), best.get("title"), best.get("state"),
            )
            return best
        log.debug("[duplicate] %d hits, none clear-matched an open issue", len(hits))
        return None

    async def _find_channel_linkage(
        self, message: discord.Message, verdict: dict
    ) -> Optional[dict]:
        """STAGE-2 DEDUP. A follow-up that ISN'T a Discord reply (a plain new
        message in the channel) can still be about an already-tracked issue.
        Scan recently tracked messages in the SAME channel (last
        DEDUP_WINDOW_HOURS) and, on a CLEAR title match, return the OPEN issue
        they're linked to — resolved live so we have its human identifier / url /
        state. Conservative: only a clear match, and only while the issue is still
        open. Returns the issue dict (id, identifier, url, title, state) or None."""
        title = verdict.get("title") or ""
        if not _normalize_title(title):
            return None
        try:
            linked = self.db.list_recent_linked(message.channel.id, self._dedup_since())
        except Exception:
            log.exception("[plan] list_recent_linked failed")
            return None

        checked: set = set()
        for row in linked:
            internal_id = row.get("linear_issue_id")
            if not internal_id or internal_id in checked:
                continue
            cls = row.get("classification") or {}
            stored_title = cls.get("title") or cls.get("target_issue_title") or ""
            if not _titles_clearly_match(title, stored_title):
                continue
            checked.add(internal_id)
            # Resolve live to confirm it's still OPEN and to get identifier/url.
            try:
                issue = await self.linear.get_issue(internal_id)
            except Exception:
                log.exception("[plan] get_issue failed for channel-linked %s", internal_id)
                continue
            if not issue:
                continue
            if issue.get("state_type") not in _OPEN_STATE_TYPES:
                log.info(
                    "[plan] channel-linked %s matched but state=%s is closed; not redirecting",
                    issue.get("identifier"), issue.get("state"),
                )
                continue
            log.info(
                "[dedup] STAGE-2 channel linkage clear-match → comment on %s "
                "(score=%.2f, %r ~ %r)",
                issue.get("identifier"),
                _title_match_score(title, stored_title), title, stored_title,
            )
            return issue
        return None

    async def _resolve_assignee_for_plan(
        self,
        *,
        thread: list[discord.Message],
        mentioned_names: list[str],
        needs_triage: bool,
        category: str = "",
    ) -> tuple[Optional[str], Optional[str], list[str]]:
        """Returns (assignee_id, intended_name, extras).

        - No mentions → (None, None, []).
        - needs_triage → force unassigned, surface ALL mentions as extras.
        - Convention: NEVER assign a BUG to QA (Harsh). If the primary mention is
          Harsh on a bug, leave it unassigned (surfaced as intended) for PM triage.
        - Otherwise: try mentioned_names[0]. On resolve → (id, None, rest).
                     On miss → (None, primary, rest). Don't fall through to
                     mentioned_names[1] (per spec)."""
        if not mentioned_names:
            return (None, None, [])
        if needs_triage:
            return (None, None, list(mentioned_names))

        primary = mentioned_names[0]
        extras = list(mentioned_names[1:])

        if category == "bug" and primary.strip().lower() in _NEVER_BUG_ASSIGNEE_NAMES:
            log.info(
                "[plan] primary mention %r is QA — never assign a bug to Harsh; "
                "leaving unassigned for PM triage",
                primary,
            )
            return (None, primary, extras)

        name_to_id = self._build_thread_name_id_map(thread)
        primary_id = name_to_id.get(primary.strip().lower(), 0)

        try:
            assignee_id = await self.linear.resolve_assignee(
                discord_user_id=primary_id,
                display_name=primary,
                discord_linear_map=config.DISCORD_LINEAR_MAP,
            )
        except Exception:
            log.exception("[plan] resolve_assignee raised for %r", primary)
            assignee_id = None

        if assignee_id:
            return (assignee_id, None, extras)
        return (None, primary, extras)

    def _build_thread_name_id_map(
        self, thread: list[discord.Message]
    ) -> dict[str, int]:
        """Lower-cased display-name → Discord user id, harvested from all
        @-mentions and authors in the thread. Used to feed
        `resolve_assignee` a Discord id for the primary mention."""
        out: dict[str, int] = {}
        for m in thread:
            for u in m.mentions:
                key = _display(u).strip().lower()
                if key:
                    out.setdefault(key, u.id)
            author_key = _display(m.author).strip().lower()
            author_id = getattr(m.author, "id", 0)
            if author_key and author_id:
                out.setdefault(author_key, author_id)
        return out

    def _build_new_issue_description(
        self,
        *,
        verdict: dict,
        reporter: str,
        source_jump: str,
        source_text: str,
        attachments: list[discord.Attachment],
        thread: list[discord.Message],
        needs_triage: bool,
        intended_assignee: Optional[str],
        extras: list[str],
        category: str = "",
    ) -> str:
        """Render the Linear issue body in the team's convention shape. For a bug:
        What was reported / (repro + expected-vs-actual are folded into the
        classifier restatement) / the reporter's own words / Screenshots &
        recordings (every image AND video link) / Raised by / a Needs-triage note.
        Feature/improvement uses a lighter "What's being asked" lead — the PM
        expands it into the full project template later."""
        is_bug = category == "bug"
        lead_label = "What was reported" if is_bug else "What's being asked"
        body = verdict["description"].strip() or "_(no description)_"
        parts: list[str] = [f"**{lead_label}:**", body]

        # The reporter's own words — kept verbatim so meaning isn't paraphrased away.
        if len(thread) > 1:
            thread_text, _ = _format_thread(thread)
            parts += ["", "**Reporter's words (thread):**", "```", thread_text, "```"]
        else:
            snippet = source_text.strip()
            if len(snippet) > 1500:
                snippet = snippet[:1497] + "…"
            if snippet:
                parts += ["", "**Reporter's words:**", f"> {snippet}"]

        # Every image AND video (and any other) attachment link — never dropped.
        if attachments:
            parts += ["", "**Screenshots & recordings:**"]
            for a in attachments:
                ctype = (a.content_type or "").lower()
                kind = ctype.split("/", 1)[0] if "/" in ctype else "file"
                parts.append(f"- {kind}: [{a.filename}]({a.url})")

        parts += ["", "---", f"**Raised by:** @{reporter}"]
        if source_jump:
            parts.append(f"**Source:** {source_jump}")

        if intended_assignee:
            parts += [
                "",
                f"_Intended assignee:_ **@{intended_assignee}** "
                f"(no matching Linear user — left unassigned)",
            ]
        if extras:
            parts += ["", "_Also mentioned:_ " + ", ".join(f"**@{x}**" for x in extras)]

        if needs_triage:
            parts += [
                "",
                "⚠️ **Needs triage** — verify the category / system label and (re)assign "
                "(PM). For a bug, confirm the BE/FE/UI label showing where the fix lives.",
            ]

        return "\n".join(parts)

    # -- approval flow ----------------------------------------------------

    def _dedup_since(self) -> str:
        """SQLite 'YYYY-MM-DD HH:MM:SS' UTC lower bound for the dedup window."""
        return (
            datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)
        ).strftime("%Y-%m-%d %H:%M:%S")

    async def _maybe_merge_into_pending(
        self, source: discord.Message, plan: dict
    ) -> Optional[int]:
        """STAGE-1 DEDUP. Before posting a NEW approval embed for a create plan,
        look for an existing PENDING approval in the SAME channel (last
        DEDUP_WINDOW_HOURS) whose proposed report clearly matches this one. On a
        match we do NOT post a second embed: record this message as merged into
        that approval (so ✅ later links it to the same issue and it's never
        re-classified) and annotate the existing embed ("also reported by @X").
        Returns the canonical approval_message_id on a merge, else None.

        Only create plans are deduped here — a comment plan already targets a real
        issue, so it can't spawn a duplicate ticket."""
        if plan.get("kind") not in ("create", "create_needs_triage"):
            return None
        try:
            pendings = self.db.list_recent_pending(source.channel.id, self._dedup_since())
        except Exception:
            log.exception("[dedup] list_recent_pending failed; posting normally")
            return None

        title = plan.get("title") or ""
        for p in pendings:
            cls = p.get("classification") or {}
            if cls.get("kind") not in ("create", "create_needs_triage"):
                continue
            other_title = cls.get("title") or ""
            if not _titles_clearly_match(title, other_title):
                continue
            approval_id = p.get("approval_message_id")
            log.info(
                "[dedup] STAGE-1 SUPPRESS embed: msg=%s matches pending approval=%s "
                "(score=%.2f, %r ~ %r) — merging, NOT posting a second embed",
                source.id, approval_id, _title_match_score(title, other_title),
                title, other_title,
            )
            try:
                self.db.record_merged(
                    message_id=source.id,
                    channel_id=source.channel.id,
                    classification=plan,
                    approval_message_id=int(approval_id),
                )
            except Exception:
                log.exception("[dedup] record_merged failed for msg=%s", source.id)
            await self._annotate_also_reported(int(approval_id), source, plan)
            return int(approval_id)
        return None

    async def _annotate_also_reported(
        self, approval_message_id: int, source: discord.Message, plan: dict
    ) -> None:
        """Add/extend an 'Also reported by' field on an existing approval embed so
        the reviewer can see the duplicate report was folded in. Best-effort."""
        channel = self.get_channel(config.APPROVAL_CHANNEL_ID)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(approval_message_id)
        except discord.DiscordException:
            log.exception("[dedup] couldn't fetch approval embed %s to annotate", approval_message_id)
            return
        if not msg.embeds:
            return
        embed = msg.embeds[0]
        reporter = plan.get("reporter_name", "?")
        jump = plan.get("source_jump_url") or getattr(source, "jump_url", "") or ""
        mention = f"@{reporter}"
        entry = mention + (f" ([jump]({jump}))" if jump else "")

        field_name = "Also reported by"
        idx = next(
            (i for i, f in enumerate(embed.fields) if f.name == field_name), None
        )
        if idx is None:
            embed.add_field(name=field_name, value=entry[:1024], inline=False)
        else:
            old = embed.fields[idx].value or ""
            if mention in old:  # same reporter already noted — nothing to add
                return
            embed.set_field_at(
                idx, name=field_name, value=(old + "\n" + entry)[:1024], inline=False
            )
        try:
            await msg.edit(embed=embed)
        except discord.DiscordException:
            log.exception("[dedup] failed to edit approval embed %s", approval_message_id)

    def _comment_body_from_plan(self, plan: dict) -> str:
        """Build a Linear comment body from a create plan when we redirect that
        create onto an existing issue (idempotency re-check). Reuses the plan's
        already-rendered report body so nothing the reporter said is lost."""
        reporter = plan.get("reporter_name", "?")
        jump = plan.get("source_jump_url") or ""
        parts = [f"**@{reporter}** reported what looks like the same issue:", ""]
        desc = (plan.get("description") or "").strip()
        if desc:
            parts.append(desc if len(desc) <= 3500 else desc[:3497] + "…")
        if jump and jump not in desc:
            parts += ["", f"_From Discord: {jump}_"]
        return "\n".join(parts)

    async def _post_for_approval(
        self, source: discord.Message, plan: dict
    ) -> Optional[discord.Message]:
        return await self._send_approval_embed(
            plan, source_message_id=source.id, source_channel_id=source.channel.id
        )

    async def _send_approval_embed(
        self, plan: dict, *, source_message_id: int, source_channel_id: int
    ) -> Optional[discord.Message]:
        """Post the ✅/❌ embed and record the report as pending under it. Takes the
        source ids rather than the Message so the sweeper can still propose a parked
        plan whose original message is no longer fetchable (deleted, or in a channel
        we've since lost access to) — a report is never dropped for want of a live
        Message object."""
        log.info(
            "[_post_for_approval] step 1/3: looking up approval channel %s",
            config.APPROVAL_CHANNEL_ID,
        )
        channel = self.get_channel(config.APPROVAL_CHANNEL_ID)
        if channel is None:
            log.error(
                "[_post_for_approval] approval channel %s not visible — check APPROVAL_CHANNEL_ID",
                config.APPROVAL_CHANNEL_ID,
            )
            return None

        log.info("[_post_for_approval] step 2/3: building + sending embed (kind=%s)", plan["kind"])
        embed = self._build_plan_embed(plan)
        try:
            msg = await channel.send(embed=embed)
        except discord.DiscordException:
            log.exception("[_post_for_approval] failed to send approval embed")
            return None
        try:
            await msg.add_reaction(APPROVE_EMOJI)
            await msg.add_reaction(REJECT_EMOJI)
        except discord.DiscordException:
            log.exception("[_post_for_approval] failed to add reactions to %s", msg.id)

        log.info("[_post_for_approval] step 3/3: recording pending (approval_msg=%s)", msg.id)
        try:
            self.db.record_pending(
                message_id=source_message_id,
                channel_id=source_channel_id,
                classification=plan,
                approval_message_id=msg.id,
            )
        except Exception:
            log.exception(
                "[_post_for_approval] DB record_pending failed for msg=%s",
                source_message_id,
            )
        log.info(
            "[_post_for_approval] DONE — awaiting reaction on approval_msg=%s", msg.id
        )
        return msg

    def _action_text(self, plan: dict) -> str:
        """F) Human-readable action statement for the embed / confirmation."""
        kind = plan["kind"]
        target = (
            plan.get("target_issue_identifier")
            or (f"'{plan.get('target_issue_title')}'" if plan.get("target_issue_title") else None)
            or "an existing issue"
        )
        if kind == "create":
            return "Create issue"
        if kind == "create_needs_triage":
            return "Create (needs triage, unassigned)"
        if kind == "comment":
            return f"Comment on {target}"
        if kind == "comment_transition":
            state_name = SIGNAL_TO_STATE_NAME.get(plan.get("status_signal", ""))
            if state_name:
                return f"Comment + move {target} → {state_name} (never Done)"
            return f"Comment on {target}"
        if kind == "comment_dup":
            return f"Comment on {target} (matched as duplicate)"
        return kind

    def _build_plan_embed(self, plan: dict) -> discord.Embed:
        kind = plan["kind"]
        category = plan.get("category", "")
        title = plan.get("title", "(untitled)")

        if kind == "create":
            icon = CATEGORY_ICON.get(category, "📝")
            color = CATEGORY_COLOR.get(category, 0x888888)
        elif kind == "create_needs_triage":
            icon = "⚠️ " + CATEGORY_ICON.get(category, "📝")
            color = _TRIAGE_COLOR
        else:
            icon = "💬"
            color = _COMMENT_COLOR

        body = plan.get("description") or plan.get("comment_body") or "_(no body)_"
        if len(body) > 4000:
            body = body[:3997] + "…"
        full_title = f"{icon} {title}".strip()
        if len(full_title) > 256:
            full_title = full_title[:253] + "…"

        embed = discord.Embed(
            title=full_title,
            description=body,
            color=color,
            url=plan.get("source_jump_url") or None,
        )
        embed.add_field(name="Action", value=f"**{self._action_text(plan)}**", inline=False)

        if kind in ("create", "create_needs_triage"):
            embed.add_field(name="Category", value=category or "—", inline=True)
        embed.add_field(name="Priority", value=plan.get("priority", "?"), inline=True)
        if plan.get("confidence") is not None:
            embed.add_field(
                name="Confidence",
                value=f"{plan['confidence']:.0%}",
                inline=True,
            )
        if plan.get("label_names"):
            embed.add_field(
                name="Labels", value=", ".join(plan["label_names"]), inline=False
            )

        if kind in ("create", "create_needs_triage"):
            if plan.get("assignee_id"):
                embed.add_field(
                    name="Assignee", value=f"`{plan['assignee_id']}`", inline=False
                )
            elif plan.get("assignee_display"):
                embed.add_field(
                    name="Assignee",
                    value=f"_unassigned — intended @{plan['assignee_display']}_",
                    inline=False,
                )
            else:
                embed.add_field(name="Assignee", value="_(unassigned)_", inline=False)

        if kind in ("comment", "comment_transition", "comment_dup"):
            target_field = plan.get("target_issue_url") or plan.get("target_issue_title")
            if target_field:
                embed.add_field(name="Target", value=target_field, inline=False)

        reporter = plan.get("reporter_name", "?")
        if plan.get("source_jump_url"):
            embed.add_field(
                name="Source",
                value=f"by @{reporter} • [jump]({plan['source_jump_url']})",
                inline=False,
            )

        embed.set_footer(text=f"{APPROVE_EMOJI} execute — {REJECT_EMOJI} discard")
        return embed

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        log.debug(
            "[reaction] user=%s channel=%s msg=%s emoji=%r",
            payload.user_id,
            payload.channel_id,
            payload.message_id,
            str(payload.emoji),
        )
        if payload.user_id == self.user.id:
            return

        # ✅ on a follow-up NUDGE dismisses that open thread ("handled — stop asking").
        # Nudges live in the channel the promise came from, never in the approval
        # channel, so this can't shadow an approval: the lookup only matches a message
        # we posted as a reminder.
        if config.COS_FOLLOWUP_ENABLED and str(payload.emoji) == CLOSE_EMOJI:
            try:
                item = self.db.find_open_thread_by_reminder(payload.message_id)
            except Exception:
                log.exception("[reaction] open-thread lookup failed")
                item = None
            if item:
                try:
                    self.db.set_open_thread_status(item["id"], "closed")
                except Exception:
                    log.exception("[reaction] set_open_thread_status(closed) failed")
                    return
                log.info(
                    "[reaction] %s on nudge %s → CLOSED open thread %d (%s owed %r)",
                    CLOSE_EMOJI, payload.message_id, item["id"],
                    item["person_name"], item["what"],
                )
                return

        if payload.channel_id != config.APPROVAL_CHANNEL_ID:
            return
        emoji = str(payload.emoji)
        if emoji not in (APPROVE_EMOJI, REJECT_EMOJI):
            return

        try:
            entry = self.db.get_by_approval(payload.message_id)
        except Exception:
            log.exception("[reaction] DB lookup failed for approval_msg=%s", payload.message_id)
            return
        if entry is None:
            log.debug("[reaction] no DB entry for approval_msg=%s", payload.message_id)
            return
        if entry["status"] != "pending":
            log.info(
                "[reaction] approval_msg=%s already %s — ignoring",
                payload.message_id,
                entry["status"],
            )
            return

        approval_channel = self.get_channel(payload.channel_id)
        if approval_channel is None:
            log.error("[reaction] could not resolve approval channel %s", payload.channel_id)
            return
        try:
            approval_msg = await approval_channel.fetch_message(payload.message_id)
        except discord.NotFound:
            log.warning("[reaction] approval_msg=%s not found (deleted?)", payload.message_id)
            return
        except discord.DiscordException:
            log.exception("[reaction] failed to fetch approval_msg=%s", payload.message_id)
            return

        if emoji == REJECT_EMOJI:
            log.info("[reaction] approval_msg=%s → REJECT", payload.message_id)
            try:
                self.db.mark_rejected(payload.message_id)
            except Exception:
                log.exception("[reaction] mark_rejected failed")
            try:
                await approval_msg.reply("❌ Discarded.")
            except discord.DiscordException:
                log.exception("[reaction] reject reply failed")
            return

        # ✅ approve → execute the stored plan.
        plan = entry["classification"]
        if "kind" not in plan:
            log.error(
                "[reaction] approval_msg=%s has no plan.kind; can't execute",
                payload.message_id,
            )
            try:
                await approval_msg.reply("⚠️ Stale approval (no plan kind) — discard and re-post.")
            except discord.DiscordException:
                pass
            return

        log.info(
            "[reaction] approval_msg=%s → APPROVE, executing plan=%s",
            payload.message_id,
            plan["kind"],
        )
        result = await self._execute_plan(plan)
        if result is None:
            try:
                await approval_msg.reply(f"⚠️ Failed to execute `{plan['kind']}` — see logs.")
            except discord.DiscordException:
                log.exception("[reaction] failure-reply failed")
            return

        try:
            self.db.mark_approved(payload.message_id, result["linear_issue_id"])
        except Exception:
            log.exception("[reaction] mark_approved failed")
        try:
            await approval_msg.reply(self._format_result_message(plan, result))
        except discord.DiscordException:
            log.exception("[reaction] confirmation reply failed")
        log.info(
            "[reaction] approval_msg=%s → executed plan=%s linear=%s",
            payload.message_id,
            plan["kind"],
            result["linear_issue_id"],
        )

    # -- auto-execute path (REQUIRE_APPROVAL=False) ----------------------

    async def _execute_immediately(
        self, message: discord.Message, plan: dict
    ) -> None:
        """F.FALSE: execute now, post a short confirmation. The source message
        id stands in for approval_message_id in the DB row (Discord IDs are
        globally unique so this can never collide with a real approval embed)."""
        log.info(
            "[exec-auto] step 1/3: msg=%s recording pending (auto-execute path)",
            message.id,
        )
        try:
            self.db.record_pending(
                message_id=message.id,
                channel_id=message.channel.id,
                classification=plan,
                approval_message_id=message.id,
            )
        except Exception:
            log.exception("[exec-auto] record_pending failed; continuing anyway")

        log.info("[exec-auto] step 2/3: msg=%s executing plan=%s", message.id, plan["kind"])
        result = await self._execute_plan(plan)
        if result is None:
            try:
                self.db.mark_rejected(message.id)
            except Exception:
                log.exception("[exec-auto] mark_rejected failed")
            await self._post_to_approval_channel(
                f"⚠️ Auto-execute failed for `{plan['kind']}` on {message.jump_url}"
            )
            return

        log.info("[exec-auto] step 3/3: msg=%s marking approved + confirming", message.id)
        try:
            self.db.mark_approved(message.id, result["linear_issue_id"])
        except Exception:
            log.exception("[exec-auto] mark_approved failed")
        await self._post_to_approval_channel(self._format_result_message(plan, result))

    async def _post_to_approval_channel(self, content: str) -> None:
        channel = self.get_channel(config.APPROVAL_CHANNEL_ID)
        if channel is None:
            log.error("[notify] approval channel %s not visible", config.APPROVAL_CHANNEL_ID)
            return
        try:
            await channel.send(content)
        except discord.DiscordException:
            log.exception("[notify] send to approval channel failed")

    # -- CoS: clarifying questions ----------------------------------------
    # A report missing something essential gets ONE question, not a guess and not
    # a half-complete ticket. The plan is PARKED (never executed from here) and
    # reaches Linear only via the normal approval gate — after the answer lands,
    # or after the ask times out and the sweeper proposes it as-is.

    # A plain (non-reply) message from the reporter counts as the answer only if it
    # lands within this window of the ask. After that we require an explicit reply,
    # so an unrelated report typed an hour later isn't swallowed as an answer.
    CLARIFY_LOOSE_ANSWER_MINUTES = 30

    async def _maybe_ask_clarification(
        self, message: discord.Message, thread: list, plan: dict
    ) -> bool:
        """Ask the reporter the FEW things genuinely missing — bundled into ONE message,
        addressed to them by @mention — instead of proposing a half-complete ticket.

        Returns True when we asked (the plan is parked; the caller must NOT post an
        approval embed). False means proceed exactly as before — every failure path
        here returns False, so a broken clarification can only ever fall back to the
        old behaviour, never stall or lose a report."""
        if not config.COS_CLARIFY_ENABLED:
            return False
        if not clarify.is_clarifiable(plan):
            log.debug(
                "[clarify] msg=%s plan kind=%s is not a create; not asking",
                message.id, plan.get("kind"),
            )
            return False

        # Ask about a given report at most ONCE. If there's already a row (awaiting,
        # answered, or expired), we've had our question — we never re-ping.
        try:
            if self.db.get_clarification(message.id):
                log.info("[clarify] msg=%s already has a clarification; not asking again", message.id)
                return False
        except Exception:
            log.exception("[clarify] get_clarification failed; proposing as normal")
            return False

        thread_text, _imgs = _format_thread(thread)
        assessment = await self._assess_clarification_for(message, thread_text, plan)
        if not self._clarification_wanted(message, assessment):
            return False

        # DON'T PESTER — the screenshot/recording often lands a moment AFTER the text.
        # Wait briefly, re-read the thread, and if anything new arrived (an attachment or
        # a follow-up message) re-assess: the gap may already be filled.
        wait = config.COS_CLARIFY_ATTACHMENT_WAIT_SECONDS
        if wait > 0:
            before_sig = _thread_signature(thread)
            await asyncio.sleep(wait)
            # If the report got answered/handled during the wait, stand down.
            try:
                if self.db.already_processed(message.id) or self.db.get_clarification(message.id):
                    log.info("[clarify] msg=%s handled during the attachment-wait; not asking", message.id)
                    return False
            except Exception:
                log.exception("[clarify] state re-check during wait failed; continuing")
            try:
                thread = await self._collect_thread_context(message)
            except Exception:
                log.exception("[clarify] re-collect during wait failed; using original thread")
            if _thread_signature(thread) != before_sig:
                log.info(
                    "[clarify] msg=%s a follow-up landed during the wait; re-assessing", message.id
                )
                thread_text, _imgs = _format_thread(thread)
                assessment = await self._assess_clarification_for(message, thread_text, plan)
                if not self._clarification_wanted(message, assessment):
                    log.info(
                        "[clarify] msg=%s the follow-up filled the gap; not asking", message.id
                    )
                    return False

        questions = assessment["questions"] or [clarify.fallback_question(plan)]
        body = self._format_clarify_message(message.author, questions)
        try:
            # Reply to the report and @mention the ORIGINAL REPORTER (mention_author=True
            # pings the person whose message we're replying to — the reporter — not a
            # bystander in the thread).
            ask_msg = await message.reply(body, mention_author=True)
        except discord.DiscordException:
            log.exception(
                "[clarify] msg=%s could not post the question; proposing as normal",
                message.id,
            )
            return False

        try:
            self.db.record_clarification(
                source_message_id=message.id,
                channel_id=message.channel.id,
                ask_message_id=ask_msg.id,
                reporter_id=getattr(message.author, "id", None),
                question=body,
                plan=plan,
            )
        except Exception:
            # We asked but couldn't park the plan — nothing would ever propose it.
            # Propose it now: a duplicate-looking embed beats a lost report.
            log.exception(
                "[clarify] msg=%s record_clarification FAILED; proposing the plan now "
                "so the report isn't lost",
                message.id,
            )
            return False

        log.info(
            "[clarify] msg=%s ASKED reporter=%s (%d question(s), missing=%s, ask_msg=%s); "
            "plan parked until answered or %d min timeout",
            message.id,
            _display(message.author),
            len(questions),
            assessment["missing"],
            ask_msg.id,
            config.COS_CLARIFY_TIMEOUT_MINUTES,
        )
        return True

    async def _assess_clarification_for(
        self, message: discord.Message, thread_text: str, plan: dict
    ) -> Optional[dict]:
        """Run the model's clarification assessment for a report. Returns the verdict
        dict or None (None ⇒ don't ask)."""
        return await self.classifier.assess_clarification(
            thread_text=thread_text,
            plan=plan,
            channel=getattr(message.channel, "name", "?"),
            reporter=plan.get("reporter_name") or _display(message.author),
            max_questions=config.COS_CLARIFY_MAX_QUESTIONS,
        )

    def _clarification_wanted(
        self, message: discord.Message, assessment: Optional[dict]
    ) -> bool:
        """Whether a (fresh) assessment says we should ask: it wants clarification, is
        confident enough, and actually produced a question. Logs the reason when not."""
        if not assessment or not assessment["needs_clarification"]:
            log.info("[clarify] msg=%s nothing essential missing; proposing as normal", message.id)
            return False
        if assessment["confidence"] < config.COS_CLARIFY_MIN_CONFIDENCE:
            log.info(
                "[clarify] msg=%s gap %s but confidence %.2f < %.2f; proposing as normal",
                message.id, assessment["missing"], assessment["confidence"],
                config.COS_CLARIFY_MIN_CONFIDENCE,
            )
            return False
        if not assessment.get("questions"):
            log.info("[clarify] msg=%s no concrete question; proposing as normal", message.id)
            return False
        return True

    def _format_clarify_message(self, reporter, questions: list[str]) -> str:
        """One message addressed to the reporter. A single question stays inline; two or
        three are bundled as a short numbered list — never several messages."""
        mention = f"<@{getattr(reporter, 'id', '')}>".strip()
        qs = [q.strip() for q in questions if q.strip()]
        if len(qs) <= 1:
            return f"{mention} {qs[0]}" if mention else qs[0]
        numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(qs, 1))
        lead = f"{mention} a couple of quick things so I can get this filed right:" if mention \
            else "A couple of quick things so I can get this filed right:"
        return f"{lead}\n{numbered}"

    def _match_awaiting_clarification(
        self, message: discord.Message
    ) -> Optional[dict]:
        """The parked report this message answers, if any.

        Two ways to answer, in order of confidence:
          1. A REPLY to our question (or to the original report) — from ANYONE. A
             teammate who knows the repro may answer on the reporter's behalf.
          2. A plain message from the REPORTER themselves, shortly after the ask —
             how people actually reply in a busy channel. Time-boxed
             (CLARIFY_LOOSE_ANSWER_MINUTES) so a later, unrelated report from the
             same person isn't mistaken for an answer.
        """
        try:
            awaiting = self.db.list_awaiting_clarifications(message.channel.id)
        except Exception:
            log.exception("[clarify] list_awaiting_clarifications failed")
            return None
        if not awaiting:
            return None

        ref = message.reference
        ref_id = getattr(ref, "message_id", None) if ref else None
        if ref_id:
            for row in awaiting:
                if ref_id in (row["ask_message_id"], row["source_message_id"]):
                    log.info(
                        "[clarify] msg=%s is a REPLY to %s → answers parked report %s",
                        message.id, ref_id, row["source_message_id"],
                    )
                    return row

        author_id = getattr(message.author, "id", None)
        cutoff = followups.now_utc() - timedelta(minutes=self.CLARIFY_LOOSE_ANSWER_MINUTES)
        for row in awaiting:  # newest ask first
            if not author_id or row["reporter_id"] != author_id:
                continue
            # The loose window resets on each nudge — a reporter who answers shortly after
            # a bump (not just the original ask) is still answering it.
            last_contact_ts = row.get("last_nudged_at") or row["created_at"]
            if followups.from_ts(last_contact_ts) < cutoff:
                log.debug(
                    "[clarify] msg=%s from the reporter, but the last ask/bump on %s is older "
                    "than %d min — needs an explicit reply",
                    message.id, row["source_message_id"], self.CLARIFY_LOOSE_ANSWER_MINUTES,
                )
                continue
            log.info(
                "[clarify] msg=%s is the reporter answering in-channel → parked report %s",
                message.id, row["source_message_id"],
            )
            return row
        return None

    async def _maybe_resume_clarification(self, message: discord.Message) -> bool:
        """The answer landed — re-plan the WHOLE thread (report + question + answer)
        and propose the ticket. Returns True when this message was consumed as an
        answer, so `on_message` doesn't also triage it as a report of its own."""
        if not config.COS_CLARIFY_ENABLED:
            return False
        row = self._match_awaiting_clarification(message)
        if not row:
            return False
        try:
            if self.db.already_processed(message.id):
                log.info("[clarify] msg=%s already processed; not resuming twice", message.id)
                return False
        except Exception:
            log.exception("[clarify] already_processed check failed; resuming anyway")

        parked_plan = row["plan"]
        source_id = row["source_message_id"]
        log.info(
            "[clarify] RESUME: answer msg=%s for parked report %s — re-planning with the answer",
            message.id, source_id,
        )

        # Re-classify the thread as it now stands. The answer is in it, so the fresh
        # verdict/plan carries the repro, the owner, the scope — whatever we asked for.
        source: Optional[discord.Message] = None
        try:
            source = await message.channel.fetch_message(source_id)
        except discord.DiscordException as e:
            log.info(
                "[clarify] original report %s no longer fetchable (%s); re-planning from "
                "the answer's thread alone",
                source_id, type(e).__name__,
            )

        thread = await self._collect_thread_context(message)
        if source is not None and all(m.id != source.id for m in thread):
            thread = sorted([source] + thread, key=lambda m: m.created_at)

        thread_text, image_urls = _format_thread(thread)
        reporter = _display(thread[0].author) if thread else _display(message.author)
        participants = sorted({_display(m.author) for m in thread})

        plan = parked_plan
        verdict = await self.classifier.classify(
            content=thread_text,
            author=reporter,
            channel=getattr(message.channel, "name", "?"),
            image_urls=image_urls,
            participants=participants,
        )
        if verdict is None:
            log.warning(
                "[clarify] re-classify failed for %s; proposing the PARKED plan as-is",
                source_id,
            )
        elif verdict["category"] == "noise":
            # The answer withdrew the report ("nvm, my mistake", "working now"). A CoS
            # drops it — she doesn't file the ticket anyway.
            log.info(
                "[clarify] answer msg=%s makes report %s noise; CANCELLING the parked plan",
                message.id, source_id,
            )
            try:
                self.db.set_clarification_status(source_id, "cancelled")
            except Exception:
                log.exception("[clarify] set_clarification_status(cancelled) failed")
            return True
        else:
            replanned = await self._decide_plan(
                message=source or message, thread=thread, verdict=verdict
            )
            if replanned is None:
                log.warning(
                    "[clarify] re-plan returned None for %s; proposing the PARKED plan as-is",
                    source_id,
                )
            else:
                plan = replanned
                log.info(
                    "[clarify] re-planned %s with the answer: kind=%s title=%r assignee=%s",
                    source_id,
                    plan["kind"],
                    plan.get("title"),
                    plan.get("assignee_id") or plan.get("assignee_display") or "(unassigned)",
                )

        try:
            self.db.set_clarification_status(source_id, "answered")
        except Exception:
            log.exception("[clarify] set_clarification_status(answered) failed")

        tracked_under = await self._propose_plan(source or message, plan)

        # Mark the ANSWER processed against the same report, so it can never be
        # re-triaged into a ticket of its own.
        if tracked_under is not None and message.id != (source.id if source else message.id):
            try:
                self.db.record_merged(
                    message_id=message.id,
                    channel_id=message.channel.id,
                    classification=plan,
                    approval_message_id=tracked_under,
                )
            except Exception:
                log.exception("[clarify] record_merged for the answer msg=%s failed", message.id)

        log.info("[clarify] RESUME DONE for report %s (proposed under %s)", source_id, tracked_under)
        return True

    async def _propose_timed_out_clarifications(self) -> None:
        """Nobody answered in time → propose the PARKED plan, flagged NEEDS-TRIAGE. The
        question was an attempt to improve the ticket, not a condition for filing it: an
        ignored question must never mean a silently dropped report, and it must never mean
        endless re-pinging either — we file it (for the PM to triage) and stop."""
        cutoff = followups.to_ts(
            followups.now_utc() - timedelta(minutes=config.COS_CLARIFY_TIMEOUT_MINUTES)
        )
        try:
            stale = self.db.list_stale_clarifications(cutoff)
        except Exception:
            log.exception("[sweep] list_stale_clarifications failed")
            return
        if not stale:
            return
        log.info(
            "[sweep] %d clarification(s) unanswered after %d min — filing them needs-triage",
            len(stale), config.COS_CLARIFY_TIMEOUT_MINUTES,
        )

        for row in stale:
            plan = row["plan"]
            source_id = row["source_message_id"]
            if not plan or "kind" not in plan:
                log.error("[sweep] parked plan for %s is unusable; marking expired", source_id)
                try:
                    self.db.set_clarification_status(source_id, "expired")
                except Exception:
                    log.exception("[sweep] set_clarification_status(expired) failed")
                continue

            # No answer → file it for a human to triage rather than as a confidently-owned
            # ticket. Flip the plan to needs-triage (unassigned by convention) and note why.
            plan = dict(plan)
            plan["kind"] = "create_needs_triage"
            plan["assignee_id"] = None
            plan["assignee_display"] = None
            plan["needs_triage_reason"] = "reporter didn't answer the clarifying question"

            # Prefer the full path (dedup + embed) with a real Message; fall back to
            # posting the embed straight from the stored ids if it's gone.
            posted = False
            try:
                channel = self.get_channel(row["channel_id"]) or await self.fetch_channel(
                    row["channel_id"]
                )
                source = await channel.fetch_message(source_id)
                posted = await self._propose_plan(source, plan) is not None
            except discord.DiscordException as e:
                log.info(
                    "[sweep] source %s unfetchable (%s); posting the embed from stored ids",
                    source_id, type(e).__name__,
                )
                sent = await self._send_approval_embed(
                    plan,
                    source_message_id=source_id,
                    source_channel_id=row["channel_id"],
                )
                posted = sent is not None

            if not posted:
                # Leave it 'awaiting' so the next sweep retries. Stage-1 dedup means a
                # retry can't produce a second embed for the same report.
                log.error(
                    "[sweep] could not propose parked plan for %s; will retry next sweep",
                    source_id,
                )
                continue

            try:
                self.db.set_clarification_status(source_id, "expired")
            except Exception:
                log.exception("[sweep] set_clarification_status(expired) failed")
            log.info("[sweep] filed unanswered report %s as needs-triage", source_id)

            # If we actually chased the reporter (at least one nudge) and still got
            # silence, tell the PMs once — a report went in untriaged because nobody
            # answered. A report that simply timed out without being nudged doesn't
            # warrant a PM ping.
            if row.get("nudges_sent", 0) >= 1 and not row.get("pm_notified"):
                notified = await self._notify_pms_giveup(
                    person_mention=followups.mention_or_name(row.get("reporter_id"), "the reporter"),
                    what=f"my question on their report ({(plan.get('title') or 'a report')[:80]})",
                    attempts=row.get("nudges_sent", 0),
                    outcome="I've filed it as needs-triage so it isn't lost",
                )
                if notified:
                    try:
                        self.db.mark_clarification_pm_notified(source_id)
                    except Exception:
                        log.exception("[sweep] mark_clarification_pm_notified failed for %s", source_id)

    # -- CoS: open threads (proactive follow-up) ---------------------------
    # Someone said they'd come back with something. We remember it, and when it
    # ages past due we surface it back to THEM. The nudge is a reminder to a human
    # — nothing is filed, changed, or escalated on their behalf.

    async def _maybe_track_commitment(self, message: discord.Message) -> None:
        """Spot "I'll confirm the DM dates in an hour" and start waiting on it."""
        if not config.COS_FOLLOWUP_ENABLED:
            return
        text = (message.content or "").strip()
        if not followups.looks_like_commitment(text):
            return
        try:
            if self.db.find_open_thread_by_source(message.id):
                log.debug("[followup] msg=%s already tracked as an open thread", message.id)
                return
        except Exception:
            log.exception("[followup] find_open_thread_by_source failed; skipping")
            return

        log.info("[followup] msg=%s looks like a commitment; asking the model", message.id)
        verdict = await self.classifier.detect_commitment(
            text=text,
            author=_display(message.author),
            channel=getattr(message.channel, "name", "?"),
        )
        if not verdict or not verdict["is_commitment"]:
            log.info("[followup] msg=%s is not a commitment; not tracking", message.id)
            return
        if verdict["confidence"] < config.COS_FOLLOWUP_MIN_CONFIDENCE:
            log.info(
                "[followup] msg=%s commitment conf %.2f < %.2f; not tracking (a false "
                "nudge is worse than a missed one)",
                message.id, verdict["confidence"], config.COS_FOLLOWUP_MIN_CONFIDENCE,
            )
            return

        promised_at = message.created_at or followups.now_utc()
        due_at = followups.due_at_from(
            verdict["due_minutes"],
            default_minutes=config.COS_FOLLOWUP_DEFAULT_DUE_MINUTES,
            promised_at=promised_at,
        )
        try:
            created = self.db.record_open_thread(
                channel_id=message.channel.id,
                message_id=message.id,
                jump_url=message.jump_url,
                person_id=getattr(message.author, "id", None),
                person_name=_display(message.author),
                what=verdict["what"],
                promised_at=followups.to_ts(promised_at),
                due_at=followups.to_ts(due_at),
            )
        except Exception:
            log.exception("[followup] record_open_thread failed for msg=%s", message.id)
            return
        if created:
            log.info(
                "[followup] TRACKING open thread: %s owes %r — due %s (stated=%s min)",
                _display(message.author),
                verdict["what"],
                followups.to_ts(due_at),
                verdict["due_minutes"],
            )

    async def _maybe_close_open_thread(self, message: discord.Message) -> bool:
        """A reply to our nudge — or to the original promise — means they came back.
        Stop waiting. Doesn't consume the message: the follow-up may itself be a
        report, and it still deserves triage."""
        if not config.COS_FOLLOWUP_ENABLED:
            return False
        ref = message.reference
        ref_id = getattr(ref, "message_id", None) if ref else None
        if not ref_id:
            return False
        try:
            item = self.db.find_open_thread_by_reminder(ref_id) or (
                self.db.find_open_thread_by_source(ref_id)
            )
        except Exception:
            log.exception("[followup] open-thread lookup failed for ref=%s", ref_id)
            return False
        if not item:
            return False
        try:
            self.db.set_open_thread_status(item["id"], "closed")
        except Exception:
            log.exception("[followup] set_open_thread_status(closed) failed")
            return False
        log.info(
            "[followup] CLOSED open thread %d (%s owed %r) — msg=%s replied to %s",
            item["id"], item["person_name"], item["what"], message.id, ref_id,
        )
        return True

    # -- CoS: unified nudge policy (commitments + clarifications) -----------
    # Both nudge sources obey the same guardrail: the same person is nudged about the
    # same item at most once per window, at most N attempts, then we stop, record it, and
    # tell the PMs once. The window/cap are the CONSERVATIVE combination of the legacy
    # open-thread settings and the new unified ones, so this is never MORE chatty.

    def _nudge_cooldown_minutes(self) -> int:
        return max(
            config.COS_FOLLOWUP_REMINDER_COOLDOWN_MINUTES,
            config.COS_NUDGE_WINDOW_HOURS * 60,
        )

    def _nudge_max_attempts(self) -> int:
        return min(config.COS_FOLLOWUP_MAX_REMINDERS, config.COS_NUDGE_MAX_ATTEMPTS)

    async def _notify_pms_giveup(
        self, *, person_mention: str, what: str, attempts: int, outcome: str
    ) -> bool:
        """Tell the PMs ONCE that a nudge went unanswered after every attempt — e.g.
        "I've asked @Ravi twice about the DM date with no reply." Posts to the escalation
        channel, @-mentioning ESCALATION_USER_IDS. READ-ONLY (Discord only). Returns True
        if a message went out (so the caller can flag it notified and never repeat)."""
        if not config.ESCALATION_USER_IDS:
            log.info("[followup] give-up: ESCALATION_USER_IDS empty; not informing PMs")
            return False
        channel_id = config.cos_nudge_channel_id()
        if not channel_id:
            log.info("[followup] give-up: no nudge channel configured; not informing PMs")
            return False
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.DiscordException as e:
                log.warning("[followup] give-up: nudge channel %s unreachable (%s)", channel_id, type(e).__name__)
                return False
        pm_mentions = " ".join(f"<@{uid}>" for uid in sorted(config.ESCALATION_USER_IDS))
        times = "once" if attempts == 1 else f"{attempts} times" if attempts else "repeatedly"
        text = (
            f"{pm_mentions} heads up — I've asked {person_mention} {times} about {what} "
            f"with no reply, so I'm going to stop chasing it. {outcome}."
        )
        try:
            await channel.send(text)
        except discord.DiscordException:
            log.exception("[followup] give-up: could not post PM notice")
            return False
        log.info("[followup] give-up: informed PMs about %s (%d attempt(s))", what, attempts)
        return True

    async def _nudge_due_open_threads(self) -> None:
        """Surface aged open threads back into the channel they came from."""
        now = followups.now_utc()
        try:
            items = self.db.list_due_open_threads(
                now=followups.to_ts(now),
                reminder_cutoff=followups.to_ts(
                    now - timedelta(minutes=self._nudge_cooldown_minutes())
                ),
            )
        except Exception:
            log.exception("[sweep] list_due_open_threads failed")
            return
        if not items:
            log.debug("[sweep] no open threads due")
            return
        log.info("[sweep] %d open thread(s) due for a nudge", len(items))

        max_attempts = self._nudge_max_attempts()
        for item in items:
            if item["reminders_sent"] >= max_attempts:
                # Asked enough. Stop, record the non-response (the stale status + the
                # reminder count ARE the record), and tell the PMs once.
                log.info(
                    "[sweep] open thread %d nudged %d time(s) with no answer → stale",
                    item["id"], item["reminders_sent"],
                )
                if not item.get("pm_notified"):
                    notified = await self._notify_pms_giveup(
                        person_mention=followups.mention_or_name(
                            item["person_id"], item["person_name"]
                        ),
                        what=item["what"],
                        attempts=item["reminders_sent"],
                        outcome="nothing's been filed or changed on their behalf",
                    )
                    if notified:
                        try:
                            self.db.mark_open_thread_pm_notified(item["id"])
                        except Exception:
                            log.exception("[sweep] mark_open_thread_pm_notified failed for %d", item["id"])
                try:
                    self.db.set_open_thread_status(item["id"], "stale")
                except Exception:
                    log.exception("[sweep] set_open_thread_status(stale) failed")
                continue
            await self._nudge_open_thread(item)

    async def _nudge_open_thread(self, item: dict) -> None:
        channel = self.get_channel(item["channel_id"])
        if channel is None:
            try:
                channel = await self.fetch_channel(item["channel_id"])
            except discord.DiscordException as e:
                log.warning(
                    "[followup] channel %s unreachable (%s); can't nudge open thread %d",
                    item["channel_id"], type(e).__name__, item["id"],
                )
                return

        mention = followups.mention_or_name(item["person_id"], item["person_name"])
        text = await self.classifier.followup_nudge(
            mention=mention,
            what=item["what"],
            when=followups.humanize_age(followups.from_ts(item["promised_at"])),
            jump_url=item["jump_url"],
        )

        # Reply to the promise itself when it's still there, so the nudge sits in
        # context; otherwise just post it in the channel.
        sent: Optional[discord.Message] = None
        try:
            promise_msg = await channel.fetch_message(item["message_id"])
            sent = await promise_msg.reply(text, mention_author=False)
        except discord.DiscordException:
            try:
                sent = await channel.send(text)
            except discord.DiscordException:
                log.exception("[followup] could not post nudge for open thread %d", item["id"])
                return

        # ✅ dismisses it — "handled, stop asking".
        try:
            await sent.add_reaction(CLOSE_EMOJI)
        except discord.DiscordException:
            log.debug("[followup] could not add %s to nudge %s", CLOSE_EMOJI, sent.id)

        try:
            self.db.mark_open_thread_reminded(
                item["id"],
                reminder_message_id=sent.id,
                at=followups.to_ts(followups.now_utc()),
            )
        except Exception:
            log.exception("[followup] mark_open_thread_reminded failed for %d", item["id"])

        log.info(
            "[followup] NUDGED %s about %r (open thread %d, reminder %d/%d, msg=%s)",
            item["person_name"],
            item["what"],
            item["id"],
            item["reminders_sent"] + 1,
            self._nudge_max_attempts(),
            sent.id,
        )

    async def _nudge_open_clarifications(self) -> None:
        """The SECOND nudge source: unanswered clarifying questions. Remind the reporter
        once (subject to the same window/cap as commitments) if they've gone quiet on a
        question that's still parking their report. The timeout path handles the give-up
        (file needs-triage + inform the PMs), so here we only chase, never escalate."""
        if not config.COS_CLARIFY_ENABLED:
            return
        now = followups.now_utc()
        created_before = followups.to_ts(
            now - timedelta(minutes=config.COS_CLARIFY_NUDGE_AFTER_MINUTES)
        )
        last_nudged_before = followups.to_ts(
            now - timedelta(minutes=self._nudge_cooldown_minutes())
        )
        try:
            rows = self.db.list_nudgeable_clarifications(
                created_before=created_before, last_nudged_before=last_nudged_before
            )
        except Exception:
            log.exception("[sweep] list_nudgeable_clarifications failed")
            return
        if not rows:
            return
        max_attempts = self._nudge_max_attempts()
        log.info("[sweep] %d open clarification(s) to consider nudging", len(rows))
        for row in rows:
            if row["nudges_sent"] >= max_attempts:
                # Chased enough. The timeout sweep files it needs-triage and tells the PMs.
                continue
            await self._nudge_open_clarification(row)

    async def _nudge_open_clarification(self, row: dict) -> None:
        channel = self.get_channel(row["channel_id"])
        if channel is None:
            try:
                channel = await self.fetch_channel(row["channel_id"])
            except discord.DiscordException as e:
                log.warning(
                    "[followup] channel %s unreachable (%s); can't nudge clarification %s",
                    row["channel_id"], type(e).__name__, row["source_message_id"],
                )
                return
        reporter_id = row.get("reporter_id")
        mention = followups.mention_or_name(reporter_id, "there")
        text = (
            f"{mention} just a gentle bump on this — whenever you get a moment, an answer "
            "to my question above lets me get it filed. No rush."
        )
        anchor_id = row.get("ask_message_id") or row.get("source_message_id")
        try:
            anchor = await channel.fetch_message(anchor_id)
            await anchor.reply(text, mention_author=bool(reporter_id))
        except discord.DiscordException:
            try:
                await channel.send(text)
            except discord.DiscordException:
                log.exception(
                    "[followup] could not post clarification nudge for %s",
                    row["source_message_id"],
                )
                return
        try:
            self.db.mark_clarification_nudged(
                row["source_message_id"], at=followups.to_ts(followups.now_utc())
            )
        except Exception:
            log.exception(
                "[followup] mark_clarification_nudged failed for %s", row["source_message_id"]
            )
        log.info(
            "[followup] NUDGED reporter about clarification %s (attempt %d/%d)",
            row["source_message_id"], row["nudges_sent"] + 1, self._nudge_max_attempts(),
        )

    # -- CoS: the two-level escalation ladder ------------------------------
    # She audits OPEN Linear issues for gaps worth chasing, and asks a HUMAN:
    #   level 1 → the ASSIGNEE, for a data gap on their own issue,
    #   level 2 → the PMs, for a call that is above an IC.
    # NOTHING here writes to Linear. Every path ends in a Discord message.

    def _resolve_audience(self, finding: dict) -> Optional[tuple[str, str]]:
        """Who to tag for this finding → (mentions, target_key), or None when there is
        nobody to ask (so the finding is dropped rather than shouted into the void).

        `target_key` identifies the audience for the RATE LIMIT — it must be stable
        across sweeps, so it's keyed on the Discord id (or the lower-cased name when the
        person isn't mapped), never on anything that changes run to run.

        This is the only place people are resolved, and it is the guardrail on who may
        be @-mentioned:
          - PMs: ONLY ESCALATION_USER_IDS. Never anyone else.
          - Assignee: only via the reverse DISCORD_LINEAR_MAP lookup. An UNMAPPED person
            is NEVER pinged — we name them in plain text instead, so a stray Linear
            account can't be tagged into the channel.
        """
        if finding["level"] == escalation.LEVEL_PM:
            if not config.ESCALATION_USER_IDS:
                log.info(
                    "[escalate] %s needs a PM call but ESCALATION_USER_IDS is empty; skipping",
                    finding["issue_id"],
                )
                return None
            mentions = " ".join(f"<@{uid}>" for uid in sorted(config.ESCALATION_USER_IDS))
            return mentions, "pm"

        # Level 1 — the issue's own assignee.
        name = (finding.get("assignee") or "").strip()
        email = (finding.get("assignee_email") or "").strip()
        if not name and not email:
            log.debug("[escalate] %s has no assignee to tag; skipping", finding["issue_id"])
            return None

        discord_id = config.discord_id_for_linear(email, name)
        if discord_id:
            return f"<@{discord_id}>", f"assignee:{discord_id}"

        # Known to Linear, unknown to Discord → say their name, don't fake a mention.
        log.info(
            "[escalate] no Discord id for Linear user %r (%s); naming them in plain text",
            name or email, finding["issue_id"],
        )
        return name or email, f"assignee:{(name or email).lower()}"

    async def _audit_linear_gaps(self) -> None:
        """One audit pass: read the open issues, decide what a CoS would chase, and ask
        the right human about it. READ-ONLY on Linear throughout."""
        if not (config.COS_TAG_ASSIGNEE_ENABLED or config.COS_ESCALATE_ENABLED):
            return
        channel_id = config.cos_nudge_channel_id()
        if not channel_id:
            log.debug("[escalate] no nudge channel configured; audit no-ops")
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.DiscordException as e:
                log.warning(
                    "[escalate] nudge channel %s unreachable (%s); audit no-ops",
                    channel_id, type(e).__name__,
                )
                return

        # SCOPE (point 1): only ACTIVE issues — In Progress / Implemented (awaiting QA) /
        # In Review — matched by state NAME. Backlog / Todo / Done / Canceled are ignored.
        issues = await self.linear.list_active_issues_for_audit(
            state_names=config.COS_ACTIVE_STATE_NAMES,
            limit=config.COS_AUDIT_MAX_ISSUES,
        )
        if not issues:
            log.debug("[escalate] no active issues to audit")
            return
        issues_by_id = {i.get("identifier"): i for i in issues if i.get("identifier")}

        today = followups.now_utc().date()
        findings = escalation.find_gaps(
            issues,
            launch_window_days=config.COS_LAUNCH_WINDOW_DAYS,
            stale_in_progress_days=config.STALE_IN_PROGRESS_DAYS,
            today=today,
        )

        # The model-judged level-1 gaps (missing repro / vague scope). Only for
        # launch-critical, ASSIGNED issues with no deterministic finding already — we
        # never send two nudges about one ticket, and we don't pay for an LLM call on a
        # backlog issue nobody is waiting on.
        if config.COS_TAG_ASSIGNEE_ENABLED:
            covered = {f["issue_id"] for f in findings}
            for issue in issues:
                ident = issue.get("identifier")
                if not ident or ident in covered:
                    continue
                if not issue.get("assignee"):
                    continue
                if not escalation.is_launch_critical(
                    issue, window_days=config.COS_LAUNCH_WINDOW_DAYS, today=today
                ):
                    continue
                verdict = await self.classifier.assess_issue_gap(issue=issue)
                if not verdict or verdict["gap"] == "none":
                    continue
                if verdict["confidence"] < config.COS_CLARIFY_MIN_CONFIDENCE:
                    log.info(
                        "[escalate] %s gap=%s but confidence %.2f < %.2f; staying quiet",
                        ident, verdict["gap"], verdict["confidence"],
                        config.COS_CLARIFY_MIN_CONFIDENCE,
                    )
                    continue
                findings.append(
                    escalation.model_finding(
                        issue,
                        kind=verdict["gap"],
                        detail=verdict["detail"],
                        today=today,
                    )
                )

        # CHECK COMMENTS BEFORE TAGGING (point 2): for each assignee-level gap, read the
        # issue's comments first. A comment may already answer it (don't tag — and, for a
        # missing date, feed the loop-closer), or reveal a blocker that belongs with the PMs
        # rather than the IC. Returns the rewritten findings + any deadlines the comments
        # already supplied.
        comment_deadlines: list[tuple] = []
        if config.COS_CHECK_COMMENTS_ENABLED and (
            config.COS_TAG_ASSIGNEE_ENABLED or config.COS_ESCALATE_ENABLED
        ):
            findings, comment_deadlines = await self._reconcile_findings_with_comments(
                findings, issues_by_id, today
            )

        # CLOSE THE LOOP from a comment source (point 3): a deadline already sitting in the
        # comments can be written straight away (guarded by the deadline flag + dry-run).
        if comment_deadlines and config.COS_UPDATE_DEADLINE_ENABLED:
            for issue, new_date, source in comment_deadlines:
                await self._apply_deadline_update(issue, new_date, source)

        if not findings:
            log.debug("[escalate] audit found nothing to chase")
            return

        # Respect the flags: each rung can be turned off independently.
        findings = [
            f
            for f in findings
            if (f["level"] == escalation.LEVEL_ASSIGNEE and config.COS_TAG_ASSIGNEE_ENABLED)
            or (f["level"] == escalation.LEVEL_PM and config.COS_ESCALATE_ENABLED)
        ]

        log.info("[escalate] audit: %d finding(s) to consider", len(findings))
        cooldown_since = followups.to_ts(
            followups.now_utc() - timedelta(hours=config.COS_TAG_COOLDOWN_HOURS)
        )
        sent = 0
        seen: set[tuple] = set()

        for finding in findings:
            if sent >= config.COS_MAX_NUDGES_PER_SWEEP:
                log.info(
                    "[escalate] hit the per-sweep cap (%d); %d finding(s) held over to "
                    "the next sweep",
                    config.COS_MAX_NUDGES_PER_SWEEP,
                    len(findings) - sent,
                )
                break

            audience = self._resolve_audience(finding)
            if audience is None:
                continue
            mentions, target_key = audience
            key = (target_key, finding["issue_id"], finding["kind"])

            # De-dupe within this sweep...
            if key in seen:
                continue
            seen.add(key)

            # ...and across sweeps. FAIL CLOSED: if the rate-limit check itself breaks we
            # stay quiet, because double-tagging someone is worse than missing a nudge.
            try:
                if self.db.was_nudged_since(
                    target_key=target_key,
                    issue_id=finding["issue_id"],
                    kind=finding["kind"],
                    since=cooldown_since,
                ):
                    log.info(
                        "[escalate] already tagged %s about %s (%s) in the last %dh; quiet",
                        target_key, finding["issue_id"], finding["kind"],
                        config.COS_TAG_COOLDOWN_HOURS,
                    )
                    continue
            except Exception:
                log.exception(
                    "[escalate] rate-limit check failed for %s/%s; staying quiet (fail closed)",
                    target_key, finding["issue_id"],
                )
                continue

            text = await self.classifier.compose_escalation(
                level=finding["level"], mentions=mentions, finding=finding
            )
            try:
                msg = await channel.send(text)
            except discord.DiscordException:
                log.exception(
                    "[escalate] could not post %s nudge for %s",
                    finding["level"], finding["issue_id"],
                )
                continue

            try:
                self.db.record_nudge(
                    target_key=target_key,
                    issue_id=finding["issue_id"],
                    kind=finding["kind"],
                    level=finding["level"],
                    channel_id=channel_id,
                    message_id=msg.id,
                    sent_at=followups.to_ts(followups.now_utc()),
                )
            except Exception:
                log.exception(
                    "[escalate] record_nudge FAILED for %s — it may be re-sent next sweep",
                    finding["issue_id"],
                )
            sent += 1
            log.info(
                "[escalate] POSTED level=%s kind=%s issue=%s → %s (msg=%s)",
                finding["level"], finding["kind"], finding["issue_id"], target_key, msg.id,
            )

            # CLOSE THE LOOP (point 3): after tagging an assignee about a MISSING DUE DATE,
            # start watching for the answer (their reply, a comment, or the standup) so a
            # later sweep can set the date. Only records intent — no Linear write here.
            if (
                config.COS_UPDATE_DEADLINE_ENABLED
                and finding["level"] == escalation.LEVEL_ASSIGNEE
                and finding["kind"] == escalation.MISSING_DUE_DATE
            ):
                src_issue = issues_by_id.get(finding["issue_id"]) or {}
                issue_uuid = src_issue.get("id")
                if issue_uuid and not self.db.has_pending_deadline_watch(finding["issue_id"]):
                    try:
                        self.db.record_deadline_watch(
                            issue_uuid=issue_uuid,
                            issue_identifier=finding["issue_id"],
                            assignee_key=target_key,
                            channel_id=channel_id,
                            nudge_message_id=msg.id,
                            asked_at=followups.to_ts(followups.now_utc()),
                        )
                        log.info("[deadline] watching %s for a deadline answer", finding["issue_id"])
                    except Exception:
                        log.exception("[deadline] record_deadline_watch failed for %s", finding["issue_id"])

        log.info("[escalate] audit done: %d nudge(s) posted", sent)

    # -- CoS: check comments before tagging (point 2, read-only) -----------

    # Assignee-level gaps whose comments are worth reading before we ping the IC.
    _COMMENT_CHECKED_KINDS = frozenset(
        {escalation.MISSING_DUE_DATE, escalation.MISSING_REPRO, escalation.VAGUE_SCOPE}
    )

    async def _reconcile_findings_with_comments(
        self, findings: list[dict], issues_by_id: dict, today
    ) -> tuple[list[dict], list[tuple]]:
        """For each ASSIGNEE-level gap, read the issue's comments and decide (point 2):
          - comments already answer it → DROP the ping; if it's a due-date gap and a date
            was committed, hand it back as a deadline to write (loop-closer, point 3);
          - comments show a blocker needing a decision → REPLACE with a LEVEL-2 PM finding
            surfacing the reason; a blocker with no decision to make → drop quietly (a ping
            won't help);
          - otherwise → keep the assignee ping.
        Returns (rewritten_findings, [(issue, date, source), …]). Read-only."""
        today_iso = today.isoformat()
        kept: list[dict] = []
        deadlines: list[tuple] = []
        for f in findings:
            if (
                f.get("level") != escalation.LEVEL_ASSIGNEE
                or f.get("kind") not in self._COMMENT_CHECKED_KINDS
            ):
                kept.append(f)
                continue
            issue = issues_by_id.get(f["issue_id"])
            if not issue:
                kept.append(f)
                continue
            issue_uuid = issue.get("id") or f["issue_id"]
            comments = await self.linear.list_comments(issue_uuid)
            verdict = await self.classifier.assess_issue_comments(
                issue=issue, comments=comments, gap_kind=f["kind"], today=today_iso
            )
            # No verdict / low confidence → no reliable signal, tag as planned.
            if not verdict or verdict["confidence"] < config.COS_CLARIFY_MIN_CONFIDENCE:
                kept.append(f)
                continue

            if verdict["resolves_gap"]:
                if f["kind"] == escalation.MISSING_DUE_DATE and verdict["found_deadline"]:
                    deadlines.append((issue, verdict["found_deadline"], "issue comment"))
                    log.info(
                        "[escalate] %s: a comment already commits a deadline (%s) — not tagging",
                        f["issue_id"], verdict["found_deadline"],
                    )
                else:
                    log.info(
                        "[escalate] %s: comments already resolve the %s gap — not tagging",
                        f["issue_id"], f["kind"],
                    )
                continue  # gap answered; drop the ping

            if verdict["blocker"]:
                if verdict["escalate_to_pm"] and config.COS_ESCALATE_ENABLED:
                    reason = verdict["reason"] or "is blocked (see the issue comments)"
                    log.info(
                        "[escalate] %s: comments show a blocker — escalating to PMs instead "
                        "of pinging %s", f["issue_id"], f.get("assignee") or "the assignee",
                    )
                    kept.append(escalation.pm_blocker_finding(issue, detail=reason, today=today))
                else:
                    log.info(
                        "[escalate] %s: comments show a blocker but no PM decision needed — "
                        "staying quiet (a ping to the IC won't help)", f["issue_id"],
                    )
                continue  # either escalated or dropped; never ping the IC

            kept.append(f)  # comments neither answer nor block → tag as planned
        return kept, deadlines

    # -- CoS: close the loop — the one scoped Linear write (point 3) --------

    async def _apply_deadline_update(self, issue: dict, new_date: str, source: str) -> bool:
        """Set an issue's DUE DATE to `new_date` and comment noting the date + `source`.
        The ONLY Linear write on this path — it never changes status or assignee. In
        DRY-RUN (default) it logs the intended write and touches nothing. Returns True on
        a successful write (or dry-run), False on failure."""
        ident = issue.get("identifier") or issue.get("id") or "?"
        issue_uuid = issue.get("id")
        if config.COS_UPDATE_DEADLINE_DRY_RUN or not issue_uuid:
            log.info(
                "[deadline] DRY-RUN: would set %s due %s (source: %s)", ident, new_date, source
            )
            return True
        body = (
            f"📅 Setting the due date to **{new_date}** based on {source}. "
            f"Status and assignee are unchanged. — {persona.NAME}"
        )
        try:
            await self.linear.add_comment(issue_uuid, body)
        except Exception:
            log.exception("[deadline] comment write failed for %s; aborting due-date set", ident)
            return False
        updated = await self.linear.set_issue_due_date(issue_uuid, new_date)
        if not updated:
            log.error("[deadline] due-date write failed for %s (comment was posted)", ident)
            return False
        log.info("[deadline] WROTE %s due %s (source: %s)", ident, new_date, source)
        return True

    async def _resolve_deadline_watches(self) -> None:
        """Each sweep: for every pending deadline watch, look for the answer (the assignee's
        Discord reply to the tag, a new issue comment, or the standup notes), and update the
        ticket when a date is found. Old watches nobody answered are expired."""
        try:
            watches = self.db.list_pending_deadline_watches()
        except Exception:
            log.exception("[deadline] list_pending_deadline_watches failed")
            return
        if not watches:
            return
        log.info("[deadline] resolving %d pending watch(es)", len(watches))
        today_iso = followups.now_utc().date().isoformat()
        for w in watches:
            try:
                found = await self._find_deadline_answer(w, today_iso)
            except Exception:
                log.exception("[deadline] answer search failed for %s", w.get("issue_identifier"))
                continue
            if found:
                new_date, source = found
                issue = {"identifier": w["issue_identifier"], "id": w["issue_uuid"]}
                ok = await self._apply_deadline_update(issue, new_date, source)
                if ok:
                    self.db.resolve_deadline_watch(
                        w["id"], resolved_date=new_date, resolved_source=source
                    )
                continue
            # No answer yet — expire if it's been waiting too long.
            age_days = (followups.now_utc() - followups.from_ts(w["asked_at"])).days
            if age_days >= config.COS_DEADLINE_WATCH_EXPIRE_DAYS:
                self.db.set_deadline_watch_status(w["id"], "expired")
                log.info(
                    "[deadline] watch on %s expired after %d day(s) with no answer",
                    w["issue_identifier"], age_days,
                )

    async def _find_deadline_answer(self, watch: dict, today_iso: str) -> Optional[tuple]:
        """Look for a committed deadline for one watch, in priority order:
        (1) the assignee's Discord REPLY to the tag, (2) a new issue COMMENT, (3) the
        STANDUP notes. Returns (YYYY-MM-DD, source_label) or None. Read-only."""
        min_conf = config.COS_CLARIFY_MIN_CONFIDENCE

        # 1) Discord reply to the nudge.
        reply_text = await self._latest_reply_to(
            watch.get("channel_id"), watch.get("nudge_message_id")
        )
        if reply_text:
            v = await self.classifier.extract_deadline(text=reply_text, today=today_iso)
            if v and v["date"] and v["confidence"] >= min_conf:
                return v["date"], "Discord reply"

        # 2) A comment on the issue left since we asked.
        asked_at = followups.from_ts(watch["asked_at"])
        comments = await self.linear.list_comments(watch["issue_uuid"])
        recent = [c for c in comments if _iso_after(c.get("createdAt"), asked_at)]
        for c in reversed(recent[-5:]):  # newest few, newest first
            v = await self.classifier.extract_deadline(text=c.get("body", ""), today=today_iso)
            if v and v["date"] and v["confidence"] >= min_conf:
                return v["date"], "issue comment"

        # 3) The standup notes, matched to this issue's key.
        date_from_standup = await self._deadline_from_standup(
            watch["issue_identifier"], today_iso
        )
        if date_from_standup:
            return date_from_standup, "standup notes"
        return None

    async def _latest_reply_to(
        self, channel_id: Optional[int], nudge_message_id: Optional[int]
    ) -> Optional[str]:
        """The text of the most recent non-bot reply to our tag message, or None. Scans a
        bounded slice of channel history after the tag for messages whose reference is it."""
        if not channel_id or not nudge_message_id:
            return None
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.DiscordException:
                return None
        try:
            anchor = await channel.fetch_message(nudge_message_id)
        except discord.DiscordException:
            return None
        latest: Optional[str] = None
        try:
            async for m in channel.history(after=anchor, limit=200):
                if getattr(m.author, "bot", False):
                    continue
                ref_id = getattr(m.reference, "message_id", None) if m.reference else None
                if ref_id == nudge_message_id and (m.content or "").strip():
                    latest = m.content  # keep going; the last match is the freshest
        except discord.DiscordException:
            log.exception("[deadline] history scan failed for channel %s", channel_id)
            return None
        return latest

    async def _deadline_from_standup(
        self, issue_identifier: str, today_iso: str
    ) -> Optional[str]:
        """A deadline for `issue_identifier` mentioned in the most recent standup note, or
        None. Matches lines that name the issue key, then extracts a date from them."""
        if not standup.is_configured():
            return None
        note = await asyncio.to_thread(standup.read_standup)
        if not note:
            return None
        ident = (issue_identifier or "").lower()
        if not ident:
            return None
        candidates: list[str] = []
        for step in note.get("next_steps") or []:
            task = step.get("task") or ""
            if ident in task.lower():
                candidates.append(task)
        raw = note.get("raw") or ""
        if not candidates and ident in raw.lower():
            # Fall back to the block of raw text around the mention.
            candidates.append(raw)
        for text in candidates:
            v = await self.classifier.extract_deadline(text=text, today=today_iso)
            if v and v["date"] and v["confidence"] >= config.COS_CLARIFY_MIN_CONFIDENCE:
                return v["date"]
        return None

    # -- CoS: the periodic sweep -------------------------------------------

    async def _sweep_loop(self) -> None:
        """The heartbeat behind both proactive paths. Never lets an exception kill
        it — a bad row or a Discord blip must not silently end all follow-up."""
        await self.wait_until_ready()
        interval = max(1, config.COS_FOLLOWUP_CHECK_INTERVAL_MINUTES) * 60
        log.info("[sweep] loop started (every %d min)", interval // 60)
        while not self.is_closed():
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                log.info("[sweep] loop cancelled; exiting")
                raise
            except Exception:
                log.exception("[sweep] pass FAILED; continuing on the next tick")
            await asyncio.sleep(interval)

    async def _sweep_once(self) -> None:
        log.debug("[sweep] pass starting")
        if config.COS_FOLLOWUP_ENABLED:
            await self._nudge_due_open_threads()
        if config.COS_CLARIFY_ENABLED:
            await self._nudge_open_clarifications()
            await self._propose_timed_out_clarifications()
        if config.COS_TAG_ASSIGNEE_ENABLED or config.COS_ESCALATE_ENABLED:
            await self._audit_linear_gaps()
        if config.COS_UPDATE_DEADLINE_ENABLED:
            await self._resolve_deadline_watches()
        log.debug("[sweep] pass done")

    # -- plan execution ---------------------------------------------------

    async def _execute_plan(self, plan: dict) -> Optional[dict]:
        kind = plan.get("kind")
        log.info("[exec] dispatching plan=%s", kind)
        if kind in ("create", "create_needs_triage"):
            return await self._exec_create(plan)
        if kind in ("comment", "comment_dup"):
            return await self._exec_comment(plan, transition=False)
        if kind == "comment_transition":
            return await self._exec_comment(plan, transition=True)
        log.error("[exec] unknown plan kind %r — refusing", kind)
        return None

    async def _exec_create(self, plan: dict) -> Optional[dict]:
        # STAGE-1 RE-CHECK (idempotency): between proposal and this ✅, a sibling
        # report may already have produced an issue (e.g. its approval was clicked
        # first). If an OPEN issue now clearly matches this title, comment on it
        # instead of creating a duplicate — and say so in the confirmation.
        try:
            dup = await self._find_open_duplicate(plan.get("title") or "")
        except Exception:
            log.exception("[exec-create] dup re-check raised; proceeding to create")
            dup = None
        if dup:
            log.info(
                "[dedup] STAGE-1 REDIRECT create→comment: open issue %s matched at "
                "approval time; commenting instead of creating a second issue",
                dup.get("identifier"),
            )
            try:
                comment = await self.linear.add_comment(
                    dup["id"], self._comment_body_from_plan(plan)
                )
            except (LinearError, Exception):
                log.exception("[exec-create] redirect add_comment failed for %s", dup.get("identifier"))
                return None
            return {
                "linear_issue_id": dup["id"],
                "issue": dup,
                "redirected": True,
                "redirect_identifier": dup.get("identifier"),
                "redirect_url": dup.get("url"),
                "comment_url": comment.get("url"),
            }

        log.info(
            "[exec-create] step 1/2: title=%r labels=%s assignee=%s desc_len=%d",
            plan.get("title"),
            plan.get("label_names"),
            plan.get("assignee_id"),
            len(plan.get("description") or ""),
        )
        try:
            issue = await self.linear.create_issue(
                title=plan["title"],
                description=plan.get("description") or "",
                priority=plan.get("priority", "medium"),
                label_names=plan.get("label_names") or [],
                assignee_id=plan.get("assignee_id"),
            )
        except (LinearError, Exception):
            log.exception("[exec-create] create_issue failed")
            return None
        log.info(
            "[exec-create] step 2/2: DONE identifier=%s url=%s",
            issue.get("identifier"),
            issue.get("url"),
        )
        return {"linear_issue_id": issue["id"], "issue": issue}

    async def _exec_comment(
        self, plan: dict, *, transition: bool
    ) -> Optional[dict]:
        target_id = plan.get("target_issue_id")
        if not target_id:
            log.error("[exec-comment] plan missing target_issue_id")
            return None
        log.info(
            "[exec-comment] step 1/2: commenting on %s (transition=%s, signal=%s)",
            target_id,
            transition,
            plan.get("status_signal"),
        )
        try:
            comment = await self.linear.add_comment(target_id, plan.get("comment_body") or "")
        except (LinearError, Exception):
            log.exception("[exec-comment] add_comment failed for %s", target_id)
            return None

        transitioned_to: Optional[str] = None
        if transition:
            log.info(
                "[exec-comment] step 2/2: transitioning %s via signal=%s",
                target_id,
                plan.get("status_signal"),
            )
            # set_issue_status is best-effort and never raises. It returns the
            # updated issue only when it actually moved (→ Implemented / In
            # Progress); a missing/forbidden target state yields None, i.e. we
            # commented and left the status change to the PM.
            try:
                updated = await self.linear.set_issue_status(
                    target_id, plan.get("status_signal", "none")
                )
                if updated:
                    transitioned_to = (updated.get("state") or {}).get("name")
            except Exception:
                log.exception("[exec-comment] set_issue_status raised (continuing)")

        log.info(
            "[exec-comment] DONE target=%s comment_url=%s transitioned_to=%s",
            target_id,
            comment.get("url"),
            transitioned_to,
        )
        return {
            "linear_issue_id": target_id,
            "comment_url": comment.get("url"),
            "transitioned_to": transitioned_to,
        }

    def _format_result_message(self, plan: dict, result: dict) -> str:
        kind = plan["kind"]
        if kind in ("create", "create_needs_triage"):
            if result.get("redirected"):
                ident = result.get("redirect_identifier") or "?"
                url = result.get("redirect_url") or ""
                link = f"[{ident}]({url})" if url else ident
                return (
                    f"💬 An issue for this already existed — commented on **{link}** "
                    f"instead of creating a duplicate."
                )
            issue = result.get("issue", {}) or {}
            ident = issue.get("identifier") or "?"
            url = issue.get("url") or ""
            tag = " (needs triage)" if kind == "create_needs_triage" else ""
            link = f"[{ident}]({url})" if url else ident
            return f"✅ Created **{link}**{tag} — {issue.get('title', '')}"
        if kind in ("comment", "comment_transition", "comment_dup"):
            target = (
                plan.get("target_issue_identifier")
                or plan.get("target_issue_title")
                or "issue"
            )
            url = result.get("comment_url") or plan.get("target_issue_url") or ""
            link = f"[{target}]({url})" if url else target
            if kind == "comment_transition":
                moved = result.get("transitioned_to")
                if moved:
                    return f"💬 Commented on {link} and moved to **{moved}**"
                return f"💬 Commented on {link} — status change left to the PM"
            if kind == "comment_dup":
                return f"💬 Found likely duplicate — commented on {link}"
            return f"💬 Commented on {link}"
        return f"✅ Done: {kind}"

    # -- query mode (read-only) ------------------------------------------

    def _listens_for_query(self, channel_id: int) -> bool:
        """Channels where query mode is active: the monitored channels (queries
        allowed via @-mention) plus the dedicated query channel — or, when no
        dedicated channel is set, the approval channel (fallback)."""
        if channel_id in config.MONITORED_CHANNEL_IDS:
            return True
        return channel_id == config.query_channel_id()

    def _is_query_trigger(self, message: discord.Message) -> bool:
        """The single source of truth for "should this message be handled as a
        query?" — shared by `on_message` and `on_raw_message_edit` so an edit
        re-fires for exactly the same messages a fresh send would. Deliberately
        does NOT gate on the reporter allowlist — query mode is open to anyone
        (unlike the report pipeline).

        In the DEDICATED query channel every non-bot human message is a potential
        query (the @-mention requirement is dropped). Everywhere else queries are
        allowed — the monitored channels, or the approval channel in fallback
        mode — an explicit @-mention of the bot is still required."""
        if message.author.bot:
            return False
        ch = message.channel.id
        if not self._listens_for_query(ch):
            return False
        if config.is_dedicated_query_channel(ch):
            return True
        return self._is_self_mentioned_explicitly(message)

    def _is_self_mentioned_explicitly(self, message: discord.Message) -> bool:
        """True if the bot's user is @-mentioned in the message TEXT.
        Reply-pings (which auto-include the bot in `message.mentions`) don't
        count — we only want intentional mentions."""
        if self.user is None or not message.content:
            return False
        me = self.user.id
        return f"<@{me}>" in message.content or f"<@!{me}>" in message.content

    def _strip_self_mention(self, content: str) -> str:
        if not content or self.user is None:
            return (content or "").strip()
        me = self.user.id
        text = content
        for pat in (f"<@{me}>", f"<@!{me}>"):
            text = text.replace(pat, "")
        return text.strip()

    async def _safe_reply(self, message: discord.Message, body: str) -> None:
        """Reply in the same channel, no @-ping on the original author.

        Discord silently drops anything past ~2000 chars per message, so a long
        answer is split on line boundaries (never mid-sentence) and sent as
        several messages in order: the first as a reply to the source message,
        the rest as plain sends to the same channel so it reads top-to-bottom.
        A pathologically long answer is clipped at QUERY_REPLY_MAX_MESSAGES with
        a note rather than flooding the channel."""
        chunks = _split_for_discord(body or "…")

        if len(chunks) > QUERY_REPLY_MAX_MESSAGES:
            log.info(
                "[query] reply is %d chunks; clipping to %d",
                len(chunks),
                QUERY_REPLY_MAX_MESSAGES,
            )
            chunks = chunks[:QUERY_REPLY_MAX_MESSAGES]
            note = "\n\n…(reply truncated — ask a narrower question for the rest)"
            last = chunks[-1]
            if len(last) + len(note) > QUERY_REPLY_CHUNK:
                last = last[: QUERY_REPLY_CHUNK - len(note)]
            chunks[-1] = last + note

        channel = message.channel
        for i, chunk in enumerate(chunks):
            try:
                if i == 0:
                    await message.reply(chunk, mention_author=False)
                else:
                    await channel.send(chunk)
            except discord.DiscordException:
                log.exception(
                    "[query] reply chunk %d/%d failed", i + 1, len(chunks)
                )
                return

    async def _send_social_reply(
        self, message: discord.Message, kind: str, text: str = ""
    ) -> None:
        """Reply on a NON-answer path — a greeting/small-talk opener, or the
        last-resort "I couldn't make progress on that" nudge — IN THE PERSONA'S
        VOICE. Every such reply goes through here, so the greeting and capability
        paths sound like the same colleague as a real answer. Read-only: this path
        looks nothing up."""
        reply = await self.classifier.social_reply(
            kind=kind,
            text=text or self._strip_self_mention(message.content),
            requester=_display(message.author),
        )
        await self._safe_reply(message, reply)

    async def _handle_query(self, message: discord.Message) -> bool:
        """Handle a message addressed to the bot.

        Routing, in order — the capability/help text is a LAST resort, never the
        default:
          - GREETING / small talk → a warm persona reply that offers what she can
            look into. Never the rigid "Try `...`" template.
          - QUESTION (recognised OR oddly phrased) → the read-only engine. We prefer
            the engine's own judgement: it holds every read tool (Linear, standup,
            leave, archive, Discord↔Linear links) and answers honestly when nothing
            applies, so handing it a question it might not answer is cheap — while
            bouncing a real question with help text is not.
          - REPORT (a bug/feature being filed) → the report pipeline (return False),
            unless we're in a query-only channel, which has no report path.
          - Genuinely UNCLEAR, or a question the engine couldn't make progress on →
            only THEN a capability nudge, and phrased in the persona's voice.

        The one exception to "everything goes to the engine": a Discord-scoped
        PERSON question keeps the dedicated local Discord scan + identity-resolution
        path.

        Returns True if the message was handled (replied to); False only when the
        report pipeline should be given a chance instead. READ-ONLY throughout —
        never creates, comments, or modifies anything.
        """
        log.info(
            "[query] step 1/4: msg=%s author=%s(%s) channel=#%s",
            message.id,
            _display(message.author),
            getattr(message.author, "id", "?"),
            getattr(message.channel, "name", "?"),
        )
        query_only = config.is_query_only_channel(message.channel.id)

        text = self._strip_self_mention(message.content)
        if not text:
            # A bare "@bot" is a wave, not a malformed query — greet, don't lecture.
            log.info("[route] msg=%s type=greeting (bare mention) → route=greeting", message.id)
            await self._send_social_reply(message, "greeting", text="")
            return True

        # Short-term memory: the recent turns in THIS channel/thread, so a
        # follow-up ("what about Ravi?") can inherit the prior intent. Fetched
        # once and handed to both the parser and the engine.
        history = self.memory.recent(message.channel.id)

        log.info(
            "[query] step 2/4: parsing question via classifier.parse_query (history=%d)",
            len(history),
        )
        parsed = None
        try:
            parsed = await self.classifier.parse_query(
                text=text,
                requester=_display(message.author),
                history=history,
            )
        except Exception:
            log.exception("[query] parse_query raised")
        if parsed is None:
            # No verdict at all. Don't bounce the user on an infrastructure failure:
            # if it reads like a question, still let the engine try.
            if _looks_like_question(text):
                log.info(
                    "[route] msg=%s type=question (parse failed, question-shaped) "
                    "→ route=engine",
                    message.id,
                )
                if await self._handle_linear_engine_query(message, text, history=history):
                    return True
                log.info(
                    "[route] msg=%s engine made no progress → route=capability "
                    "(persona, last resort)",
                    message.id,
                )
                await self._send_social_reply(message, "unclear", text=text)
                return True
            log.warning(
                "[route] msg=%s parse_query gave no verdict and text isn't "
                "question-shaped → route=%s",
                message.id,
                "capability (persona, last resort)" if query_only else "report-pipeline",
            )
            return False

        kind = parsed.get("message_kind") or "question"
        intent = parsed.get("intent")
        source = (parsed.get("source") or "both").strip().lower()
        if source not in ("discord", "linear", "both"):
            source = "both"
        log.info(
            "[query] step 3/4: type=%s is_query=%s intent=%s source=%s parsed=%s",
            kind, bool(parsed.get("is_query")), intent, source, parsed,
        )

        # GREETING / small talk → warm persona reply. Never the help template.
        if kind == "greeting":
            log.info("[route] msg=%s type=greeting → route=greeting (persona reply)", message.id)
            await self._send_social_reply(message, "greeting", text=text)
            log.info("[query] DONE msg=%s (greeting)", message.id)
            return True

        # REPORT — a bug/feature being FILED at the bot. That's the report pipeline's
        # job, not the engine's. In a query-only channel there IS no report pipeline,
        # so fall through to the persona nudge at the bottom instead.
        if kind == "report" and not query_only:
            log.info("[route] msg=%s type=report → route=report-pipeline", message.id)
            return False

        # QUESTION — recognised OR merely question-shaped OR tagged a question by the
        # parser even though it couldn't map an intent. All of these go to the engine:
        # if any tool could plausibly answer it, let the engine try.
        if kind == "question" or parsed.get("is_query") or _looks_like_question(text):
            # The ONE dedicated path: a Discord-scoped PERSON question keeps the local
            # Discord scan + identity resolution (person activity from monitored-channel
            # history). Everything else — issue status/lists, STANDUP notes, leave,
            # links, unscoped person questions — flows through the engine, which decides
            # which read tools to call. Standup questions in particular MUST reach the
            # engine: it is the only path holding read_standup / list_standups.
            if parsed.get("is_query") and source == "discord" and intent == "person_activity":
                person = (
                    parsed.get("person")
                    or parsed.get("reporter")
                    or parsed.get("search_term")
                    or ""
                ).strip()
                log.info(
                    "[route] msg=%s type=question → route=discord-scan (intent=%s person=%r)",
                    message.id, intent, person,
                )
                await self._handle_person_activity(
                    message,
                    {**parsed, "intent": "person_activity", "source": "discord", "person": person},
                    question=text,
                )
                log.info("[query] DONE msg=%s (discord scan)", message.id)
                return True

            log.info(
                "[route] msg=%s type=question → route=engine (intent=%s source=%s)",
                message.id, intent, source,
            )
            if await self._handle_linear_engine_query(message, text, history=history):
                log.info("[query] DONE msg=%s (engine)", message.id)
                return True
            # The engine held every read tool and still couldn't make progress. THIS
            # is the only place a capability nudge is warranted — and it speaks as the
            # persona, not as a template.
            log.info(
                "[route] msg=%s engine made no progress → route=capability "
                "(persona, last resort)",
                message.id,
            )
            await self._send_social_reply(message, "unclear", text=text)
            log.info("[query] DONE msg=%s (capability)", message.id)
            return True

        # Genuinely unclear (or a report in a query-only channel, which can't be filed
        # from here) → persona nudge. A monitored channel gives the report pipeline the
        # last word instead.
        if not query_only:
            log.info("[route] msg=%s type=%s → route=report-pipeline", message.id, kind)
            return False
        log.info(
            "[route] msg=%s type=%s → route=capability (persona, last resort)",
            message.id, kind,
        )
        await self._send_social_reply(message, "unclear", text=text)
        log.info("[query] DONE msg=%s (capability)", message.id)
        return True

    async def _handle_linear_engine_query(
        self,
        message: discord.Message,
        text: str,
        history: Optional[list[dict]] = None,
    ) -> bool:
        """Answer a Linear-oriented question via the read-only tool-use engine.

        Returns True if the engine produced an answer (including an honest "nothing
        matched" — that IS an answer), False if it made no progress at all, so the
        caller can fall back to a persona-voiced nudge instead of a canned one.

        Best-effort resolves the asker to a Linear user id up front so the engine
        can honour "my"/"me" without a round-trip. `history` (recent turns in this
        channel) is replayed to the engine so a follow-up resolves against it, and
        the resulting exchange is stored back into short-term memory. READ-ONLY —
        the engine only holds read tools, so this can never create/comment/modify
        anything."""
        requester_name = _display(message.author)
        requester_linear_id: Optional[str] = None
        try:
            requester_linear_id = await self.linear.resolve_assignee(
                discord_user_id=getattr(message.author, "id", 0),
                display_name=requester_name,
                discord_linear_map=config.DISCORD_LINEAR_MAP,
            )
        except Exception:
            log.exception("[query.engine] requester resolution raised; continuing without id")

        log.info(
            "[query.engine] msg=%s requester=%s linear_id=%s q=%r",
            message.id, requester_name, requester_linear_id, text[:160],
        )
        try:
            reply = await self.query_engine.answer(
                question=text,
                requester_name=requester_name,
                requester_linear_id=requester_linear_id,
                extra_tools=(
                    self._linking_tools(message)
                    + self._standup_tools(text)
                    + self._archive_tools()
                    + self._leave_tools()
                    + self._person_activity_tools(requester_name)
                ),
                history=history,
            )
        except Exception:
            log.exception("[query.engine] answer raised")
            reply = None

        if not reply:
            # No answer at all (API failure, or the engine had nothing to say). Do
            # NOT emit a canned "Try `NFT2-123`" template from here — return False and
            # let the caller nudge in the persona's voice.
            log.info("[query.engine] msg=%s produced no answer", message.id)
            return False

        await self._safe_reply(message, reply)
        # Remember this exchange so the NEXT question in this channel can build on
        # it (e.g. "what about Ravi?" after "what is Arun working on?").
        self.memory.record(message.channel.id, text, reply)
        return True

    # -- query mode: Discord ↔ Linear linking (read-only) ----------------

    def _linking_tools(self, message: discord.Message) -> list[dict]:
        """Per-request read-only tools that bridge the stored Discord↔Linear
        mapping (db.py) to the engine. Handlers close over the current message so
        `tracked_issue_for_message` can default to whatever this question replies
        to. Read-only: they only read the DB, Linear, and Discord history."""
        reply_ctx_id = (
            getattr(message.reference, "message_id", None)
            if message.reference
            else None
        )

        async def _source_message_for_issue(inp: dict):
            ident = str(inp.get("identifier") or "").strip()
            if not ident:
                return {"error": "source_message_for_issue requires 'identifier'"}
            issue = await self.linear.get_issue(ident)
            if not issue:
                return {"error": f"no Linear issue '{ident}'"}
            rows = self.db.get_messages_for_issue(issue.get("id"))
            base = {
                "identifier": issue.get("identifier"),
                "title": issue.get("title"),
                "url": issue.get("url"),
            }
            if not rows:
                return {
                    **base,
                    "linked_messages": [],
                    "note": (
                        "No stored Discord link — the bot did not create or update "
                        "this issue itself (links only exist for those)."
                    ),
                }
            return {
                **base,
                "linked_messages": [await self._describe_linked_message(r) for r in rows],
            }

        async def _tracked_issue_for_message(inp: dict):
            mid = self._coerce_message_id(inp.get("message_id"))
            if mid is None:
                mid = reply_ctx_id
            if mid is None:
                return {
                    "error": (
                        "No message referenced. Reply to a Discord message and ask, "
                        "or pass a message id / jump link."
                    )
                }
            link = self.db.get_issue_for_message(mid)
            if not link:
                return {
                    "tracked": False,
                    "message_id": str(mid),
                    "note": (
                        "No tracked Linear issue for this message — the bot didn't "
                        "file or update one from it."
                    ),
                }
            issue = await self.linear.get_issue(link["linear_issue_id"])
            return {
                "tracked": True,
                "message_id": str(mid),
                "identifier": (issue or {}).get("identifier"),
                "title": (issue or {}).get("title"),
                "state": (issue or {}).get("state"),
                "url": (issue or {}).get("url"),
                "db_status": link.get("status"),
                "linked_at": link.get("created_at"),
            }

        return [
            {
                "schema": {
                    "name": "source_message_for_issue",
                    "description": (
                        "Given a Linear issue identifier (e.g. NFT2-591), return the "
                        "originating Discord message(s) the bot created/updated the issue "
                        "from — author, timestamp, text snippet, and a jump link. Only "
                        "issues the bot itself filed/updated have a stored link; "
                        "'linked_messages' is empty otherwise."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "identifier": {
                                "type": "string",
                                "description": "Linear issue key, e.g. NFT2-591.",
                            }
                        },
                        "required": ["identifier"],
                    },
                },
                "handler": _source_message_for_issue,
            },
            {
                "schema": {
                    "name": "tracked_issue_for_message",
                    "description": (
                        "Given a Discord message (by id or jump link, or — if omitted — "
                        "the message the current question is a reply to), return the Linear "
                        "issue the bot filed/updated from it, or tracked=false if none. Use "
                        "for 'is this already tracked?'."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "message_id": {
                                "type": "string",
                                "description": (
                                    "Discord message id or jump URL. Omit to use the message "
                                    "this question replies to."
                                ),
                            }
                        },
                        "required": [],
                    },
                },
                "handler": _tracked_issue_for_message,
            },
        ]

    async def _describe_linked_message(self, row: dict) -> dict:
        """Turn a stored linkage row into a rich, model-friendly dict: author,
        timestamp, snippet, and a jump link — fetched live from Discord. Degrades
        gracefully (a 'note' instead) when the message is deleted or the channel
        is unreadable, so a missing message never breaks the answer."""
        try:
            channel_id = int(row["channel_id"])
            message_id = int(row["message_id"])
        except (TypeError, ValueError):
            return {"note": "malformed linkage row", "status": row.get("status")}

        out: dict = {
            "message_id": str(message_id),
            "channel_id": str(channel_id),
            "db_status": row.get("status"),
            "linked_at": row.get("created_at"),
        }

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.DiscordException:
                channel = None

        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if guild_id:
            out["jump_url"] = (
                f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            )
        out["channel"] = getattr(channel, "name", None)

        if channel is None:
            out["note"] = "channel not accessible to the bot"
            return out
        try:
            msg = await channel.fetch_message(message_id)
        except discord.NotFound:
            out["note"] = "original Discord message was deleted"
            return out
        except discord.DiscordException:
            out["note"] = "Discord message unreadable"
            return out

        out["jump_url"] = msg.jump_url or out.get("jump_url")
        out["author"] = _display(msg.author)
        out["timestamp"] = msg.created_at.isoformat()
        snippet = (msg.content or "").strip().replace("\n", " ")
        out["snippet"] = (snippet[:180] + "…") if len(snippet) > 180 else (snippet or "(no text)")
        return out

    @staticmethod
    def _coerce_message_id(val) -> Optional[int]:
        """Parse a Discord message id from a raw id string or a jump URL
        (…/channels/<guild>/<channel>/<message>). None if it isn't an id."""
        if not val:
            return None
        s = str(val).strip().rstrip("/")
        if s.isdigit():
            return int(s)
        tail = s.split("/")[-1]
        return int(tail) if tail.isdigit() else None

    # -- query mode: standup notes (read-only, query-only) ---------------

    def _standup_tools(self, question_text: str) -> list[dict]:
        """Per-request read-only tools exposing synced Gemini standup notes to the
        engine. READ-ONLY and QUERY-ONLY — standup data never feeds ticket
        creation. Handlers offload blocking file/subprocess work to threads,
        run a sync-on-demand for "today/this morning" questions when
        STANDUP_SYNC_CMD is set, resolve owner names to Linear users, and always
        surface freshness so a reply can state how current the data is."""

        async def _resolve_owner(name: str) -> Optional[dict]:
            """Best-effort owner display-name → Linear user, using the same
            resolver as elsewhere. Falls back to the first name for full-name
            invitees like "Shriraksha M" / "Samyuktha Bhaskar"."""
            nm = (name or "").strip()
            if not nm:
                return None
            try:
                res = await self.linear.resolve_member_id(nm)
                if isinstance(res, dict) and res.get("id") is None and " " in nm:
                    res = await self.linear.resolve_member_id(nm.split()[0])
            except Exception:
                log.exception("[standup] owner resolution raised for %r", nm)
                return None
            if isinstance(res, dict) and res.get("id"):
                return {"id": res["id"], "displayName": res.get("displayName")}
            return None

        async def _freshness_note() -> dict:
            latest_date, mtime = await asyncio.to_thread(standup.freshness)
            return {"latest_date_on_disk": latest_date, "synced_file_mtime": mtime}

        # Distinguishes "standup access isn't configured" (STANDUP_DIR unset OR the
        # folder is missing) from "configured but no note for that day". The former
        # must be reported as not-configured, never as "nothing was discussed".
        async def _not_configured() -> Optional[dict]:
            ok = await asyncio.to_thread(standup.is_configured)
            if ok:
                return None
            return {
                "enabled": False,
                "configured": False,
                "note": (
                    "Standup access isn't configured (STANDUP_DIR is unset or the "
                    "folder is missing) — I can't read standup notes."
                ),
            }

        async def _list_standups(inp: dict):
            not_cfg = await _not_configured()
            if not_cfg:
                return not_cfg
            try:
                days = int(inp.get("days") or 14)
            except (TypeError, ValueError):
                days = 14
            notes = await asyncio.to_thread(standup.list_standups, max(1, days))
            return {
                "enabled": True,
                "configured": True,
                "standups": notes,
                "freshness": await _freshness_note(),
            }

        async def _read_standup(inp: dict):
            not_cfg = await _not_configured()
            if not_cfg:
                return not_cfg

            # Sync-on-demand: only for clearly recent/today questions, and only when
            # a sync command is configured. Best-effort — we read regardless, and we
            # tell the model whether the sync RAN and whether it SUCCEEDED so a
            # failed/timed-out sync can be reported as "data may be stale".
            sync_ran = False
            sync_ok = False
            if config.STANDUP_SYNC_CMD and standup.wants_recent(question_text):
                sync_ran = True
                sync_ok = await asyncio.to_thread(
                    standup.sync_now, config.STANDUP_SYNC_CMD
                )

            date_arg = (inp.get("date") or None)
            session_arg = (inp.get("session") or None)
            note = await asyncio.to_thread(standup.read_standup, date_arg, session_arg)
            freshness = await _freshness_note()

            if not note:
                return {
                    "enabled": True,
                    "configured": True,
                    "found": False,
                    "sync_ran": sync_ran,
                    "sync_ok": sync_ok,
                    "freshness": freshness,
                    "note": (
                        "No matching standup on file"
                        + (" (even after an on-demand sync)" if sync_ran else "")
                        + (" — the on-demand sync FAILED, so data may be stale"
                           if sync_ran and not sync_ok else "")
                        + "."
                    ),
                }

            # Enrich next-step owners with their Linear identity (read-only).
            enriched = []
            for step in note.get("next_steps") or []:
                owner_linear = await _resolve_owner(step.get("owner_name") or "")
                enriched.append({**step, "owner_linear": owner_linear})
            note = {**note, "next_steps": enriched}

            # Which sessions exist for the SAME day, so the reply can say "the other
            # sync is also on file" when no session was explicitly requested.
            sessions_that_day = await asyncio.to_thread(
                standup.sessions_on, note.get("date")
            )
            chosen_session = (note.get("session") or "").upper()
            other_sessions = [s for s in sessions_that_day if s != chosen_session]

            return {
                "enabled": True,
                "configured": True,
                "found": True,
                "sync_ran": sync_ran,
                "sync_ok": sync_ok,
                "freshness": freshness,
                "sessions_available_that_day": sessions_that_day,
                "other_sessions_available": other_sessions,
                "standup": note,
            }

        return [
            {
                "schema": {
                    "name": "list_standups",
                    "description": (
                        "List recent team standup notes (synced 'Notes by Gemini' docs) as "
                        "[{date, session (AM/PM), title, path}], newest first, plus a "
                        "'freshness' block. Read-only; standup notes never affect Linear."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "days": {"type": "integer", "description": "Look-back window in days (default 14)."}
                        },
                        "required": [],
                    },
                },
                "handler": _list_standups,
            },
            {
                "schema": {
                    "name": "read_standup",
                    "description": (
                        "Read ONE standup note. Returns {found, standup:{date, session, summary, "
                        "decisions[], next_steps:[{owner_name, task, owner_linear}]}}, plus "
                        "'freshness' (latest_date_on_disk, synced_file_mtime), 'sync_ran'/'sync_ok' "
                        "(did an on-demand sync run and succeed), and — when no session was "
                        "requested — 'other_sessions_available' (e.g. ['AM'] means the AM sync for "
                        "the SAME day is also on file; mention it). If configured=false, standup "
                        "access isn't set up — say so, don't claim nothing was discussed. Pass a "
                        "'date' (YYYY-MM-DD) computed from today's date for 'today'/'yesterday'/a "
                        "weekday/an explicit date; omit it for the most recent on file. Filter by "
                        "'session' (AM=kick-off/morning, PM=wrap-up/evening) when the user names one. "
                        "When you use this data you MUST state which note you read (e.g. 'AM sync, "
                        "2026-07-08') and the freshness line. If found=false, say the requested "
                        "standup isn't on file yet — NEVER answer from a different day's note as if "
                        "it were the one asked for, and never imply a standup didn't happen."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "YYYY-MM-DD; omit for the most recent on file."},
                            "session": {"type": "string", "enum": ["AM", "PM"], "description": "Optional session filter."},
                        },
                        "required": [],
                    },
                },
                "handler": _read_standup,
            },
        ]

    # -- query mode: archive snapshot (read-only fallback) ---------------

    def _archive_tools(self) -> list[dict]:
        """Read-only tool over the frozen Done-issues snapshot. Used as a fallback
        when live Linear can't return an issue. Every result carries the mandatory
        provenance label so it's never mistaken for live data. Never feeds ticket
        creation."""

        async def _search_archive(inp: dict):
            if not self.archive.enabled:
                return {"enabled": False, "note": "Archive snapshot isn't configured (ARCHIVE_FILE unset/empty)."}
            q = str(inp.get("query") or inp.get("text") or inp.get("identifier") or "").strip()
            if not q:
                return {"error": "search_archive requires a 'query' (identifier or keywords)."}
            results = await asyncio.to_thread(self.archive.search, q)
            return {
                "enabled": True,
                "snapshot_through": self.archive.snapshot_through,
                "provenance_label": self.archive.label(),
                "results": results,
            }

        return [
            {
                "schema": {
                    "name": "search_archive",
                    "description": (
                        "Search the FROZEN archive snapshot of past Done issues by identifier "
                        "(e.g. NFT2-123) or keywords. Use this ONLY as a fallback when live Linear "
                        "(get_issue / search_issues) can't find an issue — it may have been "
                        "archived. Returns [{identifier, title, labels, priority, owner, "
                        "completed_date, url}] plus 'provenance_label'. You MUST append that "
                        "provenance_label (e.g. '(from archive snapshot, through 2026-07-07)') to "
                        "any answer that uses this data, and never present it as live — the "
                        "snapshot knows nothing archived after its through-date."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Issue identifier (NFT2-123) or title keywords."}
                        },
                        "required": ["query"],
                    },
                },
                "handler": _search_archive,
            }
        ]

    # -- query mode: holiday / leave channel (read-only) -----------------

    def _leave_tools(self) -> list[dict]:
        """Read-only tool over the holiday/leave channel. Context for delays and
        person-activity ("was X on leave?"). This channel is NOT monitored for
        triage and never produces a ticket."""

        async def _who_is_on_leave(inp: dict):
            if not config.HOLIDAY_CHANNEL_ID:
                return {"enabled": False, "note": "Holiday/leave channel isn't configured (HOLIDAY_CHANNEL_ID unset)."}
            try:
                days = int(inp.get("days") or 45)
            except (TypeError, ValueError):
                days = 45
            around = (inp.get("around_date") or None)
            entries = await query.who_is_on_leave(
                self, days=max(1, days), around_date=around
            )
            return {"enabled": True, "days": days, "around_date": around, "leave": entries}

        return [
            {
                "schema": {
                    "name": "who_is_on_leave",
                    "description": (
                        "Read recent OOO / on-leave / holiday posts from the team's leave channel "
                        "as [{person, dates, note, posted_at, jump_url}]. Use for 'why was X "
                        "delayed' or to check if someone was away around a date. Pass 'around_date' "
                        "(YYYY-MM-DD) to focus on one day, and 'days' for the look-back window "
                        "(default 45). Read-only context — leave posts are never Linear data."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "around_date": {"type": "string", "description": "YYYY-MM-DD to focus on (optional)."},
                            "days": {"type": "integer", "description": "Look-back window in days (default 45)."},
                        },
                        "required": [],
                    },
                },
                "handler": _who_is_on_leave,
            }
        ]

    # -- query mode: person recent Discord activity (read-only) ----------

    def _person_activity_tools(self, requester_name: str) -> list[dict]:
        """Per-request tool giving the engine one person's recent monitored-channel
        Discord posts (with a done/fixed/deployed flag). This is the missing piece
        that lets the engine cross-check "is this In-Progress ticket actually
        finished?" for a reasoned "what is X working on" answer. READ-ONLY."""

        async def _recent_discord_activity(inp: dict):
            name = str(inp.get("name") or "").strip()
            if name.lower() in ("", "me", "my", "myself", "i", "mine"):
                name = requester_name
            try:
                days = int(inp.get("days") or config.QUERY_DISCORD_LOOKBACK_DAYS)
            except (TypeError, ValueError):
                days = config.QUERY_DISCORD_LOOKBACK_DAYS
            return await query.person_recent_messages(
                self, linear=self.linear, name=name, days=max(1, days)
            )

        return [
            {
                "schema": {
                    "name": "recent_discord_activity",
                    "description": (
                        "Recent monitored-channel Discord posts by ONE person (resolves their "
                        "identity), newest first, each with a 'done_signal' flag for "
                        "'done/fixed/deployed'-style phrasing. Use it for 'what is X working on' "
                        "to CROSS-CHECK whether an In-Progress Linear ticket was actually "
                        "finished, and for what X has been doing lately. Returns {person, "
                        "messages:[{channel, timestamp, text, jump_url, done_signal}]} — or "
                        "{ambiguous, candidates} when the name is unclear (then ask which person)."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Person's name (or 'me' for the asker)."},
                            "days": {"type": "integer", "description": "Look-back window in days (default from config)."},
                        },
                        "required": ["name"],
                    },
                },
                "handler": _recent_discord_activity,
            }
        ]

    @staticmethod
    def _cutoff_iso(days: int) -> Optional[str]:
        """ISO-8601 timestamp for `now - days`, or None when days <= 0."""
        if days <= 0:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # -- query mode: person_activity (read-only) -------------------------

    async def _handle_person_activity(
        self, message: discord.Message, parsed: dict, question: str = ""
    ) -> None:
        """Answer "what is <person> up to?" — scoped to Discord, Linear, or
        both per `parsed["source"]`. A source-scoped question hits ONLY that
        source, so a Discord-only answer never carries a Linear section (and
        vice versa). An optional category (`parsed["labels"]`, e.g. Bug) narrows
        the result. Read-only throughout — never creates, comments, or modifies.

        `question` is the asker's original text; the substantive answer is stored
        back into short-term memory so the next follow-up can build on it."""
        source = (parsed.get("source") or "both").strip().lower()
        if source not in ("discord", "linear", "both"):
            source = "both"
        want_discord = source in ("discord", "both")
        want_linear = source in ("linear", "both")

        labels = [l for l in (parsed.get("labels") or []) if isinstance(l, str)]
        category = labels[0] if labels else ""

        person = (parsed.get("person") or "").strip()
        if person.lower() in ("", "me", "myself", "i"):
            # "what am I working on" — resolve against the asker themselves.
            person = _display(message.author)
        log.info("[query.person] step 1/4: resolving %r (source=%s category=%r)", person, source, category)

        try:
            resolution = await query.resolve_person(
                person, linear=self.linear, client=self
            )
        except Exception:
            log.exception("[query.person] resolve_person raised; replying with error")
            await self._safe_reply(
                message,
                "⚠️ Sorry — I couldn't finish that lookup; something broke on my end. "
                "The details are in my logs — try me again in a moment?",
            )
            return

        if resolution.get("ambiguous"):
            log.info("[query.person] ambiguous; asking for clarification")
            await self._safe_reply(message, self._format_ambiguous(person, resolution))
            return

        linear_user = resolution.get("linear_user")
        discord_user = resolution.get("discord_user")

        # A Linear-scoped question needs a Linear identity; a Discord-scoped one
        # can still scan by name even if the person never posted before.
        if want_linear and not want_discord and not linear_user:
            log.info("[query.person] linear-scoped but no Linear match")
            await self._safe_reply(
                message,
                f'I couldn\'t find anyone called "{person}" in Linear. '
                f"Try their exact Linear name or email.",
            )
            return
        if not linear_user and not discord_user and want_discord and want_linear:
            log.info("[query.person] no match in either system")
            await self._safe_reply(
                message,
                f'I couldn\'t find anyone called "{person}" in Linear or recent '
                f"Discord activity. Try their exact Linear or Discord name.",
            )
            return

        window_days = int(parsed.get("window_days", 0) or 0) or config.QUERY_DISCORD_LOOKBACK_DAYS
        log.info(
            "[query.person] step 2/4: linear=%s discord=%s window=%dd source=%s",
            (linear_user or {}).get("displayName"),
            (discord_user or {}).get("display_name"),
            window_days,
            source,
        )

        # LINEAR side — active/assigned issues updated within the window. Only
        # touched when the question is Linear-scoped (or unscoped).
        linear_issues: list[dict] = []
        if want_linear and linear_user and linear_user.get("id"):
            updated_after = self._cutoff_iso(window_days)
            try:
                linear_issues = await self.linear.active_issues_for_user(
                    linear_user["id"], updated_after, label_names=labels or None
                )
            except Exception:
                log.exception("[query.person] active_issues_for_user raised; continuing")
                linear_issues = []

        # DISCORD side — recent posts in monitored channels (id preferred). Only
        # touched when the question is Discord-scoped (or unscoped).
        discord_messages: list[dict] = []
        if want_discord:
            scan_id = (discord_user or {}).get("id")
            scan_name = None if scan_id else (discord_user or {}).get("display_name") or person
            try:
                discord_messages = await query.scan_recent_messages(
                    self,
                    author_id=scan_id,
                    author_name=scan_name,
                    days=window_days,
                    max_per_channel=config.QUERY_MAX_MESSAGES_PER_CHANNEL,
                )
            except Exception:
                log.exception("[query.person] scan_recent_messages raised; continuing")
                discord_messages = []

        log.info(
            "[query.person] step 3/4: linear_issues=%d discord_messages=%d (source=%s)",
            len(linear_issues),
            len(discord_messages),
            source,
        )

        display = (
            (linear_user or {}).get("displayName")
            or (discord_user or {}).get("display_name")
            or person
        )

        # Nothing in ANY in-scope source → say so explicitly, naming what was
        # checked, rather than emitting a bare "<Person> — what they're working
        # on" header with empty sections (which reads like a broken reply).
        if not linear_issues and not discord_messages:
            scopes = []
            if want_linear:
                scopes.append("Linear (assigned/active)")
            if want_discord:
                scopes.append(f"Discord (last {window_days} days, monitored channels)")
            log.info(
                "[query.person] nothing found for %r in %s", display, " or ".join(scopes)
            )
            await self._safe_reply(
                message,
                f"I checked {' and '.join(scopes)} and came up empty for "
                f"**{display}** — nothing there right now.",
            )
            return

        # SYNTHESIS — one concise reply covering ONLY the source(s) in scope.
        note_parts = []
        if want_discord:
            note_parts.append(f"Discord: last {window_days} days, monitored channels only.")
        if want_linear:
            note_parts.append("Linear: active/assigned issues in the window.")
        if category:
            note_parts.append(f"Filtered to {category}.")
        coverage_note = " ".join(note_parts)

        reply = None
        try:
            reply = await self.classifier.summarize_person_activity(
                person=display,
                window_days=window_days,
                linear_issues=linear_issues,
                discord_messages=discord_messages,
                source=source,
                category=category,
                coverage_note=coverage_note,
            )
        except Exception:
            log.exception("[query.person] summarize raised; using fallback render")

        if not reply:
            reply = self._fallback_person_activity(
                display, window_days, linear_issues, discord_messages, coverage_note,
                source=source, category=category,
            )

        log.info("[query.person] step 4/4: replying")
        await self._safe_reply(message, reply)
        # Remember this exchange for follow-up resolution in this channel.
        self.memory.record(message.channel.id, question or self._strip_self_mention(message.content), reply)

    def _format_ambiguous(self, person: str, resolution: dict) -> str:
        """Clarification prompt listing the plausible matches — we do NOT guess.

        Persona-voiced (first person, warm), but rendered DETERMINISTICALLY rather
        than through `classifier.social_reply`: the candidate names are real people
        resolved from Linear/Discord, and a model rewrite could drop or invent one.
        The voice is the persona's; the facts stay exactly as resolved."""
        lines = [
            f'A couple of people could be "{person}" — which one do you mean?'
        ]
        for c in resolution.get("candidates", [])[:8]:
            if c.get("source") == "linear":
                label = c.get("displayName") or c.get("name") or "?"
                extra = f" ({c['email']})" if c.get("email") else ""
                lines.append(f"• {label}{extra} — _Linear_")
            else:
                label = c.get("display_name") or c.get("name") or "?"
                lines.append(f"• {label} — _Discord_")
        lines.append("_Give me the exact name and I'll take another look._")
        return "\n".join(lines)

    def _fallback_person_activity(
        self,
        person: str,
        window_days: int,
        linear_issues: list[dict],
        discord_messages: list[dict],
        coverage_note: str,
        *,
        source: str = "both",
        category: str = "",
    ) -> str:
        """Deterministic render used when the synthesis model call is
        unavailable. Same shape as the model output but scoped to `source`, so a
        Discord-only answer carries NO Linear block (and vice versa). No
        summarisation, just the facts we already hold — never invents anything."""
        source = (source or "both").strip().lower()
        want_linear = source in ("linear", "both")
        want_discord = source in ("discord", "both")

        # Compact, Discord-friendly render: bold labels (no headers), one line per
        # item, single line breaks (no blank lines between sections) so short
        # answers land in a single message.
        header = f"**{person} — working on**"
        if source == "discord":
            header = f"**{person} — recent Discord activity**"
        elif source == "linear":
            header = f"**{person} — Linear issues**"
        if category:
            header += f" _({category})_"
        lines = [header]

        if want_linear:
            lines.append("**Linear:**")
            if linear_issues:
                for i in linear_issues[:QUERY_LIST_LIMIT]:
                    ident = i.get("identifier") or "?"
                    title = (i.get("title") or "(untitled)").strip()
                    if len(title) > 80:
                        title = title[:77] + "…"
                    status = i.get("state_name") or "?"
                    url = i.get("url") or ""
                    link = f"[{ident}]({url})" if url else ident
                    lines.append(f"{link} — {title} · {status}")
                if len(linear_issues) > QUERY_LIST_LIMIT:
                    lines.append(f"_…and {len(linear_issues) - QUERY_LIST_LIMIT} more_")
            else:
                lines.append("_nothing in Linear_")

        if want_discord:
            lines.append(f"**Discord (last {window_days} days):**")
            if discord_messages:
                for m in discord_messages[:5]:
                    ts = m.get("timestamp")
                    when = ts.strftime("%b %d") if hasattr(ts, "strftime") else ""
                    snippet = (m.get("text") or "").strip().replace("\n", " ")
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "…"
                    jump = m.get("jump_url") or ""
                    ref = f" ([msg]({jump}))" if jump else ""
                    chan = m.get("channel") or "?"
                    lines.append(f"_{when}_ #{chan}: {snippet or '(no text)'}{ref}")
                if len(discord_messages) > 5:
                    lines.append(f"_(+{len(discord_messages) - 5} more)_")
            else:
                lines.append("_nothing in Discord_")

        if coverage_note:
            lines.append(f"_{coverage_note}_")
        return "\n".join(lines)
