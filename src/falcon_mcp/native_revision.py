"""State written by instrument scripts while the owned host processes audio, kept apart from real edits.

Many Falcon instruments ship a Lua script that writes envelopes, pan and similar parameters on its first
process blocks after a load. The serialized state then changes although nobody edited it, and a revision
pinned before that first render no longer matches. Settling processes silence after a load until the state
stops changing; revision aliases let a guard accept the revision a render started from when the only change
since is what that render (the plugin's own scripts) wrote.
"""

from __future__ import annotations

SETTLE_SECONDS = 0.5
# Sample loading can delay a script's first write, so one quiet pass is not proof of a settled state.
SETTLE_STABLE_PASSES = 2
SETTLE_MAX_PASSES = 8
MAX_ALIASES = 32


def settle(plugin, fingerprint, sample_rate: int = 48000, seconds: float = SETTLE_SECONDS,
           stable_passes: int = SETTLE_STABLE_PASSES, max_passes: int = SETTLE_MAX_PASSES) -> dict:
    """Process silence until the state fingerprint is unchanged for stable_passes passes in a row."""
    start = last = fingerprint()
    passes = stable = 0
    if seconds <= 0:
        return {"passes": 0, "seconds": 0.0, "changed": False, "stable": None, "revision": start}
    while passes < max_passes and stable < stable_passes:
        plugin([], duration=seconds, sample_rate=sample_rate, num_channels=2, buffer_size=512)
        passes += 1
        now = fingerprint()
        stable = stable + 1 if now == last else 0
        last = now
    return {"passes": passes, "seconds": round(passes * seconds, 3), "changed": last != start,
            "stable": stable >= stable_passes, "revision": last}


class RevisionAliases:
    """Earlier revisions that differ from the current state only by what renders wrote into it.

    Any other change (load, edit, connection) clears the aliases, so a guard still rejects real edits.
    """

    def __init__(self):
        self.current: str | None = None
        self.earlier: list[str] = []

    def of(self, revision: str) -> list[str]:
        return list(self.earlier) if revision == self.current else []

    def matches(self, expected: str, revision: str) -> bool:
        return expected == revision or expected in self.of(revision)

    def rendered(self, before: str, after: str) -> bool:
        """Record a render that moved the state from before to after; True when it changed the state."""
        if before == after:
            return False
        chain = [before, *self.of(before)]
        # Keep the oldest revision (the one an agent inspected) and the most recent ones.
        self.current, self.earlier = after, chain if len(chain) <= MAX_ALIASES else chain[:MAX_ALIASES - 1] + chain[-1:]
        return True

    def clear(self):
        self.current, self.earlier = None, []
