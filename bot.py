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
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

import archive as archive_mod
import config
import query
import standup
from classifier import Classifier
from db import DB
from linear_client import LinearClient, LinearError, SIGNAL_TO_STATE_NAME
from query_engine import QueryEngine

log = logging.getLogger(__name__)

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

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


def _display(user) -> str:
    return (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or str(user)
    )


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
        log.info(
            "[bot.init] TriageBot ready (require_approval=%s, allowlist: %d ids + %d names)",
            config.REQUIRE_APPROVAL,
            len(config.ALLOWED_REPORTER_IDS),
            len(config.ALLOWED_REPORTER_NAMES),
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
            # Fell through: parse_query said "not a Linear question". A query-only
            # channel (the dedicated query channel, or the approval channel in
            # fallback mode) has no report path, so nudge politely; a monitored
            # channel falls through to the report pipeline below.
            if config.is_query_only_channel(message.channel.id):
                await self._safe_reply(
                    message,
                    "I only answer questions here. Try `what is Sid working on?`, "
                    "`list my open bugs`, or `status of NFT-123`.",
                )
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

        # F) Approval gate.
        if config.REQUIRE_APPROVAL:
            await self._post_for_approval(message, plan)
        else:
            await self._execute_immediately(message, plan)

        log.info("[on_message] msg=%s DONE", message.id)

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
        """Conservative duplicate detector: normalised-title equality against
        an OPEN issue. Falls through to "no dup" on any error or ambiguity —
        we'd rather create a near-duplicate than silently dump a real new
        issue onto an unrelated old one."""
        title_norm = " ".join((title or "").lower().split())
        if not title_norm:
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

        for hit in hits:
            hit_title_norm = " ".join((hit.get("title") or "").lower().split())
            if hit_title_norm != title_norm:
                continue
            if hit.get("state") not in open_names:
                continue
            log.info(
                "[duplicate] clear match: %s '%s' state=%s",
                hit.get("identifier"),
                hit.get("title"),
                hit.get("state"),
            )
            return hit
        log.debug("[duplicate] %d hits, none clear-matched an open issue", len(hits))
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

    async def _post_for_approval(
        self, source: discord.Message, plan: dict
    ) -> Optional[discord.Message]:
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
                message_id=source.id,
                channel_id=source.channel.id,
                classification=plan,
                approval_message_id=msg.id,
            )
        except Exception:
            log.exception(
                "[_post_for_approval] DB record_pending failed for msg=%s", source.id
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

    async def _handle_query(self, message: discord.Message) -> bool:
        """Handle an @-mention as a question.

        `parse_query` classifies the question and, crucially, its `source`
        scope. Discord-scoped questions ("what did Harsh post") keep the local
        scan + identity-resolution path; anything touching Linear is handed to
        the read-only tool-driven `QueryEngine`, which chooses its own Linear
        reads (get_issue / get_issue_history / list_issues / search_issues /
        list_team_members).

        Returns True if the message was handled (replied to) as a query;
        False if it didn't read like a question and the report pipeline should
        be given a chance instead. READ-ONLY throughout — never creates,
        comments, or modifies anything.
        """
        log.info(
            "[query] step 1/4: msg=%s author=%s(%s) channel=#%s",
            message.id,
            _display(message.author),
            getattr(message.author, "id", "?"),
            getattr(message.channel, "name", "?"),
        )

        text = self._strip_self_mention(message.content)
        if not text:
            await self._safe_reply(
                message,
                "👋 Mention me with a question — e.g. "
                "`what is Sid working on?`, `list my open bugs`, "
                "`what's open with the Bug label`, or `status of NFT-123`.",
            )
            log.info("[query] msg=%s empty after stripping mention; help reply", message.id)
            return True

        log.info("[query] step 2/4: parsing question via classifier.parse_query")
        try:
            parsed = await self.classifier.parse_query(
                text=text,
                requester=_display(message.author),
            )
        except Exception:
            log.exception("[query] parse_query raised; letting report path try")
            return False
        if parsed is None:
            log.warning("[query] parse_query returned None; letting report path try")
            return False
        if not parsed.get("is_query"):
            log.info("[query] LLM said not a Linear question; letting report path try")
            return False

        intent = parsed.get("intent")
        source = (parsed.get("source") or "both").strip().lower()
        if source not in ("discord", "linear", "both"):
            source = "both"
        log.info("[query] step 3/4: intent=%s source=%s parsed=%s", intent, source, parsed)

        # ROUTING — Discord-scoped questions keep the existing Discord scan +
        # identity-resolution path (person activity from monitored-channel
        # history). Everything that touches Linear — issue status, issue lists,
        # AND the Linear side of a person question — now flows through the
        # tool-driven engine, which decides for itself which read-only Linear
        # tools to call. No more per-intent Linear handlers.
        if source == "discord":
            person = (
                parsed.get("person")
                or parsed.get("reporter")
                or parsed.get("search_term")
                or ""
            ).strip()
            await self._handle_person_activity(
                message,
                {**parsed, "intent": "person_activity", "source": "discord", "person": person},
            )
            log.info("[query] DONE msg=%s (discord scan)", message.id)
            return True

        # source == "linear" or "both" → tool-driven Linear engine.
        await self._handle_linear_engine_query(message, text)
        log.info("[query] DONE msg=%s (engine)", message.id)
        return True

    async def _handle_linear_engine_query(
        self, message: discord.Message, text: str
    ) -> None:
        """Answer a Linear-oriented question via the read-only tool-use engine.

        Best-effort resolves the asker to a Linear user id up front so the engine
        can honour "my"/"me" without a round-trip. READ-ONLY — the engine only
        holds read tools, so this can never create/comment/modify anything."""
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
            )
        except Exception:
            log.exception("[query.engine] answer raised")
            reply = None

        if not reply:
            reply = (
                "I couldn't find an answer to that in Linear. Try an issue key "
                "(e.g. `NFT2-123`), a subject (`status of the DMs issue`), or a "
                "filter (`open bugs assigned to Ravi`)."
            )
        await self._safe_reply(message, reply)

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

        async def _list_standups(inp: dict):
            if not config.STANDUP_DIR:
                return {"enabled": False, "note": "Standup notes aren't configured (STANDUP_DIR unset)."}
            try:
                days = int(inp.get("days") or 14)
            except (TypeError, ValueError):
                days = 14
            notes = await asyncio.to_thread(standup.list_standups, max(1, days))
            return {
                "enabled": True,
                "standups": notes,
                "freshness": await _freshness_note(),
            }

        async def _read_standup(inp: dict):
            if not config.STANDUP_DIR:
                return {"enabled": False, "note": "Standup notes aren't configured (STANDUP_DIR unset)."}

            # Sync-on-demand: only for clearly recent/today questions, and only
            # when a sync command is configured. Best-effort — read regardless.
            synced = False
            if config.STANDUP_SYNC_CMD and standup.wants_recent(question_text):
                synced = await asyncio.to_thread(
                    standup.sync_now, config.STANDUP_SYNC_CMD
                )

            date_arg = (inp.get("date") or None)
            session_arg = (inp.get("session") or None)
            note = await asyncio.to_thread(standup.read_standup, date_arg, session_arg)
            freshness = await _freshness_note()

            if not note:
                return {
                    "enabled": True,
                    "found": False,
                    "sync_attempted": synced,
                    "freshness": freshness,
                    "note": (
                        "No matching standup on file"
                        + (" (even after an on-demand sync)" if synced else "")
                        + "."
                    ),
                }

            # Enrich next-step owners with their Linear identity (read-only).
            enriched = []
            for step in note.get("next_steps") or []:
                owner_linear = await _resolve_owner(step.get("owner_name") or "")
                enriched.append({**step, "owner_linear": owner_linear})
            note = {**note, "next_steps": enriched}

            return {
                "enabled": True,
                "found": True,
                "sync_attempted": synced,
                "freshness": freshness,
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
                        "Read one standup note. Returns its summary, decisions/aligned bullets, "
                        "and next_steps as [{owner_name, task, owner_linear}], plus 'freshness' "
                        "(latest_date_on_disk, synced_file_mtime). Omit 'date' for the most "
                        "recent; optionally filter by 'session' (AM/PM). When you use this data "
                        "you MUST state what's on file (e.g. 'Latest standup on file: AM sync "
                        "2026-07-07'); if today's isn't present, say so — don't imply none happened."
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
        self, message: discord.Message, parsed: dict
    ) -> None:
        """Answer "what is <person> up to?" — scoped to Discord, Linear, or
        both per `parsed["source"]`. A source-scoped question hits ONLY that
        source, so a Discord-only answer never carries a Linear section (and
        vice versa). An optional category (`parsed["labels"]`, e.g. Bug) narrows
        the result. Read-only throughout — never creates, comments, or modifies."""
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
            await self._safe_reply(message, "⚠️ Sorry — person lookup failed. See bot logs.")
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
                f"Nothing found for **{display}** in {' or '.join(scopes)}.",
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

    def _format_ambiguous(self, person: str, resolution: dict) -> str:
        """Clarification prompt listing the plausible matches — we do NOT guess."""
        lines = [f'"{person}" is ambiguous — who do you mean?']
        for c in resolution.get("candidates", [])[:8]:
            if c.get("source") == "linear":
                label = c.get("displayName") or c.get("name") or "?"
                extra = f" ({c['email']})" if c.get("email") else ""
                lines.append(f"• {label}{extra} — _Linear_")
            else:
                label = c.get("display_name") or c.get("name") or "?"
                lines.append(f"• {label} — _Discord_")
        lines.append("_Reply with the exact name and I'll look again._")
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
