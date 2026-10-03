"""In-process fast decline for seats already known to be taken.

During an on-sale burst ~95% of reserve requests are for seats that are
already sold. Answering those from memory keeps them off the database and the
connection pool, which is the scarce resource on a small instance.

Safety argument (why this can't sell a seat twice or give a wrong answer):

* The cache can only DECLINE. Every sale still goes through the guarded
  UPDATE in the database transaction; a missing or expired entry just means
  the request takes the normal database path.
* Each entry records the seat's owner. A request is only declined from memory
  for seats owned by someone ELSE; if the requester owns any requested seat it
  may be a retry of its own booking, so the database decides (200 replay or
  `idempotency_key_reused`). This holds no matter which request cached the
  seat or whether the owner's key was remembered yet.
* Keys this process has seen succeed also skip the cache, so reusing one for a
  seat sold to someone else still gets `idempotency_key_reused`.
* Release fencing: a cancel removes its seats right after its commit and
  bumps a per-seat release sequence. A request that read "taken" from the
  database before that release may only cache the seat if no release happened
  since its read began (`epoch()` taken before the read, passed to
  `mark_taken(since=...)`). So a late writer can't re-cache a freed seat and a
  released seat is rebookable immediately.
* Entries expire after `ttl_seconds`. With a single instance they are only
  ever removed by cancels in this process; the TTL bounds staleness if the
  service is ever scaled to several instances (a cancel on another instance
  would otherwise be invisible here).

Known difference from the database path: a key used before a process restart
is not in memory, so reusing it for a seat sold (to someone else) after the
restart is declined as `seat_taken` instead of `idempotency_key_reused`
(still a 409, never a sale).
"""

import time
import uuid
from collections import OrderedDict

SeatKey = tuple[uuid.UUID, str]


class SoldSeatCache:
    def __init__(self, ttl_seconds: float, max_keys: int = 200_000, max_releases: int = 200_000) -> None:
        self._ttl = ttl_seconds
        self._max_keys = max_keys
        self._max_releases = max_releases
        # (show_id, label) -> (monotonic expiry time, owner user_id)
        self._taken: dict[SeatKey, tuple[float, str]] = {}
        # (user_id, idempotency_key) seen to exist in the database; bounded LRU.
        self._keys: OrderedDict[tuple[str, str], None] = OrderedDict()
        # Release fencing: global sequence, and the sequence of each seat's last release.
        self._seq = 0
        self._released: OrderedDict[SeatKey, int] = OrderedDict()

    # --- idempotency keys -----------------------------------------------------

    def knows_key(self, user_id: str, key: str) -> bool:
        return (user_id, key) in self._keys

    def remember_key(self, user_id: str, key: str) -> None:
        self._keys[(user_id, key)] = None
        self._keys.move_to_end((user_id, key))
        if len(self._keys) > self._max_keys:
            self._keys.popitem(last=False)

    # --- seats ----------------------------------------------------------------

    def epoch(self) -> int:
        """Take before reading seat state from the database; pass to mark_taken."""
        return self._seq

    def taken_by_others(self, show_id: uuid.UUID, labels: list[str], user_id: str) -> list[str]:
        """Labels known to be taken by someone other than `user_id` (sorted).

        Returns [] (= "ask the database") if `user_id` owns any of `labels`:
        that request may be a retry of its own booking.
        """
        now = time.monotonic()
        hits = []
        for label in labels:
            entry = self._taken.get((show_id, label))
            if entry is None:
                continue
            expires, owner = entry
            if expires <= now:
                del self._taken[(show_id, label)]
                continue
            if owner == user_id:
                return []
            hits.append(label)
        return sorted(hits)

    def mark_taken(self, show_id: uuid.UUID, owners: dict[str, str], since: int | None = None) -> None:
        """Cache `label -> owner user_id`. With `since` (an earlier `epoch()`),
        seats released after that point are skipped: the caller's view of them
        is older than the release."""
        expires = time.monotonic() + self._ttl
        for label, owner in owners.items():
            seat = (show_id, label)
            if since is not None and self._released.get(seat, -1) > since:
                continue
            self._taken[seat] = (expires, owner)

    def release(self, show_id: uuid.UUID, labels: list[str]) -> None:
        for label in labels:
            seat = (show_id, label)
            self._taken.pop(seat, None)
            self._seq += 1
            self._released[seat] = self._seq
            self._released.move_to_end(seat)
        while len(self._released) > self._max_releases:
            self._released.popitem(last=False)

    def __len__(self) -> int:
        return len(self._taken)
