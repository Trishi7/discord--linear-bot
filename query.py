"""Person-centric query building blocks (READ-ONLY).

Primitives for answering "what is <person> working on?" by combining Linear
assignments with recent Discord activity:

  - scan_recent_messages() — walk monitored channels for a given person's
    recent posts,
  - resolve_person()       — map a free-text name to BOTH a Linear user and a
    Discord user.

Nothing here mutates Discord or Linear. These are not yet wired into the
message handler — they're the data sources + identity layer the query router
will sit on top of. Errors are logged and swallowed; helpers return empty /
None rather than raising (same convention as the read paths in linear_client).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

import config

log = logging.getLogger(__name__)


def _display(user) -> str:
    """Best display label for a Discord user/member (mirrors bot._display)."""
    return (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or str(user)
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- Discord activity scan ---------------------------------------------------


async def scan_recent_messages(
    client: discord.Client,
    *,
    author_id: Optional[int] = None,
    author_name: Optional[str] = None,
    days: int = config.QUERY_DISCORD_LOOKBACK_DAYS,
    max_per_channel: int = config.QUERY_MAX_MESSAGES_PER_CHANNEL,
) -> list[dict]:
    """Scan config.MONITORED_CHANNEL_IDS ONLY for recent messages by one person.

    Matching: prefer an exact `author_id`; when no id is given, fall back to a
    case-insensitive display-name match against `author_name`. At most
    `max_per_channel` messages are scanned per channel (newest first, going back
    `days`), bounding API cost. Channels the bot can't read are logged and
    skipped — never raises.

    Returns a small list of dicts across all channels, newest first:
        {channel, timestamp, text, jump_url, attachment_urls}
    """
    if author_id is None and not (author_name or "").strip():
        log.warning("[query.scan] called with neither author_id nor author_name; nothing to match")
        return []

    cutoff = _utcnow() - timedelta(days=max(0, int(days)))
    wanted_name = (author_name or "").strip().lower()
    log.info(
        "[query.scan] step 1/2: author_id=%s author_name=%r days=%d max_per_channel=%d channels=%d",
        author_id, author_name, days, max_per_channel, len(config.MONITORED_CHANNEL_IDS),
    )

    out: list[dict] = []
    for cid in config.MONITORED_CHANNEL_IDS:
        channel = client.get_channel(cid)
        if channel is None:
            log.debug("[query.scan] channel %s not visible to the bot; skip", cid)
            continue
        channel_name = getattr(channel, "name", str(cid))
        scanned = matched = 0
        try:
            async for msg in channel.history(
                after=cutoff, limit=int(max_per_channel), oldest_first=False
            ):
                scanned += 1
                author = msg.author
                if author is None:
                    continue
                if author_id is not None:
                    if getattr(author, "id", None) != author_id:
                        continue
                elif _display(author).strip().lower() != wanted_name:
                    continue
                matched += 1
                out.append(
                    {
                        "channel": channel_name,
                        "timestamp": msg.created_at,
                        "text": (msg.content or "").strip(),
                        "jump_url": msg.jump_url,
                        "attachment_urls": [a.url for a in msg.attachments],
                    }
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(
                "[query.scan] cannot read #%s (%s): %s — skip", channel_name, cid, type(e).__name__
            )
            continue
        log.debug(
            "[query.scan] #%s: scanned %d, matched %d", channel_name, scanned, matched
        )

    out.sort(key=lambda r: r["timestamp"], reverse=True)
    log.info("[query.scan] step 2/2: %d matching message(s) across all channels", len(out))
    return out


# -- identity resolution -----------------------------------------------------


def _match_linear_member(member: dict, wanted: str) -> Optional[str]:
    """Return "exact" / "partial" / None for a Linear member vs. a wanted name.

    "exact"   — a name/displayName/email(-local) equals `wanted`, or `wanted`
                is the first token of the member's name/displayName.
    "partial" — `wanted` (len >= 2) is a substring of a name field or the
                email local-part.
    """
    fields = [member.get("displayName"), member.get("name")]
    email = (member.get("email") or "").strip().lower()
    local = email.split("@", 1)[0] if email else ""

    for f in fields:
        fl = (f or "").strip().lower()
        if not fl:
            continue
        if fl == wanted:
            return "exact"
        first = fl.split()[0] if fl.split() else fl
        if first and first == wanted:
            return "exact"
    if wanted and (email == wanted or local == wanted):
        return "exact"

    if len(wanted) >= 2:
        for f in fields:
            fl = (f or "").strip().lower()
            if fl and wanted in fl:
                return "partial"
        if local and wanted in local:
            return "partial"
    return None


def _match_discord_name(display: Optional[str], username: Optional[str], wanted: str) -> Optional[str]:
    """Return "exact" / "partial" / None for a Discord display/username vs. wanted."""
    for f in (display, username):
        fl = (f or "").strip().lower()
        if not fl:
            continue
        if fl == wanted:
            return "exact"
        first = fl.split()[0] if fl.split() else fl
        if first and first == wanted:
            return "exact"
    if len(wanted) >= 2:
        for f in (display, username):
            fl = (f or "").strip().lower()
            if fl and wanted in fl:
                return "partial"
    return None


def _linear_candidate(member: dict) -> dict:
    return {
        "source": "linear",
        "id": member.get("id"),
        "name": member.get("name"),
        "displayName": member.get("displayName"),
        "email": member.get("email"),
    }


def _discord_candidate(entry: dict) -> dict:
    return {
        "source": "discord",
        "id": entry.get("id"),
        "name": entry.get("name"),
        "display_name": entry.get("display_name"),
    }


def _lookup_discord_user(client: discord.Client, user_id: int) -> dict:
    """Best-effort {id, name, display_name} for a known Discord id, checking the
    user cache then guild members. Falls back to id-only when uncached."""
    user = client.get_user(user_id)
    if user is not None:
        return {"id": user_id, "name": getattr(user, "name", None), "display_name": _display(user)}
    for guild in getattr(client, "guilds", []) or []:
        member = guild.get_member(user_id)
        if member is not None:
            return {"id": user_id, "name": getattr(member, "name", None), "display_name": _display(member)}
    return {"id": user_id, "name": None, "display_name": None}


def _discord_id_for_linear_user(linear_user: dict) -> Optional[int]:
    """If `linear_user` appears as a VALUE (email or UUID) in
    config.DISCORD_LINEAR_MAP, return the mapped Discord id (the string key)."""
    email = (linear_user.get("email") or "").strip().lower()
    uid = str(linear_user.get("id") or "").strip().lower()
    for discord_id, ref in (config.DISCORD_LINEAR_MAP or {}).items():
        ref_norm = str(ref).strip().lower()
        if not ref_norm:
            continue
        if (email and ref_norm == email) or (uid and ref_norm == uid):
            try:
                return int(str(discord_id).strip())
            except (TypeError, ValueError):
                log.warning("[query.resolve] DISCORD_LINEAR_MAP key %r is not an int id; skip", discord_id)
                return None
    return None


async def _collect_discord_pool(
    client: discord.Client, *, days: int, max_per_channel: int
) -> dict[int, dict]:
    """Pool of candidate Discord identities: recent posters in monitored
    channels plus any cached guild members. id → {id, name, display_name}."""
    cutoff = _utcnow() - timedelta(days=max(0, int(days)))
    pool: dict[int, dict] = {}

    for cid in config.MONITORED_CHANNEL_IDS:
        channel = client.get_channel(cid)
        if channel is None:
            continue
        try:
            async for msg in channel.history(
                after=cutoff, limit=int(max_per_channel), oldest_first=False
            ):
                a = msg.author
                if a is None or getattr(a, "bot", False):
                    continue
                pool.setdefault(
                    a.id, {"id": a.id, "name": getattr(a, "name", None), "display_name": _display(a)}
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("[query.resolve] cannot read channel %s (%s); skip", cid, type(e).__name__)
            continue

    for guild in getattr(client, "guilds", []) or []:
        for m in getattr(guild, "members", []) or []:
            if getattr(m, "bot", False):
                continue
            pool.setdefault(
                m.id, {"id": m.id, "name": getattr(m, "name", None), "display_name": _display(m)}
            )

    return pool


async def resolve_person(
    name: str,
    *,
    linear,
    client: discord.Client,
    days: int = config.QUERY_DISCORD_LOOKBACK_DAYS,
    max_per_channel: int = config.QUERY_MAX_MESSAGES_PER_CHANNEL,
) -> dict:
    """Map a free-text `name` to BOTH a Linear user and a Discord user.

    Linear: match `name` against linear.list_team_members() by
    displayName / name / email, case-insensitive, allowing first-name / partial
    matches.

    Discord: if a Linear user was found AND it appears as a value in
    config.DISCORD_LINEAR_MAP (email or id), use the mapped Discord id as an
    exact match. Otherwise match `name` against recent monitored-channel
    posters' display names and guild members.

    Returns:
        {
          "query_name": str,
          "linear_user": {id, name, displayName, email} | None,
          "discord_user": {id, name, display_name} | None,
          "ambiguous": bool,
          "candidates": [ {source, ...}, ... ],
        }

    When several plausible matches exist on either side, `ambiguous` is True and
    the resolved side is left None with the options in `candidates`, so the
    caller can ask for clarification instead of guessing. Read-only; never
    raises — on failure the corresponding side resolves to None.
    """
    result = {
        "query_name": name,
        "linear_user": None,
        "discord_user": None,
        "ambiguous": False,
        "candidates": [],
    }
    wanted = (name or "").strip().lower()
    if not wanted:
        log.info("[query.resolve] empty name; nothing to resolve")
        return result

    log.info("[query.resolve] step 1/3: matching %r against Linear team members", name)
    try:
        members = await linear.list_team_members()
    except Exception:
        log.exception("[query.resolve] list_team_members raised; treating as no Linear match")
        members = []

    exacts, partials = [], []
    for m in members:
        verdict = _match_linear_member(m, wanted)
        if verdict == "exact":
            exacts.append(m)
        elif verdict == "partial":
            partials.append(m)
    linear_matches = exacts or partials

    if len(linear_matches) == 1:
        result["linear_user"] = _linear_candidate(linear_matches[0])
        # Drop the bookkeeping "source" key from the resolved user.
        result["linear_user"].pop("source", None)
        log.info("[query.resolve] Linear match → %s", result["linear_user"].get("displayName"))
    elif len(linear_matches) > 1:
        result["ambiguous"] = True
        result["candidates"].extend(_linear_candidate(m) for m in linear_matches)
        log.info("[query.resolve] %d Linear matches — ambiguous", len(linear_matches))
    else:
        log.info("[query.resolve] no Linear match for %r", name)

    # -- Discord side --------------------------------------------------------
    log.info("[query.resolve] step 2/3: resolving Discord identity")
    mapped_id: Optional[int] = None
    if result["linear_user"] is not None:
        mapped_id = _discord_id_for_linear_user(result["linear_user"])

    if mapped_id is not None:
        result["discord_user"] = _lookup_discord_user(client, mapped_id)
        log.info("[query.resolve] Discord via DISCORD_LINEAR_MAP → id=%s", mapped_id)
    else:
        try:
            pool = await _collect_discord_pool(client, days=days, max_per_channel=max_per_channel)
        except Exception:
            log.exception("[query.resolve] building Discord candidate pool raised; treating as no pool")
            pool = {}

        d_exacts, d_partials = [], []
        for entry in pool.values():
            verdict = _match_discord_name(entry.get("display_name"), entry.get("name"), wanted)
            if verdict == "exact":
                d_exacts.append(entry)
            elif verdict == "partial":
                d_partials.append(entry)
        discord_matches = d_exacts or d_partials

        if len(discord_matches) == 1:
            result["discord_user"] = {
                "id": discord_matches[0].get("id"),
                "name": discord_matches[0].get("name"),
                "display_name": discord_matches[0].get("display_name"),
            }
            log.info("[query.resolve] Discord name match → %s", result["discord_user"].get("display_name"))
        elif len(discord_matches) > 1:
            result["ambiguous"] = True
            result["candidates"].extend(_discord_candidate(e) for e in discord_matches)
            log.info("[query.resolve] %d Discord matches — ambiguous", len(discord_matches))
        else:
            log.info("[query.resolve] no Discord match for %r", name)

    log.info(
        "[query.resolve] step 3/3: DONE linear=%s discord=%s ambiguous=%s candidates=%d",
        bool(result["linear_user"]),
        bool(result["discord_user"]),
        result["ambiguous"],
        len(result["candidates"]),
    )
    return result
