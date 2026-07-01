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

import config
import query
from classifier import Classifier
from db import DB
from linear_client import LinearClient, LinearError

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

# Human-readable form of a status_signal for embed / confirmation text.
_SIGNAL_HUMAN = {
    "resolved": "resolved",
    "in_progress": "in-progress",
    "cannot_reproduce": "cannot-reproduce",
}

# Linear workflow state TYPEs we consider "open" for duplicate detection.
_OPEN_STATE_TYPES = {"backlog", "unstarted", "started", "triage"}

# Query-mode mapping from a coarse state token (parsed `states[]`) to the set of
# Linear workflow state TYPEs to filter by. Unioned across the requested tokens
# by `_state_types_for`; an empty result means "no state filter".
_QUERY_STATE_TYPES = {
    "open": ["backlog", "unstarted", "started", "triage"],
    "closed": ["completed", "canceled"],
    "in_progress": ["started"],
    "done": ["completed"],
    "cancelled": ["canceled"],
}

# Cap on how many issues we list in a single query-mode reply.
QUERY_LIST_LIMIT = 10

# Discord single-message ceiling we render within (matches `_safe_reply`'s clip).
# When an issue description is requested we budget against this so the "full text
# in Linear" pointer always survives rather than being silently clipped away.
DISCORD_REPLY_BUDGET = 1900
# Hard cap on how much raw Linear description we inline before pointing to Linear.
DESCRIPTION_MAX_CHARS = 1500

# Phrasing that asks an issue_status question to enumerate EVERY matching issue
# ("all issues containing DMs", "list the DM tickets") rather than pinpoint one.
# When this matches we list matches instead of answering as a single issue.
_WANTS_ALL_MATCHES_RE = re.compile(
    r"\b(all|list|every|each|both|which\s+ones|what\s+are\s+the)\b", re.IGNORECASE
)

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
        log.info(
            "[bot.init] TriageBot ready (require_approval=%s, allowlist: %d ids + %d names)",
            config.REQUIRE_APPROVAL,
            len(config.ALLOWED_REPORTER_IDS),
            len(config.ALLOWED_REPORTER_NAMES),
        )

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s. Monitoring %d channels, posting to channel %s. require_approval=%s",
            self.user,
            len(config.MONITORED_CHANNEL_IDS),
            config.APPROVAL_CHANNEL_ID,
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
        # never become a ticket. Trigger: explicit @-mention of self in a
        # monitored channel OR the approval channel. Replies in the same
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
            # Fell through: parse_query said "not a Linear question". In a
            # monitored channel let the report pipeline have a go; in the
            # approval channel there's no report path, so nudge politely.
            if message.channel.id == config.APPROVAL_CHANNEL_ID:
                await self._safe_reply(
                    message,
                    "I only answer questions here. Try `what is Sid working on?`, "
                    "`list my open bugs`, or `status of NFT-123`.",
                )
                return

        # B) Drop non-monitored channels at debug-level so the terminal stays
        # readable. Approval channel ends here unless a query handled it above.
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
        # listens on (monitored OR approval).
        if not (
            payload.channel_id in config.MONITORED_CHANNEL_IDS
            or payload.channel_id == config.APPROVAL_CHANNEL_ID
        ):
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
            kind = "comment_transition" if status_signal != "none" else "comment"
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
        mentioned_names = list(verdict.get("mentioned_assignees") or [])
        assignee_id, intended_name, extras = await self._resolve_assignee_for_plan(
            thread=thread,
            mentioned_names=mentioned_names,
            needs_triage=needs_triage,
        )

        # E) Labels: category → label name + area_labels, in that order.
        label_names: list[str] = []
        cat_label = _CATEGORY_LABEL_NAME.get(category)
        if cat_label:
            label_names.append(cat_label)
        for area in verdict.get("area_labels") or []:
            if area and area not in label_names:
                label_names.append(area)

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
        )

        return {
            **base,
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
    ) -> tuple[Optional[str], Optional[str], list[str]]:
        """Returns (assignee_id, intended_name, extras).

        - No mentions → (None, None, []).
        - needs_triage → force unassigned, surface ALL mentions as extras.
        - Otherwise: try mentioned_names[0]. On resolve → (id, None, rest).
                     On miss → (None, primary, rest). Don't fall through to
                     mentioned_names[1] (per spec)."""
        if not mentioned_names:
            return (None, None, [])
        if needs_triage:
            return (None, None, list(mentioned_names))

        primary = mentioned_names[0]
        extras = list(mentioned_names[1:])
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
    ) -> str:
        parts: list[str] = [verdict["description"].strip() or "_(no description)_"]

        if needs_triage:
            parts.append("")
            parts.append(
                "⚠️ **Needs triage** — classifier was uncertain about the category; "
                "please verify and reassign."
            )

        parts.append("")
        parts.append("---")
        parts.append(f"**Raised by:** @{reporter}")
        if source_jump:
            parts.append(f"**Source:** {source_jump}")

        if len(thread) > 1:
            thread_text, _ = _format_thread(thread)
            parts.append("")
            parts.append("**Thread context:**")
            parts.append("```")
            parts.append(thread_text)
            parts.append("```")
        else:
            snippet = source_text.strip()
            if len(snippet) > 1500:
                snippet = snippet[:1497] + "…"
            if snippet:
                parts.append("")
                parts.append("**Original message:**")
                parts.append(f"> {snippet}")

        if attachments:
            parts.append("")
            parts.append("**Attachments:**")
            for a in attachments:
                ctype = (a.content_type or "").lower()
                kind = ctype.split("/", 1)[0] if "/" in ctype else "file"
                parts.append(f"- {kind}: [{a.filename}]({a.url})")

        if intended_assignee:
            parts.append("")
            parts.append(
                f"_Intended assignee:_ **@{intended_assignee}** "
                f"(no matching Linear user — left unassigned)"
            )

        if extras:
            parts.append("")
            parts.append(
                "_Also mentioned:_ " + ", ".join(f"**@{x}**" for x in extras)
            )

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
            signal = _SIGNAL_HUMAN.get(
                plan.get("status_signal", ""), plan.get("status_signal", "")
            )
            return f"Comment + mark {target} {signal}"
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

        if transition:
            log.info(
                "[exec-comment] step 2/2: transitioning %s via signal=%s",
                target_id,
                plan.get("status_signal"),
            )
            # set_issue_status is best-effort and never raises.
            try:
                await self.linear.set_issue_status(
                    target_id, plan.get("status_signal", "none")
                )
            except Exception:
                log.exception("[exec-comment] set_issue_status raised (continuing)")

        log.info(
            "[exec-comment] DONE target=%s comment_url=%s",
            target_id,
            comment.get("url"),
        )
        return {"linear_issue_id": target_id, "comment_url": comment.get("url")}

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
                signal = _SIGNAL_HUMAN.get(
                    plan.get("status_signal", ""), plan.get("status_signal", "")
                )
                return f"💬 Commented on {link} and marked **{signal}**"
            if kind == "comment_dup":
                return f"💬 Found likely duplicate — commented on {link}"
            return f"💬 Commented on {link}"
        return f"✅ Done: {kind}"

    # -- query mode (read-only) ------------------------------------------

    def _is_query_trigger(self, message: discord.Message) -> bool:
        """The single source of truth for "should this message be handled as a
        query?" — shared by `on_message` and `on_raw_message_edit` so an edit
        re-fires for exactly the same messages a fresh send would. A trigger is:
        a non-bot author, in a monitored channel OR the approval channel, with an
        explicit @-mention of the bot. Deliberately does NOT gate on the reporter
        allowlist — query mode is open to anyone (unlike the report pipeline)."""
        if message.author.bot:
            return False
        in_query_channel = (
            message.channel.id in config.MONITORED_CHANNEL_IDS
            or message.channel.id == config.APPROVAL_CHANNEL_ID
        )
        return in_query_channel and self._is_self_mentioned_explicitly(message)

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
        """Reply in the same channel, no @-ping on the original author."""
        # Discord message body cap ~2000 chars; clip with ellipsis.
        if len(body) > 1900:
            body = body[:1897] + "…"
        try:
            await message.reply(body, mention_author=False)
        except discord.DiscordException:
            log.exception("[query] reply failed")

    async def _handle_query(self, message: discord.Message) -> bool:
        """Handle an @-mention as a question — either "what is <person> working
        on?" (person_activity) or an issue list/lookup (issue_list).

        Returns True if the message was handled (replied to) as a query;
        False if it didn't read like a question and the report pipeline should
        be given a chance instead. READ-ONLY throughout — only reads from Linear
        (get_issue / list_issues / search_issues / active_issues_for_user /
        list_team_members) and Discord history; never creates, comments, or
        modifies anything.
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
        log.info("[query] step 3/4: intent=%s parsed=%s", intent, parsed)

        if intent == "person_activity":
            await self._handle_person_activity(message, parsed)
            log.info("[query] DONE msg=%s (person_activity)", message.id)
            return True

        if intent == "issue_status":
            await self._handle_issue_status(message, parsed)
            log.info("[query] DONE msg=%s (issue_status)", message.id)
            return True

        if intent == "issue_list":
            await self._handle_issue_list(message, parsed)
            log.info("[query] DONE msg=%s (issue_list)", message.id)
            return True

        # is_query=true but no actionable intent — treat as not-a-query so the
        # report pipeline (in a monitored channel) can have a go.
        log.info("[query] intent %r not actionable; letting report path try", intent)
        return False

    # -- query mode: issue_status (read-only) ----------------------------

    async def _handle_issue_status(
        self, message: discord.Message, parsed: dict
    ) -> None:
        """Answer "what's the status of X?" for ONE issue — by explicit key if
        given, otherwise by fuzzy subject search. Every branch replies with a
        useful message (never a bare null). Read-only — only reads from Linear.
        """
        identifier = (parsed.get("identifier") or "").strip()
        subject = (parsed.get("subject") or parsed.get("search_term") or "").strip()
        # "none" | "more" | "description" — how much of the issue body to show.
        detail = (parsed.get("detail") or "none").strip().lower()
        if detail not in ("none", "more", "description"):
            detail = "none"

        # 1) Explicit key wins — fetch directly, no search.
        if identifier:
            log.info("[query.issue_status] identifier=%r detail=%s → get_issue", identifier, detail)
            try:
                issue = await self.linear.get_issue(identifier)
            except Exception:
                log.exception("[query.issue_status] get_issue raised")
                issue = None
            if issue:
                await self._safe_reply(message, self._format_single_issue(issue, detail=detail))
            else:
                await self._safe_reply(
                    message, f"I couldn't find issue **{identifier}** in Linear."
                )
            return

        # 2) No key — need a subject to search on.
        if not subject:
            await self._safe_reply(
                message,
                "Which issue do you mean? Give me its key (e.g. `NFT-123`) or a few "
                "words from its title (e.g. `status of the payout bug`).",
            )
            return

        log.info("[query.issue_status] subject=%r → find_issues_by_text", subject)
        try:
            matches = await self.linear.find_issues_by_text(subject, include_closed=False)
        except Exception:
            log.exception("[query.issue_status] find_issues_by_text raised")
            await self._safe_reply(
                message, "⚠️ Sorry — Linear search failed. See bot logs."
            )
            return

        # 2a) Nothing open matched — be honest, name what was searched, offer closed.
        if not matches:
            await self._safe_reply(
                message,
                f'No **open** Linear issue matched "{subject}" — I searched issue '
                f"title keywords. It may be closed or titled differently. Want me to "
                f"include closed issues, or try different words?",
            )
            return

        # Did the asker want the WHOLE set ("all issues containing DMs", "list
        # the DM tickets")? Check the raw question and the parsed subject.
        wants_all = bool(
            _WANTS_ALL_MATCHES_RE.search(self._strip_self_mention(message.content))
            or _WANTS_ALL_MATCHES_RE.search(subject)
        )

        # 2b) Exactly one match and no "list them all" phrasing — answer fully
        # (fetch details so we can show the latest comment).
        if len(matches) == 1 and not wants_all:
            top = matches[0]
            issue = None
            ident = top.get("identifier")
            if ident:
                try:
                    issue = await self.linear.get_issue(ident)
                except Exception:
                    log.exception("[query.issue_status] detail fetch raised; using search hit")
            await self._safe_reply(
                message,
                self._format_single_issue(issue, detail=detail) if issue
                else self._format_single_issue(self._match_to_issue(top), detail=detail),
            )
            return

        # 2c) Several matches (or an explicit "list them all") — enumerate every
        # match with its current status. We don't force a disambiguation question.
        log.info(
            "[query.issue_status] listing %d match(es) (wants_all=%s)",
            len(matches), wants_all,
        )
        await self._safe_reply(
            message, self._format_status_matches(subject, matches)
        )

    @staticmethod
    def _match_to_issue(m: dict) -> dict:
        """Map a find_issues_by_text hit onto the _format_single_issue shape."""
        return {
            "identifier": m.get("identifier"),
            "title": m.get("title"),
            "state": m.get("state_name"),
            "url": m.get("url"),
            "assignee": m.get("assignee_name"),
            "labels": [],
            "latest_comment": None,
        }

    def _format_status_matches(self, subject: str, matches: list[dict]) -> str:
        """List every open issue matching `subject`, each with its identifier,
        title, and current status. Used when a status question matches several
        issues (or explicitly asks for all of them) — no disambiguation prompt."""
        total = len(matches)
        shown = matches[:QUERY_LIST_LIMIT]
        header = f'**{total}** open issue(s) match "{subject}":'
        lines = [header]
        for m in shown:
            ident = m.get("identifier") or "?"
            title = (m.get("title") or "(untitled)").strip()
            if len(title) > 80:
                title = title[:77] + "…"
            state = m.get("state_name") or "?"
            url = m.get("url") or ""
            link = f"[{ident}]({url})" if url else ident
            lines.append(f"• {link} — _{state}_ — {title}")
        if total > len(shown):
            lines.append(f"_(showing {len(shown)} of {total} — narrow the wording to trim.)_")
        lines.append("_Ask about any identifier (e.g. NFT-123) for the full detail + latest comment._")
        return "\n".join(lines)

    # -- query mode: issue_list (read-only) ------------------------------

    async def _handle_issue_list(self, message: discord.Message, parsed: dict) -> None:
        """reporter / date / label / state → issues, or a single-identifier
        lookup. Read-only.

        A Discord-scoped issue question ("what bugs did Harsh mention on
        discord") is NOT a Linear query — it's a request to summarise Discord.
        Redirect those to the person_activity Discord path so we never hit
        Linear for them."""
        source = (parsed.get("source") or "both").strip().lower()
        if source == "discord":
            reporter = (parsed.get("reporter") or "").strip()
            search_term = (parsed.get("search_term") or "").strip()
            person = reporter or search_term
            if not person:
                await self._safe_reply(
                    message,
                    "That looks like a Discord question — tell me *whose* messages "
                    "to summarise (e.g. `what bugs did Harsh mention on discord`).",
                )
                log.info("[query.issue_list] discord-scoped but no person; asked for one")
                return
            log.info("[query.issue_list] discord-scoped → person_activity(discord) person=%r", person)
            await self._handle_person_activity(
                message,
                {**parsed, "intent": "person_activity", "source": "discord", "person": person},
            )
            return

        log.info("[query.issue_list] step 4/4: running against Linear")
        try:
            issues, single = await self._run_issue_list_query(
                parsed, requester=message.author
            )
        except Exception:
            log.exception("[query.issue_list] raised; replying with error")
            await self._safe_reply(message, "⚠️ Sorry — Linear lookup failed. See bot logs.")
            return

        if single:
            reply = (
                self._format_single_issue(issues[0])
                if issues
                else "_(no matching Linear issue)_"
            )
        else:
            reply = self._format_issue_list(issues)
        await self._safe_reply(message, reply)

    async def _run_issue_list_query(
        self, parsed: dict, *, requester: discord.User
    ) -> tuple[list[dict], bool]:
        """Translate an issue_list query into get_issue / list_issues /
        search_issues. Returns (issues, is_single_lookup). Read-only."""
        identifier = (parsed.get("identifier") or "").strip()
        if identifier:
            issue = await self.linear.get_issue(identifier)
            return ([issue] if issue else []), True

        # Resolve "reporter" to a Linear user id (best-effort).
        creator_id: Optional[str] = None
        rep = (parsed.get("reporter") or "").strip()
        if rep:
            try:
                if rep.lower() == "me":
                    creator_id = await self.linear.resolve_assignee(
                        discord_user_id=getattr(requester, "id", 0),
                        display_name=_display(requester),
                        discord_linear_map=config.DISCORD_LINEAR_MAP,
                    )
                else:
                    creator_id = await self.linear.resolve_assignee(
                        discord_user_id=0,
                        display_name=rep,
                        discord_linear_map={},
                    )
            except Exception:
                log.exception("[query] reporter resolution raised for %r", rep)
                creator_id = None
            if not creator_id:
                # Asker explicitly asked for a person's issues but we can't
                # map them. Returning [] is more honest than listing everybody.
                log.info(
                    "[query] reporter %r could not be mapped to a Linear user; returning empty",
                    rep,
                )
                return [], False

        labels = parsed.get("labels") or []
        state_types = self._state_types_for(parsed.get("states") or [])
        created_after = self._cutoff_iso(int(parsed.get("window_days", 0) or 0))
        search_term = (parsed.get("search_term") or "").strip()

        # Pure free-text and nothing else → use the cheaper text search.
        if search_term and not (creator_id or labels or state_types or created_after):
            hits = await self.linear.search_issues(search_term)
            return [self._search_hit_to_issue(h) for h in hits[:QUERY_LIST_LIMIT]], False

        issues = await self.linear.list_issues(
            creator_id=creator_id,
            label_names=labels or None,
            state_types=state_types or None,
            created_after=created_after,
            limit=QUERY_LIST_LIMIT,
        )
        return issues, False

    @staticmethod
    def _state_types_for(states: list[str]) -> list[str]:
        """Union the coarse state tokens into Linear workflow state TYPEs.
        Empty (or anything mapping to no types) means "no state filter"."""
        out: list[str] = []
        for s in states or []:
            for t in _QUERY_STATE_TYPES.get(s, []):
                if t not in out:
                    out.append(t)
        return out

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

        header = f"**{person} — what they're working on**"
        if source == "discord":
            header = f"**{person} — recent Discord activity**"
        elif source == "linear":
            header = f"**{person} — Linear issues**"
        if category:
            header += f" _({category})_"
        lines = [header]

        if want_linear:
            lines += ["", "**Working on (Linear):**"]
            if linear_issues:
                for i in linear_issues[:QUERY_LIST_LIMIT]:
                    ident = i.get("identifier") or "?"
                    title = (i.get("title") or "(untitled)").strip()
                    if len(title) > 80:
                        title = title[:77] + "…"
                    status = i.get("state_name") or "?"
                    url = i.get("url") or ""
                    link = f"[{ident}]({url})" if url else ident
                    lines.append(f"• {link} — {title} — _{status}_")
            else:
                lines.append("_nothing in Linear_")

        if want_discord:
            lines.append("")
            lines.append(f"**Recent Discord activity (last {window_days} days):**")
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
                    lines.append(f"• _{when}_ #{chan}: {snippet or '(no text)'}{ref}")
                if len(discord_messages) > 5:
                    lines.append(f"_(+{len(discord_messages) - 5} more)_")
            else:
                lines.append("_nothing in Discord_")

        if coverage_note:
            lines.append("")
            lines.append(f"_{coverage_note}_")
        return "\n".join(lines)

    @staticmethod
    def _search_hit_to_issue(hit: dict) -> dict:
        """search_issues returns a thinner shape than list_issues — pad to the
        same keys so the formatter doesn't have to special-case."""
        return {
            "id": hit.get("id"),
            "identifier": hit.get("identifier"),
            "title": hit.get("title"),
            "url": hit.get("url"),
            "state": hit.get("state"),
            "state_type": None,
            "labels": [],
            "assignee": None,
            "creator": None,
            "latest_comment": None,
        }

    def _format_issue_list(self, issues: list[dict]) -> str:
        if not issues:
            return "_(no matching Linear issues)_"

        lines = [f"**{len(issues)}** matching issue(s):"]
        for issue in issues[:QUERY_LIST_LIMIT]:
            ident = issue.get("identifier") or "?"
            title = (issue.get("title") or "(untitled)").strip()
            if len(title) > 80:
                title = title[:77] + "…"
            state = issue.get("state") or "?"
            url = issue.get("url") or ""
            link = f"[{ident}]({url})" if url else ident
            lines.append(f"• {link} — _{state}_ — {title}")
        if len(issues) > QUERY_LIST_LIMIT:
            lines.append(f"_(showing {QUERY_LIST_LIMIT} of {len(issues)})_")
        return "\n".join(lines)

    def _format_single_issue(self, issue: dict, *, detail: str = "none") -> str:
        """Render one issue as a Discord reply.

        detail:
          - "none"        → summary line + assignee/labels + latest comment (as before).
          - "more"        → summary, then the issue DESCRIPTION under it.
          - "description" → LEAD with the full DESCRIPTION, then a compact summary.

        A long description is truncated to ~DESCRIPTION_MAX_CHARS (and further, if
        needed, to keep the whole reply within Discord's message ceiling) with a
        "…(full text in Linear)" pointer + URL — never silently dropped. An issue
        with no description says so explicitly when a description was requested."""
        ident = issue.get("identifier") or "?"
        title = (issue.get("title") or "(untitled)").strip()
        state = issue.get("state") or "?"
        url = issue.get("url") or ""
        link = f"[**{ident}**]({url})" if url else f"**{ident}**"
        assignee = issue.get("assignee") or "_unassigned_"

        summary = [
            f"{link} — _{state}_",
            f"**{title}**",
            f"Assignee: {assignee}",
        ]
        if issue.get("labels"):
            summary.append(f"Labels: {', '.join(issue['labels'])}")

        # Plain status check — original behaviour, no description block.
        if detail == "none":
            latest = issue.get("latest_comment")
            if latest and (latest.get("body") or "").strip():
                body = latest["body"].strip()
                if len(body) > 250:
                    body = body[:247] + "…"
                author = latest.get("author") or "?"
                summary += ["", f"_Latest comment by @{author}:_", f"> {body}"]
            return "\n".join(summary)

        # Description requested ("more" / "description") — budget the body against
        # the reply ceiling so the Linear pointer can't get clipped off the end.
        summary_text = "\n".join(summary)
        budget = DISCORD_REPLY_BUDGET - len(summary_text) - 40  # 40 ≈ headers/separators
        block = self._format_description_block(issue.get("description"), url, budget=budget)

        if detail == "description":
            # Lead with the description; compact summary follows.
            return f"{block}\n\n{summary_text}"
        # "more" — summary first, description underneath.
        return f"{summary_text}\n\n**Description:**\n{block}"

    @staticmethod
    def _format_description_block(
        description: Optional[str], url: str, *, budget: int
    ) -> str:
        """Render an issue description for inlining: trimmed to the smaller of
        DESCRIPTION_MAX_CHARS and `budget`, with a Linear pointer when cut.
        Empty/whitespace description → an explicit "(no description set)"."""
        desc = (description or "").strip()
        if not desc:
            return "_(no description set)_"
        limit = min(DESCRIPTION_MAX_CHARS, max(200, budget))
        if len(desc) <= limit:
            return desc
        truncated = desc[:limit].rstrip()
        pointer = (
            f"\n\n_…(truncated — full text in Linear: {url})_"
            if url
            else "\n\n_…(truncated — full text in Linear)_"
        )
        return truncated + pointer
