"""SQLite-backed state.

Three jobs:
1. Dedup — if the same Discord message ID is seen twice (e.g. bot restart and
   replay), don't classify it again.
2. Pending approvals — map an approval embed's message ID back to the source
   message + classification, so the reaction handler can act on a ✅/❌.
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
    status              TEXT NOT NULL,   -- pending | approved | rejected
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

    def get_by_approval(self, approval_message_id: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute(
                """
                SELECT message_id, channel_id, classification_json, status
                FROM processed WHERE approval_message_id = ?
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
