"""SQLite-backed state.

Three jobs:
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
"""


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)

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
