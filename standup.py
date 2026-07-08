"""Standup notes — READ-ONLY context for QUERY MODE only.

A separate rclone process syncs "Notes by Gemini" standup docs from Google Drive
into a local folder (config.STANDUP_DIR). This module ONLY reads those local
files — it holds no Google credential and never writes anything to Drive. Nothing
here is ever an input to ticket creation; it exists purely so query mode can
answer "what did we decide this morning / what's on file".

Format-tolerant by design: the on-disk export format depends on how rclone is
configured (Google Docs commonly export to .docx, but .txt / .md / .html are also
common). `_extract_text` handles all of those with the stdlib only — no new deps.

Everything degrades gracefully: an unset/empty/missing STANDUP_DIR, an unreadable
file, or an unknown format yields [] / None rather than raising.
"""
import html
import logging
import os
import re
import subprocess
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

# A real Gemini notes doc's title/name contains "Notes by Gemini". The shorter
# "Notes - ..." stubs do NOT, so requiring this substring excludes them.
_GEMINI_MARKER = "notes by gemini"

# A date embedded in the title/name. rclone sanitises the "/" in a Google Docs
# title to a full-width slash "／" (or sometimes "_"), so accept any separator.
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[\s/／._-]{1,3}(\d{1,2})[\s/／._-]{1,3}(\d{1,2})(?!\d)")
# "(AM Sync)" / "(PM Sync)" or a bare AM/PM token; morning/evening as a fallback.
_SESSION_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)

# Text extensions we can read directly as UTF-8.
_TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".csv", ".log", ""}

# Section headers we recognise inside a Gemini note (canonical key → aliases).
_SECTION_ALIASES = {
    "summary": ("summary",),
    "details": ("details", "discussion", "notes"),
    "decisions": (
        "decisions", "aligned", "decisions and aligned", "decisions & aligned",
        "decisions / aligned", "key decisions", "agreements", "aligned on",
    ),
    "next_steps": ("next steps", "next step", "action items", "action item", "actions"),
}
# Reverse lookup: normalised header text → canonical key.
_HEADER_TO_KEY = {alias: key for key, aliases in _SECTION_ALIASES.items() for alias in aliases}

# A "[Name] task" next-step line (optionally bulleted).
_NEXT_STEP_RE = re.compile(r"^\s*[\*\-•·]?\s*\[(?P<owner>[^\]]+)\]\s*(?P<task>.+?)\s*$")
# A bullet line (for decisions / generic lists).
_BULLET_RE = re.compile(r"^\s*[\*\-•·]\s+(?P<text>.+?)\s*$")
# Boilerplate footer lines Gemini appends — never part of the real content.
_TRAILER_RE = re.compile(
    r"(?i)^\s*("
    r"you should review gemini|gemini can make mistakes|get tips and learn|"
    r"this (summary|report) was (created|generated)|review gemini'?s notes|"
    r".*gemini'?s notes to make sure)",
)

# Phrases that mean "I care about the freshest / today's standup" → sync first.
_WANTS_RECENT_RE = re.compile(
    r"\b(today|todays|this\s+morning|this\s+afternoon|this\s+evening|tonight|"
    r"just\s+now|latest|most\s+recent|stand[\s-]?up|this\s+sync|todays\s+sync)\b",
    re.IGNORECASE,
)

# How much raw text to hand back to the model as a fallback.
_RAW_MAX_CHARS = 4000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- text extraction (format-tolerant, stdlib only) --------------------------


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _docx_text(path: str) -> str:
    """Extract text from a .docx (a zip of XML) without python-docx: read
    word/document.xml and turn paragraph/break tags into newlines."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        log.debug("[standup] could not read docx %s", path, exc_info=True)
        return ""
    xml = re.sub(r"(?i)</w:p>", "\n", xml)
    xml = re.sub(r"(?i)<w:br\s*/?>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _extract_text(path: str) -> str:
    """Best-effort plain text from a note file. Returns "" on anything we can't
    read (e.g. .pdf — no stdlib extractor)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".docx":
            return _docx_text(path)
        if ext in (".html", ".htm"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return _strip_html(f.read())
        if ext in _TEXT_EXTS:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        log.debug("[standup] extract failed for %s", path, exc_info=True)
        return ""
    log.debug("[standup] unsupported extension %r for %s; skipping", ext, path)
    return ""


# -- filename / title parsing ------------------------------------------------


def _parse_date(text: str) -> Optional[date]:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_session(text: str) -> Optional[str]:
    m = _SESSION_RE.search(text or "")
    if m:
        return m.group(1).upper()
    low = (text or "").lower()
    if "morning" in low:
        return "AM"
    if "afternoon" in low or "evening" in low:
        return "PM"
    return None


def _looks_like_gemini(name_or_title: str) -> bool:
    return _GEMINI_MARKER in (name_or_title or "").lower()


def _candidate_files(standup_dir: str):
    """Yield (path, filename) for every regular file under STANDUP_DIR."""
    try:
        entries = sorted(os.listdir(standup_dir))
    except OSError:
        log.info("[standup] cannot list dir %r; treating as empty", standup_dir)
        return
    for name in entries:
        path = os.path.join(standup_dir, name)
        if os.path.isfile(path):
            yield path, name


def _meta_for_file(path: str, name: str) -> Optional[dict]:
    """Return {date, session, path, title} for a Gemini note file, or None if it
    isn't one (no Gemini marker, or no parseable date). Reads the file's first
    lines only when the filename alone lacks the marker/date."""
    stem = os.path.splitext(name)[0]

    head = ""
    marker = _looks_like_gemini(stem)
    d = _parse_date(stem)
    session = _parse_session(stem)

    # Fall back to the document's own title line(s) when the filename is terse
    # (e.g. rclone kept a plain name but the doc header carries marker/date).
    if not marker or d is None:
        head = _extract_text(path)[:600]
        if not marker:
            marker = _looks_like_gemini(head)
        if d is None:
            d = _parse_date(head)
        if session is None:
            session = _parse_session(head)

    if not marker or d is None:
        return None
    title = stem if _looks_like_gemini(stem) else (head.strip().splitlines() or [stem])[0][:160]
    return {"date": d.isoformat(), "session": session, "path": path, "title": title.strip()}


# -- public API --------------------------------------------------------------


def list_standups(days: int = 14, *, standup_dir: Optional[str] = None) -> list[dict]:
    """Parse STANDUP_DIR for Gemini notes docs from the last `days`. Returns
    [{date, session, path, title}] newest first. [] when disabled/empty."""
    standup_dir = _resolve_dir(standup_dir)
    if not standup_dir:
        return []
    cutoff = (_utcnow() - timedelta(days=max(0, int(days)))).date()
    out: list[dict] = []
    for path, name in _candidate_files(standup_dir):
        meta = _meta_for_file(path, name)
        if not meta:
            continue
        try:
            d = date.fromisoformat(meta["date"])
        except ValueError:
            continue
        if d < cutoff:
            continue
        out.append(meta)
    # Newest first; AM before PM within a day (AM < PM alphabetically, so reverse
    # date but keep session order by sorting date desc then session asc).
    out.sort(key=lambda m: (m["date"], m.get("session") or ""), reverse=True)
    log.info("[standup] list_standups(days=%d) → %d note(s)", days, len(out))
    return out


def _segment_sections(text: str) -> dict[str, list[str]]:
    """Split note text into {canonical_section: [lines]} by recognised headers.
    Lines before the first header go under "_preamble"."""
    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if _TRAILER_RE.match(line):
            continue  # drop Gemini's boilerplate footer wherever it appears
        norm = line.strip().rstrip(":").strip().lower()
        key = _HEADER_TO_KEY.get(norm)
        # Treat a short standalone header line as a section boundary.
        if key and len(line.strip()) <= 40:
            current = key
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _clean_bullets(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m = _BULLET_RE.match(ln)
        out.append(m.group("text").strip() if m else s)
    return out


def _parse_next_steps(lines: list[str]) -> list[dict]:
    """Parse the Next steps block into [{owner_name, task}] by splitting on the
    leading [Name] tag. Gemini writes one action item per line, so each line is
    its own item — a "[Name] task" line yields that owner; an untagged
    (non-boilerplate) line yields owner_name=None. Boilerplate is dropped upstream."""
    steps: list[dict] = []
    for ln in lines:
        if not ln.strip():
            continue
        m = _NEXT_STEP_RE.match(ln)
        if m:
            steps.append({"owner_name": m.group("owner").strip(), "task": m.group("task").strip()})
            continue
        cont = _BULLET_RE.match(ln)
        text = (cont.group("text") if cont else ln).strip()
        if text:
            steps.append({"owner_name": None, "task": text})
    return steps


def read_standup(
    date: Optional[str] = None,
    session: Optional[str] = None,
    *,
    standup_dir: Optional[str] = None,
) -> Optional[dict]:
    """Parse the matching standup doc. Returns
    {date, session, title, path, summary, decisions[], next_steps[{owner_name,
    task}], raw}. If `date` is None, uses the most recent. None when
    disabled/no match. Read-only."""
    standup_dir = _resolve_dir(standup_dir)
    if not standup_dir:
        return None
    notes = list_standups(days=3650, standup_dir=standup_dir)
    if not notes:
        return None

    if date:
        notes = [n for n in notes if n["date"] == date]
    if session:
        want = session.strip().upper()
        notes = [n for n in notes if (n.get("session") or "").upper() == want]
    if not notes:
        return None
    chosen = notes[0]  # already newest-first

    text = _extract_text(chosen["path"])
    sections = _segment_sections(text)
    summary_lines = [ln for ln in sections.get("summary", []) if ln.strip()]
    summary = " ".join(_clean_bullets(summary_lines)).strip()
    decisions = _clean_bullets(sections.get("decisions", []))
    next_steps = _parse_next_steps(sections.get("next_steps", []))

    raw = (text or "").strip()
    if len(raw) > _RAW_MAX_CHARS:
        raw = raw[:_RAW_MAX_CHARS].rstrip() + "\n…(truncated)"

    log.info(
        "[standup] read_standup(date=%s session=%s) → %s %s: summary=%d decisions=%d next_steps=%d",
        date, session, chosen["date"], chosen.get("session"),
        len(summary), len(decisions), len(next_steps),
    )
    return {
        "date": chosen["date"],
        "session": chosen.get("session"),
        "title": chosen.get("title"),
        "path": chosen["path"],
        "summary": summary,
        "decisions": decisions,
        "next_steps": next_steps,
        "raw": raw,
    }


def freshness(*, standup_dir: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (latest_date_on_disk_iso, newest_file_mtime_iso) so a reply can
    state how current the data is. (None, None) when disabled/empty."""
    standup_dir = _resolve_dir(standup_dir)
    if not standup_dir:
        return (None, None)
    notes = list_standups(days=3650, standup_dir=standup_dir)
    latest_date = notes[0]["date"] if notes else None

    newest_mtime = None
    try:
        mtimes = [
            os.path.getmtime(p) for p, _ in _candidate_files(standup_dir)
        ]
        if mtimes:
            newest_mtime = datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat()
    except OSError:
        log.debug("[standup] mtime scan failed", exc_info=True)
    return (latest_date, newest_mtime)


def wants_recent(question: str) -> bool:
    """True when the question is clearly about a recent/today standup, so a
    sync-on-demand is worth running before reading."""
    return bool(_WANTS_RECENT_RE.search(question or ""))


def sync_now(sync_cmd: str, *, timeout: float = 25.0) -> bool:
    """Run the rclone sync command (shell) to pull the freshest notes before a
    read. Best-effort: logs and returns False on any failure/timeout — never
    raises. Blocking; call via asyncio.to_thread from async code."""
    if not (sync_cmd or "").strip():
        return False
    log.info("[standup] sync-on-demand: running %r (timeout=%ss)", sync_cmd, timeout)
    try:
        proc = subprocess.run(
            sync_cmd, shell=True, timeout=timeout,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        log.warning("[standup] sync command timed out after %ss", timeout)
        return False
    except Exception:
        log.exception("[standup] sync command raised")
        return False
    if proc.returncode != 0:
        log.warning(
            "[standup] sync exited %s; stderr=%s", proc.returncode, (proc.stderr or "")[:300]
        )
        return False
    log.info("[standup] sync-on-demand complete")
    return True


def _resolve_dir(standup_dir: Optional[str]) -> str:
    """Resolve the effective standup dir: explicit arg, else config.STANDUP_DIR.
    Returns "" (disabled) when unset/empty or the path doesn't exist."""
    if standup_dir is None:
        try:
            import config
            standup_dir = config.STANDUP_DIR
        except Exception:
            standup_dir = ""
    standup_dir = (standup_dir or "").strip()
    if not standup_dir:
        return ""
    if not os.path.isdir(standup_dir):
        log.info("[standup] STANDUP_DIR %r does not exist; standup features disabled", standup_dir)
        return ""
    return standup_dir
