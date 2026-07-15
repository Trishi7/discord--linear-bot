"""Short-term, in-memory conversational memory for QUERY MODE.

Query mode is otherwise stateless — each question is parsed and answered in
isolation, so a follow-up like "what about Ravi?" (after "what is Arun working
on?") has nothing to resolve against. This keeps the last few (question, answer)
turns PER Discord channel/thread so such follow-ups carry their intent forward
without the user restating it.

Properties, on purpose:
- IN-MEMORY only — lost on restart. This is working memory, not history.
- Bounded per channel (last N turns) and time-expiring (turns older than the TTL
  are dropped on read/write) so stale context can't bleed into a new question.
- READ-ONLY to the outside world: nothing here is persisted or ever used as an
  input to ticket creation. It only shapes how the read-only query path answers.
"""
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Deque

log = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 5
DEFAULT_TTL_MINUTES = 10
# Stored answers are capped so a long multi-message reply can't bloat the memory
# (and the prompts we later feed it into). We only need enough to resolve a
# follow-up, not the whole answer.
_ANSWER_CHAR_CAP = 800


class ConversationMemory:
    """Per-channel ring of recent (question, answer) turns with TTL expiry.

    Thread-safety: the bot processes messages on a single asyncio event loop, so
    these plain-dict/deque mutations don't need locking.
    """

    def __init__(
        self,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> None:
        self._max_turns = max(1, int(max_turns))
        self._ttl = timedelta(minutes=max(1, int(ttl_minutes)))
        self._by_channel: dict[int, Deque[dict]] = defaultdict(
            lambda: deque(maxlen=self._max_turns)
        )

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _prune(self, channel_id: int) -> None:
        """Drop expired turns (older than TTL) from the front; forget the channel
        entirely once it's empty so the map doesn't grow unbounded."""
        dq = self._by_channel.get(channel_id)
        if not dq:
            return
        cutoff = self._now() - self._ttl
        while dq and dq[0]["at"] < cutoff:
            dq.popleft()
        if not dq:
            self._by_channel.pop(channel_id, None)

    def recent(self, channel_id: int) -> list[dict]:
        """The channel's live (non-expired) turns, oldest first, as
        [{"question", "answer"}]. Empty list when there's no recent context."""
        self._prune(channel_id)
        dq = self._by_channel.get(channel_id)
        if not dq:
            return []
        return [{"question": t["question"], "answer": t["answer"]} for t in dq]

    def record(self, channel_id: int, question: str, answer: str) -> None:
        """Append one exchange for `channel_id`. No-ops on an empty question or
        answer. The deque's maxlen enforces the last-N-turns cap automatically."""
        q = (question or "").strip()
        a = (answer or "").strip()
        if not q or not a:
            return
        if len(a) > _ANSWER_CHAR_CAP:
            a = a[: _ANSWER_CHAR_CAP - 1] + "…"
        self._prune(channel_id)
        self._by_channel[channel_id].append(
            {"question": q, "answer": a, "at": self._now()}
        )
        log.debug(
            "[memory] recorded turn for channel=%s (%d turn(s) held)",
            channel_id,
            len(self._by_channel[channel_id]),
        )

    def clear(self, channel_id: int) -> None:
        """Forget a channel's context (unused today; handy for tests / a reset)."""
        self._by_channel.pop(channel_id, None)
