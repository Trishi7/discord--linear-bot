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
import re
from datetime import date, datetime, timedelta, timezone
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


# -- holiday / leave channel scan (read-only) --------------------------------

# Freeform leave-message detector — tolerant, matches how people actually post.
_LEAVE_RE = re.compile(
    r"\b(ooo|o\.o\.o|out of (the )?office|on leave|taking leave|going on leave|"
    r"day ?off|days ?off|off today|off tomorrow|be off|holiday|vacation|"
    r"annual leave|pto|sick leave|sick today|on holiday|be away|be out)\b",
    re.IGNORECASE,
)
# "Arun is on leave", "Ravi will be OOO", "Shreyansh: holiday Friday" → subject name.
_LEAVE_SUBJECT_RE = re.compile(
    r"^\s*@?(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b[^.\n]{0,30}?"
    r"\b(is|are|will be|on|has|takes?|taking|going|won'?t)\b",
)
_MONTHS = {
    m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\b")
_MONTH_DAY_RE = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b")


def _extract_leave_dates(text: str, ref: date) -> list[str]:
    """Best-effort ISO dates from a freeform leave message. Handles explicit
    YYYY-MM-DD, "2nd July" / "July 2", and today/tomorrow/yesterday relative to
    the message date. Ambiguous relative phrasing is left to the note text."""
    out: list[str] = []

    def add(y, mo, d):
        try:
            out.append(date(y, mo, d).isoformat())
        except ValueError:
            pass

    for y, mo, d in _ISO_DATE_RE.findall(text):
        add(int(y), int(mo), int(d))
    for d, mon in _DAY_MONTH_RE.findall(text):
        mo = _MONTHS.get(mon[:3].lower())
        if mo:
            add(ref.year, mo, int(d))
    for mon, d in _MONTH_DAY_RE.findall(text):
        mo = _MONTHS.get(mon[:3].lower())
        if mo:
            add(ref.year, mo, int(d))

    low = text.lower()
    if re.search(r"\btoday\b", low):
        out.append(ref.isoformat())
    if re.search(r"\btomorrow\b", low):
        out.append((ref + timedelta(days=1)).isoformat())
    if re.search(r"\byesterday\b", low):
        out.append((ref - timedelta(days=1)).isoformat())

    # De-dupe, keep order.
    seen: set[str] = set()
    uniq = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _is_leave_message(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 500:
        return False
    return bool(_LEAVE_RE.search(t))


async def who_is_on_leave(
    client: discord.Client,
    *,
    days: int = 45,
    around_date: Optional[str] = None,
    max_messages: int = 400,
) -> list[dict]:
    """Scan config.HOLIDAY_CHANNEL_ID (READ-ONLY) for OOO / on-leave posts over the
    last `days` and extract {person, dates, note, posted_at, jump_url}. Freeform-
    tolerant. When `around_date` (YYYY-MM-DD) is given, keep only entries whose
    parsed dates include it — or, when a message carries no parseable date, that
    were posted within ±3 days of it.

    This channel is NOT monitored for triage — nothing here creates a ticket.
    Returns [] when disabled/unreadable. Never raises."""
    channel_id = getattr(config, "HOLIDAY_CHANNEL_ID", 0)
    if not channel_id:
        return []
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException as e:
            log.info("[leave] holiday channel %s unreadable (%s)", channel_id, type(e).__name__)
            return []

    cutoff = _utcnow() - timedelta(days=max(1, int(days)))
    target: Optional[date] = None
    if around_date:
        try:
            target = date.fromisoformat(around_date.strip())
        except ValueError:
            target = None

    out: list[dict] = []
    try:
        async for msg in channel.history(after=cutoff, limit=int(max_messages), oldest_first=False):
            if getattr(msg.author, "bot", False):
                continue
            text = (msg.content or "").strip()
            if not _is_leave_message(text):
                continue
            ref = msg.created_at.date()
            dates = _extract_leave_dates(text, ref)
            # Prefer a named subject ("Arun is on leave"); else the poster.
            subj = _LEAVE_SUBJECT_RE.match(text)
            person = subj.group("name").strip() if subj else _display(msg.author)
            note = text if len(text) <= 240 else text[:237] + "…"
            out.append(
                {
                    "person": person,
                    "dates": dates,
                    "note": note,
                    "posted_at": msg.created_at.isoformat(),
                    "jump_url": msg.jump_url,
                }
            )
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("[leave] cannot read holiday channel %s: %s", channel_id, type(e).__name__)
        return []

    if target is not None:
        kept = []
        for e in out:
            if e["dates"]:
                if target.isoformat() in e["dates"]:
                    kept.append(e)
            else:
                try:
                    posted = datetime.fromisoformat(e["posted_at"]).date()
                    if abs((posted - target).days) <= 3:
                        kept.append(e)
                except ValueError:
                    pass
        out = kept

    log.info("[leave] %d leave message(s) (days=%d around=%s)", len(out), days, around_date)
    return out


# -- person recent Discord activity (read-only, for the reasoned status path) --

# Phrasing that signals an in-progress item may actually be finished — used to
# cross-check Discord against a still-"In Progress" Linear ticket.
_DONE_SIGNAL_RE = re.compile(
    r"\b(done|fixed|deployed|shipped|resolved|merged|released|completed|"
    r"pushed( it)? live|it'?s live|live now|good to go|ready for qa)\b",
    re.IGNORECASE,
)


def is_done_signal(text: str) -> bool:
    """True if a message reads like a completion signal ("done", "deployed", …)."""
    return bool(_DONE_SIGNAL_RE.search(text or ""))


async def person_recent_messages(
    client: discord.Client,
    *,
    linear,
    name: str,
    days: int = config.QUERY_DISCORD_LOOKBACK_DAYS,
) -> dict:
    """Resolve `name` to a Discord identity and return their recent monitored-
    channel messages (newest first), each flagged with `done_signal`. Read-only.

    Returns {person, ambiguous, candidates, messages:[{channel, timestamp, text,
    jump_url, done_signal}]}. On an ambiguous name it returns the candidates and no
    messages, so the engine can ask which person. Never raises."""
    try:
        resolution = await resolve_person(name, linear=linear, client=client)
    except Exception:
        log.exception("[query.person_recent] resolve_person raised for %r", name)
        resolution = {"ambiguous": False, "candidates": [], "discord_user": None, "linear_user": None}

    if resolution.get("ambiguous"):
        return {
            "person": name,
            "ambiguous": True,
            "candidates": resolution.get("candidates", []),
            "messages": [],
        }

    du = resolution.get("discord_user") or {}
    lu = resolution.get("linear_user") or {}
    scan_id = du.get("id")
    scan_name = None if scan_id else (du.get("display_name") or name)

    try:
        msgs = await scan_recent_messages(
            client, author_id=scan_id, author_name=scan_name, days=max(1, int(days))
        )
    except Exception:
        log.exception("[query.person_recent] scan_recent_messages raised for %r", name)
        msgs = []

    out: list[dict] = []
    for m in msgs[:25]:
        ts = m.get("timestamp")
        text = (m.get("text") or "").strip()
        snippet = text[:300] + ("…" if len(text) > 300 else "")
        out.append(
            {
                "channel": m.get("channel"),
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "text": snippet,
                "jump_url": m.get("jump_url"),
                "done_signal": is_done_signal(text),
            }
        )

    return {
        "person": du.get("display_name") or lu.get("displayName") or name,
        "ambiguous": False,
        "candidates": [],
        "messages": out,
    }
