"""In-process fast decline for seats already known to be taken.

During an on-sale burst ~95% of reserve requests are for seats that are
already sold. Answering those from memory keeps them off the database and the
connection pool, which is the scarce resource on a small instance.

Safety argument (why this can't sell a seat twice or give a wrong answer):

* The cache can only DECLINE. Every sale still goes through the guarded
  UPDATE in the database transaction; a missing or expired entry just means
  the request takes the normal database path.
* Idempotency comes first: if this process has seen `(user_id, key)` succeed,
  the request skips the cache and goes to the database, so a retry still gets
  its 200 replay and a reused key still gets `idempotency_key_reused`.
  `remember_key` runs before `mark_taken` with no `await` in between, so no
  other request can observe "seat cached, key not yet known".
* A cancel removes its seats right after its commit, so a released seat is
  rebookable immediately.
* Entries expire after `ttl_seconds`. With a single instance they are only
  ever removed by cancels in this process; the TTL bounds staleness if the
  service is ever scaled to several instances (a cancel on another instance
  would otherwise be invisible here).

Known difference from the database path: a key used before a process restart
is not in memory, so reusing it for a seat sold after the restart is declined
as `seat_taken` instead of `idempotency_key_reused` (still a 409, never a sale).
"""

import time
import uuid
from collections import OrderedDict


class SoldSeatCache:
    def __init__(self, ttl_seconds: float, max_keys: int = 200_000) -> None:
        self._ttl = ttl_seconds
        self._max_keys = max_keys
        # (show_id, label) -> monotonic expiry time
        self._taken: dict[tuple[uuid.UUID, str], float] = {}
        # (user_id, idempotency_key) seen to exist in the database; bounded LRU.
        self._keys: OrderedDict[tuple[str, str], None] = OrderedDict()

    # --- idempotency keys -----------------------------------------------------

    def knows_key(self, user_id: str, key: str) -> bool:
        return (user_id, key) in self._keys

    def remember_key(self, user_id: str, key: str) -> None:
        self._keys[(user_id, key)] = None
        self._keys.move_to_end((user_id, key))
        if len(self._keys) > self._max_keys:
            self._keys.popitem(last=False)

    # --- seats ----------------------------------------------------------------

    def taken(self, show_id: uuid.UUID, labels: list[str]) -> list[str]:
        """Labels known to be taken (sorted, like the database precheck)."""
        now = time.monotonic()
        hits = []
        for label in labels:
            expires = self._taken.get((show_id, label))
            if expires is None:
                continue
            if expires <= now:
                del self._taken[(show_id, label)]
                continue
            hits.append(label)
        return sorted(hits)

    def mark_taken(self, show_id: uuid.UUID, labels: list[str]) -> None:
        expires = time.monotonic() + self._ttl
        for label in labels:
            self._taken[(show_id, label)] = expires

    def release(self, show_id: uuid.UUID, labels: list[str]) -> None:
        for label in labels:
            self._taken.pop((show_id, label), None)

    def __len__(self) -> int:
        return len(self._taken)
