"""Archive snapshot — a FROZEN markdown file of past Done issues.

READ-ONLY query-mode fallback: when live Linear can't return an issue (it was
archived, or simply not found), the engine can look it up here. It is a snapshot —
it knows nothing about anything archived after its through-date, and every
archive-sourced answer must be labelled as such so it's never mistaken for live
data (see `label()`).

Loaded and indexed ONCE (an `Archive` instance is held on the bot). Never writes
anything. Format-tolerant by design: the file may be a markdown table OR a
per-issue section list — both are parsed by anchoring on `NFT2-<n>` identifiers.
Degrades gracefully: an unset/missing/garbled file yields an empty, disabled index
rather than raising.
"""
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

# Linear workspace slug — observed in live issue URLs (…/nfthing/issue/NFT2-…).
# Used only to reconstruct a link when the archive row doesn't carry one.
_LINEAR_WORKSPACE = "nfthing"

_ID_RE = re.compile(r"\bNFT2-(\d+)\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_KNOWN_LABELS = ["Bug", "Feature", "Improvement", "BE", "FE", "UI"]
_PRIORITY_WORDS = ("urgent", "high", "medium", "low", "none")

# Field-label aliases for section-style entries ("- Owner: ravi").
_FIELD_ALIASES = {
    "title": ("title", "name", "summary"),
    "labels": ("labels", "label", "tags"),
    "priority": ("priority", "prio"),
    "owner": ("owner", "assignee", "assigned", "assigned to", "dev"),
    "completed": ("completed", "done", "closed", "completed date", "done date", "resolved"),
}
_ALIAS_TO_FIELD = {a: f for f, al in _FIELD_ALIASES.items() for a in al}

# An explicit snapshot date in the file header ("... through 2026-07-07",
# "as of 2026-07-07", "snapshot: 2026-07-07").
_THROUGH_RE = re.compile(r"(?i)\b(?:through|as of|snapshot|updated|generated)\b[^\n]*?(\d{4}-\d{2}-\d{2})")


def _norm_id(raw: str) -> Optional[str]:
    m = _ID_RE.search(raw or "")
    return f"NFT2-{int(m.group(1))}" if m else None


def _first_date(text: str) -> Optional[str]:
    m = _DATE_RE.search(text or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _all_dates(text: str) -> list[str]:
    return [f"{y}-{mo}-{d}" for (y, mo, d) in _DATE_RE.findall(text or "")]


def _extract_labels(text: str) -> list[str]:
    out: list[str] = []
    for lab in _KNOWN_LABELS:
        if re.search(rf"(?<![A-Za-z]){re.escape(lab)}(?![A-Za-z])", text or ""):
            out.append(lab)
    return out


def _extract_priority(text: str) -> Optional[str]:
    low = (text or "").lower()
    for p in _PRIORITY_WORDS:
        if re.search(rf"\b{p}\b", low):
            return p
    return None


class Archive:
    def __init__(self, path: str) -> None:
        self.path = (path or "").strip()
        self._by_id: dict[str, dict] = {}
        self._entries: list[dict] = []
        self.snapshot_through: Optional[str] = None
        self.enabled = False
        if self.path:
            self._load()

    # -- loading / indexing --------------------------------------------------

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            log.info("[archive] ARCHIVE_FILE %r not found; archive disabled", self.path)
            return
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            log.exception("[archive] could not read %r; archive disabled", self.path)
            return

        try:
            entries = self._parse(text)
        except Exception:
            log.exception("[archive] parse failed for %r; archive disabled", self.path)
            return

        self._entries = entries
        self._by_id = {e["identifier"]: e for e in entries}
        # Snapshot through-date: an explicit header date if present, else the
        # latest completed date across entries.
        header = _THROUGH_RE.search(text[:2000])
        entry_dates = [e["completed_date"] for e in entries if e.get("completed_date")]
        self.snapshot_through = (
            header.group(1) if header else (max(entry_dates) if entry_dates else None)
        )
        self.enabled = bool(entries)
        log.info(
            "[archive] loaded %d entries from %s (through=%s)",
            len(entries), self.path, self.snapshot_through,
        )

    def _parse(self, text: str) -> list[dict]:
        lines = text.splitlines()
        # A markdown table if there's a header row + separator row of pipes.
        if self._looks_like_table(lines):
            return self._parse_table(lines)
        return self._parse_blocks(lines)

    @staticmethod
    def _looks_like_table(lines: list[str]) -> bool:
        for i, ln in enumerate(lines):
            if "|" in ln and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
                # header + a |---|---| separator, and some row carries an id.
                if any(_ID_RE.search(l) for l in lines[i + 2 : i + 40]):
                    return True
        return False

    def _parse_table(self, lines: list[str]) -> list[dict]:
        # Find the header row (the line before the |---| separator).
        header_idx = next(
            (i for i in range(len(lines) - 1)
             if "|" in lines[i] and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1])),
            None,
        )
        if header_idx is None:
            return []
        headers = [h.strip().lower() for h in self._split_row(lines[header_idx])]
        col: dict[str, int] = {}
        for idx, h in enumerate(headers):
            if _ID_RE.search(h) or h in ("id", "identifier", "issue", "key"):
                col.setdefault("id", idx)
            for field, aliases in _FIELD_ALIASES.items():
                if any(a == h or a in h for a in aliases):
                    col.setdefault(field, idx)

        out: list[dict] = []
        for ln in lines[header_idx + 2 :]:
            if "|" not in ln or not _ID_RE.search(ln):
                continue
            cells = self._split_row(ln)
            ident = _norm_id(cells[col["id"]]) if "id" in col and col["id"] < len(cells) else _norm_id(ln)
            if not ident:
                continue

            def cell(field: str) -> str:
                i = col.get(field)
                return cells[i] if i is not None and i < len(cells) else ""

            title = cell("title") or self._title_after_id(ln, ident)
            labels_src = cell("labels") or ln
            owner = cell("owner").strip() or self._owner_guess(ln)
            completed = _first_date(cell("completed")) or _first_date(ln)
            out.append(self._entry(ident, title, labels_src, cell("priority") or ln, owner, completed, ln))
        return out

    @staticmethod
    def _anchor_id(line: str) -> Optional[str]:
        """The id that STARTS this line (after leading markup) — i.e. a heading /
        list item that begins a new entry. None for lines that merely mention an
        id mid-text (e.g. a URL `.../NFT2-360/...` or "see NFT2-100"), so those
        don't spuriously split an entry."""
        stripped = line.lstrip("#>*-_ |\t")
        m = _ID_RE.match(stripped)
        return _norm_id(m.group(0)) if m else None

    def _parse_blocks(self, lines: list[str]) -> list[dict]:
        # Group lines under the most recent start-of-line id anchor; merge repeats
        # of the same id into one block (preserving first-seen order).
        blocks: dict[str, list[str]] = {}
        order: list[str] = []
        current: Optional[str] = None
        for ln in lines:
            aid = self._anchor_id(ln)
            if aid:
                current = aid
                if aid not in blocks:
                    blocks[aid] = []
                    order.append(aid)
            if current is not None:
                blocks[current].append(ln)

        out: list[dict] = []
        for ident in order:
            block = blocks[ident]
            head = block[0]
            body = "\n".join(block)
            fields = self._section_fields(block)
            title = fields.get("title") or self._title_after_id(head, ident)
            labels_src = fields.get("labels") or body
            priority_src = fields.get("priority") or body
            owner = (fields.get("owner") or self._owner_guess(body)).strip()
            completed = _first_date(fields.get("completed") or "") or _first_date(body)
            out.append(self._entry(ident, title, labels_src, priority_src, owner, completed, body))
        return out

    # -- field helpers -------------------------------------------------------

    @staticmethod
    def _split_row(row: str) -> list[str]:
        cells = row.split("|")
        # Drop leading/trailing empties from the outer pipes.
        if cells and not cells[0].strip():
            cells = cells[1:]
        if cells and not cells[-1].strip():
            cells = cells[:-1]
        return [c.strip() for c in cells]

    @staticmethod
    def _section_fields(block: list[str]) -> dict[str, str]:
        """Parse "Field: value" / "- Field: value" / "**Field:** value" lines."""
        fields: dict[str, str] = {}
        for ln in block:
            m = re.match(r"^\s*[-*]?\s*\*{0,2}([A-Za-z ]{2,20})\*{0,2}\s*:\s*(.+?)\s*$", ln)
            if not m:
                continue
            key = _ALIAS_TO_FIELD.get(m.group(1).strip().lower())
            if key and key not in fields:
                fields[key] = m.group(2).strip()
        return fields

    @staticmethod
    def _title_after_id(line: str, ident: str) -> str:
        # Text following the id on its line, stripped of separators / markdown.
        tail = _ID_RE.sub("", line, count=1)
        tail = re.sub(r"^[\s#>*_|:—–-]+", "", tail).strip(" |")
        tail = re.sub(r"\s*\|.*$", "", tail)  # drop trailing table cells
        tail = re.sub(r"\*{1,2}", "", tail).strip(" —–-:")
        return tail.strip()

    @staticmethod
    def _owner_guess(text: str) -> str:
        m = re.search(r"@([A-Za-z][\w.\- ]{1,30})", text or "")
        return m.group(1).strip() if m else ""

    def _entry(self, ident, title, labels_src, priority_src, owner, completed, raw) -> dict:
        url = None
        um = _URL_RE.search(raw or "")
        if um:
            url = um.group(0).rstrip(").,")
        if not url:
            url = f"https://linear.app/{_LINEAR_WORKSPACE}/issue/{ident}"
        return {
            "identifier": ident,
            "title": (title or "").strip() or "(untitled)",
            "labels": _extract_labels(labels_src),
            "priority": _extract_priority(priority_src),
            "owner": (owner or "").strip().lstrip("@").strip() or None,
            "completed_date": completed,
            "url": url,
        }

    # -- public search -------------------------------------------------------

    def search(self, text_or_id: str, *, limit: int = 10) -> list[dict]:
        """Search the snapshot by identifier (exact) or free text (title/owner/
        labels substring). Returns [{identifier, title, labels, priority, owner,
        completed_date, url}]. [] when disabled or nothing matches."""
        if not self.enabled:
            return []
        q = (text_or_id or "").strip()
        if not q:
            return []

        ident = _norm_id(q)
        # Treat as an id lookup only when the query is essentially just the id.
        if ident and len(q) <= len(ident) + 2:
            hit = self._by_id.get(ident)
            return [self._public(hit)] if hit else []

        ql = q.lower()
        words = [w for w in re.split(r"\s+", ql) if w]
        scored: list[tuple[int, dict]] = []
        for e in self._entries:
            hay = " ".join(
                [e["identifier"], e["title"], e.get("owner") or "", " ".join(e["labels"])]
            ).lower()
            if all(w in hay for w in words):
                # Prefer title matches; recency (completed_date) breaks ties.
                score = (2 if ql in e["title"].lower() else 1)
                scored.append((score, e))
        scored.sort(key=lambda t: (t[0], t[1].get("completed_date") or ""), reverse=True)
        return [self._public(e) for _, e in scored[:limit]]

    @staticmethod
    def _public(e: dict) -> dict:
        return {
            "identifier": e["identifier"],
            "title": e["title"],
            "labels": e["labels"],
            "priority": e.get("priority"),
            "owner": e.get("owner"),
            "completed_date": e.get("completed_date"),
            "url": e["url"],
        }

    def label(self) -> str:
        """The mandatory provenance label for any archive-sourced answer."""
        through = self.snapshot_through or "an unknown date"
        return f"(from archive snapshot, through {through})"
