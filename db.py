"""SQLite-backed state.

Five jobs:
1. Dedup — if the same Discord message ID is seen twice (e.g. bot restart and
   replay), don't classify it again. Also powers cross-message dedup: two
   different messages about the SAME report must not spawn two approval embeds
   or two Linear issues (see list_recent_pending / list_recent_linked /
   record_merged).
2. Pending approvals — map an approval embed's message ID back to the source
   message + classification, so the reaction handler can act on a ✅/❌. Several
   source messages may share ONE approval_message_id when later reports are
   merged into an earlier pending approval (status='merged').
3. Audit — keep a row per classified message with its final status.
4. Clarifications — a report the bot asked ONE question about instead of
   proposing a half-complete ticket. The already-built plan is parked here until
   the reporter answers (then it's re-planned with the answer) or the ask times
   out (then the parked plan is proposed as-is — a report is never dropped just
   because nobody replied). See the `clarifications` table.
5. Open threads — commitments someone made in channel ("I'll confirm the DM date
   in an hour"), so an aged one can be surfaced back to that person as a REMINDER.
   Nothing here ever writes to Linear. See the `open_threads` table.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_id          TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    classification_json TEXT NOT NULL,
    approval_message_id TEXT,
    linear_issue_id     TEXT,
    status              TEXT NOT NULL,   -- pending | approved | rejected | merged
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_approval ON processed(approval_message_id);
CREATE INDEX IF NOT EXISTS ix_status ON processed(status);

-- One row per report the bot asked a clarifying question about. `plan_json` is
-- the FULL plan that would have gone to approval; it is parked, never executed
-- from here. status: awaiting (question asked, no answer yet) | answered
-- (re-planned with the answer and proposed) | expired (timed out — the parked
-- plan was proposed as-is) | cancelled.
CREATE TABLE IF NOT EXISTS clarifications (
    source_message_id   TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    ask_message_id      TEXT,
    reporter_id         TEXT,
    question            TEXT NOT NULL,
    plan_json           TEXT NOT NULL,
    status              TEXT NOT NULL,
    nudges_sent         INTEGER NOT NULL DEFAULT 0,
    last_nudged_at      TIMESTAMP,
    pm_notified         INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_clarify_channel ON clarifications(channel_id, status);
CREATE INDEX IF NOT EXISTS ix_clarify_ask ON clarifications(ask_message_id);

-- One row per commitment someone made in a monitored channel. `due_at` is when
-- we may first nudge (derived from what they said — "in an hour" → +1h). All
-- timestamps are UTC 'YYYY-MM-DD HH:MM:SS' strings, matching CURRENT_TIMESTAMP.
-- status: open (waiting) | closed (they followed up / someone ✅'d it) | stale
-- (nudged COS_FOLLOWUP_MAX_REMINDERS times with no answer — we stop pestering).
CREATE TABLE IF NOT EXISTS open_threads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT NOT NULL,
    message_id          TEXT NOT NULL UNIQUE,
    jump_url            TEXT,
    person_id           TEXT,
    person_name         TEXT NOT NULL,
    what                TEXT NOT NULL,
    promised_at         TIMESTAMP NOT NULL,
    due_at              TIMESTAMP NOT NULL,
    status              TEXT NOT NULL,
    reminders_sent      INTEGER NOT NULL DEFAULT 0,
    last_reminder_id    TEXT,
    last_reminded_at    TIMESTAMP,
    pm_notified         INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_open_due ON open_threads(status, due_at);
CREATE INDEX IF NOT EXISTS ix_open_reminder ON open_threads(last_reminder_id);

-- One row per nudge/escalation the bot has POSTED (see escalation.py). This table is
-- the RATE LIMIT: the bot now posts unprompted @-mentions, and the thing that stops it
-- becoming a nag is that it must check here first.
-- `target_key` identifies the audience ("assignee:<discord_id_or_name>" / "pm"), and
-- (target_key, issue_id, kind) is the identity of "this person, about this issue, for
-- this reason" — never repeated inside COS_TAG_COOLDOWN_HOURS.
CREATE TABLE IF NOT EXISTS nudges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key    TEXT NOT NULL,
    issue_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    level         TEXT NOT NULL,   -- assignee | pm
    channel_id    TEXT,
    message_id    TEXT,
    sent_at       TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_nudges_key ON nudges(target_key, issue_id, kind, sent_at);

-- One row per "close the loop" deadline watch (COS_UPDATE_DEADLINE_ENABLED). When the bot
-- tags an assignee about a MISSING DUE DATE, it records a watch here so a later sweep can
-- look for the answer (their Discord reply to the nudge, a new issue comment, or the
-- standup notes) and then set the due date + comment. Keyed UNIQUE on the nudge message so
-- one ask never spawns two watches. status: pending (waiting for an answer) | resolved
-- (a date was found and written / dry-run-logged) | expired (given up on).
CREATE TABLE IF NOT EXISTS deadline_watch (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_uuid        TEXT NOT NULL,          -- Linear internal id (for the write)
    issue_identifier  TEXT NOT NULL,          -- human key, e.g. NFT2-591 (for logs)
    assignee_key      TEXT,                   -- who we asked (rate-limit target key)
    channel_id        TEXT,
    nudge_message_id  TEXT UNIQUE,            -- the tag we're waiting on a reply to
    asked_at          TIMESTAMP NOT NULL,     -- UTC 'YYYY-MM-DD HH:MM:SS'
    status            TEXT NOT NULL,
    resolved_date     TEXT,                   -- YYYY-MM-DD once found
    resolved_source   TEXT,                   -- 'Discord reply' | 'issue comment' | 'standup notes'
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_deadline_status ON deadline_watch(status, asked_at);
"""


# Columns added after the original tables shipped. `CREATE TABLE IF NOT EXISTS` won't
# alter an existing table, so an already-created bot_state.db needs these back-filled.
# (table, column, DDL) — each added only if the table lacks it. Idempotent.
_MIGRATIONS = [
    ("open_threads", "pm_notified", "INTEGER NOT NULL DEFAULT 0"),
    ("clarifications", "nudges_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("clarifications", "last_nudged_at", "TIMESTAMP"),
    ("clarifications", "pm_notified", "INTEGER NOT NULL DEFAULT 0"),
]


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c) -> None:
        """Add any columns introduced after a table first shipped, so an existing DB is
        upgraded in place. Safe to run on every startup — each column is added only if
        it's missing."""
        for table, column, ddl in _MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def already_processed(self, message_id: int) -> bool:
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed WHERE message_id = ?", (str(message_id),)
            ).fetchone()
            return row is not None

    def record_pending(
        self,
        *,
        message_id: int,
        channel_id: int,
        classification: dict,
        approval_message_id: int,
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO processed
                    (message_id, channel_id, classification_json,
                     approval_message_id, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    str(message_id),
                    str(channel_id),
                    json.dumps(classification),
                    str(approval_message_id),
                ),
            )

    def record_merged(
        self,
        *,
        message_id: int,
        channel_id: int,
        classification: dict,
        approval_message_id: int,
    ) -> None:
        """Record a later message that was MERGED into an existing pending
        approval (a duplicate report). It shares the canonical approval's
        approval_message_id so mark_approved links it to the same issue, and it
        marks the message processed so it's never re-classified. status='merged'."""
        with self.conn() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO processed
                    (message_id, channel_id, classification_json,
                     approval_message_id, status)
                VALUES (?, ?, ?, ?, 'merged')
                """,
                (
                    str(message_id),
                    str(channel_id),
                    json.dumps(classification),
                    str(approval_message_id),
                ),
            )

    def list_recent_pending(self, channel_id: int, since: str) -> list[dict]:
        """Stage-1 dedup: PENDING approvals in one channel created at/after
        `since` (an SQLite 'YYYY-MM-DD HH:MM:SS' UTC timestamp), newest first.
        Returns [{message_id, approval_message_id, classification, created_at}].
        Only canonical rows (status='pending') — merged duplicates are excluded so
        a new report compares against live proposals, not other duplicates."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT message_id, approval_message_id, classification_json, created_at
                FROM processed
                WHERE channel_id = ? AND status = 'pending'
                  AND approval_message_id IS NOT NULL
                  AND created_at >= ?
                ORDER BY created_at DESC
                """,
                (str(channel_id), str(since)),
            ).fetchall()
            out: list[dict] = []
            for r in rows:
                try:
                    cls = json.loads(r["classification_json"])
                except (TypeError, ValueError):
                    cls = {}
                out.append(
                    {
                        "message_id": int(r["message_id"]),
                        "approval_message_id": int(r["approval_message_id"]),
                        "classification": cls,
                        "created_at": r["created_at"],
                    }
                )
            return out

    def list_recent_linked(self, channel_id: int, since: str) -> list[dict]:
        """Stage-2 dedup: messages in one channel already linked to a Linear issue
        (an issue we created or commented on), created at/after `since`, newest
        first. Returns [{message_id, linear_issue_id, classification, created_at}].
        Lets a plain follow-up (not a Discord reply) be matched to the issue a
        recent same-channel message is already tracking."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT message_id, linear_issue_id, classification_json, created_at
                FROM processed
                WHERE channel_id = ? AND linear_issue_id IS NOT NULL
                  AND created_at >= ?
                ORDER BY created_at DESC
                """,
                (str(channel_id), str(since)),
            ).fetchall()
            out: list[dict] = []
            for r in rows:
                try:
                    cls = json.loads(r["classification_json"])
                except (TypeError, ValueError):
                    cls = {}
                out.append(
                    {
                        "message_id": int(r["message_id"]),
                        "linear_issue_id": r["linear_issue_id"],
                        "classification": cls,
                        "created_at": r["created_at"],
                    }
                )
            return out

    def get_linkage_for_message(self, message_id: int) -> Optional[dict]:
        """Return {linear_issue_id, classification} for a Discord message that has
        already been linked to a Linear issue (either an issue we created from it,
        or an existing issue we commented on for this message). Returns None if
        the message wasn't processed or has no linked issue yet.

        Used by the follow-up path: when a new message replies to / sits in the
        same thread as one of these, the bot adds a Linear comment on the linked
        issue instead of creating a duplicate.
        """
        with self.conn() as c:
            row = c.execute(
                """
                SELECT linear_issue_id, classification_json
                FROM processed
                WHERE message_id = ? AND linear_issue_id IS NOT NULL
                """,
                (str(message_id),),
            ).fetchone()
            if not row:
                return None
            return {
                "linear_issue_id": row["linear_issue_id"],
                "classification": json.loads(row["classification_json"]),
            }

    def get_issue_for_message(self, message_id: int) -> Optional[dict]:
        """Read-only: the stored Discord→Linear link for ONE message — the Linear
        issue the bot created/updated from it. Returns {message_id, channel_id,
        linear_issue_id, status, created_at} or None when the message wasn't
        processed or isn't linked to an issue yet.

        `linear_issue_id` is Linear's INTERNAL UUID (recorded at approval time),
        not the human identifier — resolve it via Linear if you need "NFT2-123".
        """
        with self.conn() as c:
            row = c.execute(
                """
                SELECT message_id, channel_id, linear_issue_id, status, created_at
                FROM processed
                WHERE message_id = ? AND linear_issue_id IS NOT NULL
                """,
                (str(message_id),),
            ).fetchone()
            if not row:
                return None
            return {
                "message_id": row["message_id"],
                "channel_id": row["channel_id"],
                "linear_issue_id": row["linear_issue_id"],
                "status": row["status"],
                "created_at": row["created_at"],
            }

    def get_messages_for_issue(self, linear_issue_id: str) -> list[dict]:
        """Read-only: every stored Discord message linked to a Linear issue,
        matched on Linear's INTERNAL UUID (the id recorded at approval time).
        Returns a list of {message_id, channel_id, linear_issue_id, status,
        created_at}, newest first; [] when nothing links to it.

        Identifier↔UUID resolution is the caller's job: the DB never sees the
        "NFT2-123" identifier, so pass the internal id (get it from Linear).
        """
        if not linear_issue_id:
            return []
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT message_id, channel_id, linear_issue_id, status, created_at
                FROM processed
                WHERE linear_issue_id = ?
                ORDER BY created_at DESC
                """,
                (str(linear_issue_id),),
            ).fetchall()
            return [
                {
                    "message_id": r["message_id"],
                    "channel_id": r["channel_id"],
                    "linear_issue_id": r["linear_issue_id"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def get_by_approval(self, approval_message_id: int) -> Optional[dict]:
        # Several rows can share one approval_message_id (later duplicate reports
        # merged in). The CANONICAL plan is the earliest row — order by created_at
        # ASC so the reaction handler always executes/reads that one, not a merged
        # duplicate's copy.
        with self.conn() as c:
            row = c.execute(
                """
                SELECT message_id, channel_id, classification_json, status
                FROM processed WHERE approval_message_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (str(approval_message_id),),
            ).fetchone()
            if not row:
                return None
            return {
                "message_id": int(row["message_id"]),
                "channel_id": int(row["channel_id"]),
                "classification": json.loads(row["classification_json"]),
                "status": row["status"],
            }

    def mark_approved(self, approval_message_id: int, linear_issue_id: str) -> None:
        with self.conn() as c:
            c.execute(
                """
                UPDATE processed
                SET status = 'approved',
                    linear_issue_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE approval_message_id = ?
                """,
                (linear_issue_id, str(approval_message_id)),
            )

    def mark_rejected(self, approval_message_id: int) -> None:
        with self.conn() as c:
            c.execute(
                """
                UPDATE processed
                SET status = 'rejected',
                    updated_at = CURRENT_TIMESTAMP
                WHERE approval_message_id = ?
                """,
                (str(approval_message_id),),
            )

    # -- clarifications --------------------------------------------------
    # A report the bot asked ONE question about instead of proposing a
    # half-complete ticket. The plan is PARKED, never executed from here — it
    # only ever leaves via the approval embed (after an answer, or on timeout).

    def record_clarification(
        self,
        *,
        source_message_id: int,
        channel_id: int,
        ask_message_id: int,
        reporter_id: Optional[int],
        question: str,
        plan: dict,
    ) -> None:
        """Park `plan` and remember that we asked `question` about it. REPLACE (not
        IGNORE): if an earlier ask for this same source message is still on file,
        the newer one supersedes it — we never accumulate two open asks for one
        report."""
        with self.conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO clarifications
                    (source_message_id, channel_id, ask_message_id, reporter_id,
                     question, plan_json, status)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting')
                """,
                (
                    str(source_message_id),
                    str(channel_id),
                    str(ask_message_id),
                    str(reporter_id) if reporter_id else None,
                    question,
                    json.dumps(plan),
                ),
            )

    def get_clarification(self, source_message_id: int) -> Optional[dict]:
        """The clarification parked for ONE source message, whatever its status."""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM clarifications WHERE source_message_id = ?",
                (str(source_message_id),),
            ).fetchone()
            return self._clarification_row(row) if row else None

    def list_awaiting_clarifications(self, channel_id: int) -> list[dict]:
        """Every still-unanswered ask in one channel, NEWEST FIRST. The caller
        matches an incoming message against these to decide whether it's the
        answer it's been waiting for."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM clarifications
                WHERE channel_id = ? AND status = 'awaiting'
                ORDER BY created_at DESC
                """,
                (str(channel_id),),
            ).fetchall()
            return [self._clarification_row(r) for r in rows]

    def list_stale_clarifications(self, cutoff: str) -> list[dict]:
        """Asks still unanswered and created at/before `cutoff` (a UTC
        'YYYY-MM-DD HH:MM:SS' string) — i.e. the ones that have timed out and
        whose parked plan should now be proposed as-is."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM clarifications
                WHERE status = 'awaiting' AND created_at <= ?
                ORDER BY created_at ASC
                """,
                (str(cutoff),),
            ).fetchall()
            return [self._clarification_row(r) for r in rows]

    def set_clarification_status(self, source_message_id: int, status: str) -> None:
        with self.conn() as c:
            c.execute(
                """
                UPDATE clarifications
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE source_message_id = ?
                """,
                (status, str(source_message_id)),
            )

    def list_nudgeable_clarifications(
        self, *, created_before: str, last_nudged_before: str
    ) -> list[dict]:
        """Still-awaiting clarifications (across ALL channels) old enough to nudge the
        reporter about: created at/before `created_before`, and either never nudged or
        last nudged at/before `last_nudged_before` (both UTC 'YYYY-MM-DD HH:MM:SS').
        Oldest first. The caller enforces the per-item attempt cap."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM clarifications
                WHERE status = 'awaiting'
                  AND created_at <= ?
                  AND (last_nudged_at IS NULL OR last_nudged_at <= ?)
                ORDER BY created_at ASC
                """,
                (str(created_before), str(last_nudged_before)),
            ).fetchall()
            return [self._clarification_row(r) for r in rows]

    def mark_clarification_nudged(self, source_message_id: int, *, at: str) -> None:
        with self.conn() as c:
            c.execute(
                """
                UPDATE clarifications
                SET nudges_sent = nudges_sent + 1, last_nudged_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_message_id = ?
                """,
                (str(at), str(source_message_id)),
            )

    def mark_clarification_pm_notified(self, source_message_id: int) -> None:
        """Record that we've told the PMs about this unanswered clarification — so we
        only ever escalate it once."""
        with self.conn() as c:
            c.execute(
                """
                UPDATE clarifications
                SET pm_notified = 1, updated_at = CURRENT_TIMESTAMP
                WHERE source_message_id = ?
                """,
                (str(source_message_id),),
            )

    @staticmethod
    def _clarification_row(row: sqlite3.Row) -> dict:
        try:
            plan = json.loads(row["plan_json"])
        except (TypeError, ValueError):
            plan = {}
        return {
            "source_message_id": int(row["source_message_id"]),
            "channel_id": int(row["channel_id"]),
            "ask_message_id": int(row["ask_message_id"]) if row["ask_message_id"] else None,
            "reporter_id": int(row["reporter_id"]) if row["reporter_id"] else None,
            "question": row["question"],
            "plan": plan,
            "status": row["status"],
            "nudges_sent": int(row["nudges_sent"] or 0),
            "last_nudged_at": row["last_nudged_at"],
            "pm_notified": bool(row["pm_notified"]),
            "created_at": row["created_at"],
        }

    # -- open threads (commitments the bot is waiting on) ------------------
    # Read/propose only: these produce a REMINDER to a human in Discord and
    # nothing else. Nothing here is ever an input to ticket creation.

    def record_open_thread(
        self,
        *,
        channel_id: int,
        message_id: int,
        jump_url: str,
        person_id: Optional[int],
        person_name: str,
        what: str,
        promised_at: str,
        due_at: str,
    ) -> bool:
        """Remember that `person_name` promised `what`, due at `due_at` (UTC
        'YYYY-MM-DD HH:MM:SS'). Keyed on the promising message, so re-processing
        that message can't create a second open item. Returns True if a new row
        was inserted, False if we already had this one."""
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO open_threads
                    (channel_id, message_id, jump_url, person_id, person_name,
                     what, promised_at, due_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    str(channel_id),
                    str(message_id),
                    jump_url,
                    str(person_id) if person_id else None,
                    person_name,
                    what,
                    str(promised_at),
                    str(due_at),
                ),
            )
            return cur.rowcount > 0

    def list_due_open_threads(self, *, now: str, reminder_cutoff: str) -> list[dict]:
        """Open items that are ready to be nudged: due at/before `now`, and either
        never nudged or last nudged at/before `reminder_cutoff` (the cooldown).
        Both are UTC 'YYYY-MM-DD HH:MM:SS' strings. Oldest-due first, so the most
        overdue promise gets surfaced before a fresher one."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM open_threads
                WHERE status = 'open'
                  AND due_at <= ?
                  AND (last_reminded_at IS NULL OR last_reminded_at <= ?)
                ORDER BY due_at ASC
                """,
                (str(now), str(reminder_cutoff)),
            ).fetchall()
            return [self._open_thread_row(r) for r in rows]

    def find_open_thread_by_reminder(self, reminder_message_id: int) -> Optional[dict]:
        """The open item a given reminder message is about — so a ✅ on that
        reminder, or a reply to it, can close the right one."""
        with self.conn() as c:
            row = c.execute(
                """
                SELECT * FROM open_threads
                WHERE last_reminder_id = ? AND status = 'open'
                """,
                (str(reminder_message_id),),
            ).fetchone()
            return self._open_thread_row(row) if row else None

    def find_open_thread_by_source(self, message_id: int) -> Optional[dict]:
        """The open item created FROM a given message — so a reply to the original
        promise ("here's that DM date") closes it too."""
        with self.conn() as c:
            row = c.execute(
                """
                SELECT * FROM open_threads
                WHERE message_id = ? AND status = 'open'
                """,
                (str(message_id),),
            ).fetchone()
            return self._open_thread_row(row) if row else None

    def mark_open_thread_reminded(
        self, thread_id: int, *, reminder_message_id: Optional[int], at: str
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                UPDATE open_threads
                SET reminders_sent   = reminders_sent + 1,
                    last_reminder_id = ?,
                    last_reminded_at = ?,
                    updated_at       = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    str(reminder_message_id) if reminder_message_id else None,
                    str(at),
                    thread_id,
                ),
            )

    def set_open_thread_status(self, thread_id: int, status: str) -> None:
        """'closed' (answered / ✅'d) or 'stale' (nudged enough; stop pestering)."""
        with self.conn() as c:
            c.execute(
                """
                UPDATE open_threads
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, thread_id),
            )

    def mark_open_thread_pm_notified(self, thread_id: int) -> None:
        """Record that we've told the PMs this commitment went unanswered after every
        nudge — so the give-up escalation fires exactly once."""
        with self.conn() as c:
            c.execute(
                """
                UPDATE open_threads
                SET pm_notified = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (thread_id,),
            )

    # -- nudges / escalations (the rate limit) -----------------------------
    # The bot posts UNPROMPTED @-mentions on this path. These two methods are the
    # guardrail that keeps that from becoming a nag — every nudge is checked against
    # the log before it is sent, and recorded after.

    def was_nudged_since(
        self, *, target_key: str, issue_id: str, kind: str, since: str
    ) -> bool:
        """Have we already tagged THIS audience about THIS issue for THIS reason since
        `since` (a UTC 'YYYY-MM-DD HH:MM:SS' cooldown boundary)? True → stay quiet.

        Fails CLOSED at the call site: the caller treats an exception as "assume we
        did", because double-tagging a person is worse than missing one nudge."""
        with self.conn() as c:
            row = c.execute(
                """
                SELECT 1 FROM nudges
                WHERE target_key = ? AND issue_id = ? AND kind = ? AND sent_at >= ?
                LIMIT 1
                """,
                (str(target_key), str(issue_id), str(kind), str(since)),
            ).fetchone()
            return row is not None

    def record_nudge(
        self,
        *,
        target_key: str,
        issue_id: str,
        kind: str,
        level: str,
        channel_id: Optional[int],
        message_id: Optional[int],
        sent_at: str,
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO nudges
                    (target_key, issue_id, kind, level, channel_id, message_id, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(target_key),
                    str(issue_id),
                    str(kind),
                    str(level),
                    str(channel_id) if channel_id else None,
                    str(message_id) if message_id else None,
                    str(sent_at),
                ),
            )

    # -- deadline watches (close-the-loop; the one scoped Linear write) ----
    # When she tags an assignee about a missing due date, she records a watch so a later
    # sweep can find the answer and set the date. Recording a watch is NOT itself a write to
    # Linear — the write happens only when a date is resolved AND the feature is armed.

    def record_deadline_watch(
        self,
        *,
        issue_uuid: str,
        issue_identifier: str,
        assignee_key: Optional[str],
        channel_id: Optional[int],
        nudge_message_id: Optional[int],
        asked_at: str,
    ) -> bool:
        """Remember that we asked `assignee_key` for a due date on `issue_identifier`.
        Keyed UNIQUE on the nudge message, so re-processing can't create a second watch.
        Returns True if a new row was inserted, False if we already had it."""
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO deadline_watch
                    (issue_uuid, issue_identifier, assignee_key, channel_id,
                     nudge_message_id, asked_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    str(issue_uuid),
                    str(issue_identifier),
                    str(assignee_key) if assignee_key else None,
                    str(channel_id) if channel_id else None,
                    str(nudge_message_id) if nudge_message_id else None,
                    str(asked_at),
                ),
            )
            return cur.rowcount > 0

    def has_pending_deadline_watch(self, issue_identifier: str) -> bool:
        """True if there's already a pending watch for this issue — so the audit doesn't
        stack a second one while the first is still waiting for an answer."""
        with self.conn() as c:
            row = c.execute(
                """
                SELECT 1 FROM deadline_watch
                WHERE issue_identifier = ? AND status = 'pending' LIMIT 1
                """,
                (str(issue_identifier),),
            ).fetchone()
            return row is not None

    def list_pending_deadline_watches(self) -> list[dict]:
        """Every watch still waiting for an answer, oldest ask first."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM deadline_watch WHERE status = 'pending'
                ORDER BY asked_at ASC
                """
            ).fetchall()
            return [self._deadline_watch_row(r) for r in rows]

    def resolve_deadline_watch(
        self, watch_id: int, *, resolved_date: str, resolved_source: str
    ) -> None:
        """Mark a watch resolved with the date found and where it came from."""
        with self.conn() as c:
            c.execute(
                """
                UPDATE deadline_watch
                SET status = 'resolved', resolved_date = ?, resolved_source = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(resolved_date), str(resolved_source), watch_id),
            )

    def set_deadline_watch_status(self, watch_id: int, status: str) -> None:
        """'expired' when we give up on a watch (nothing came back in time)."""
        with self.conn() as c:
            c.execute(
                """
                UPDATE deadline_watch
                SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (status, watch_id),
            )

    @staticmethod
    def _deadline_watch_row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "issue_uuid": row["issue_uuid"],
            "issue_identifier": row["issue_identifier"],
            "assignee_key": row["assignee_key"],
            "channel_id": int(row["channel_id"]) if row["channel_id"] else None,
            "nudge_message_id": int(row["nudge_message_id"]) if row["nudge_message_id"] else None,
            "asked_at": row["asked_at"],
            "status": row["status"],
            "resolved_date": row["resolved_date"],
            "resolved_source": row["resolved_source"],
        }

    @staticmethod
    def _open_thread_row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "channel_id": int(row["channel_id"]),
            "message_id": int(row["message_id"]),
            "jump_url": row["jump_url"] or "",
            "person_id": int(row["person_id"]) if row["person_id"] else None,
            "person_name": row["person_name"],
            "what": row["what"],
            "promised_at": row["promised_at"],
            "due_at": row["due_at"],
            "status": row["status"],
            "reminders_sent": int(row["reminders_sent"] or 0),
            "last_reminder_id": (
                int(row["last_reminder_id"]) if row["last_reminder_id"] else None
            ),
            "last_reminded_at": row["last_reminded_at"],
            "pm_notified": bool(row["pm_notified"]),
        }
