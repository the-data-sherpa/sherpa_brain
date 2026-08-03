"""Statistics for the evaluation harness (BLUEPRINT.md §10.1, ADR 0002).

An earlier version of the migration trigger read "negative slope for 3 consecutive
weeks" on a 50-item golden set. That does not survive contact with binomial noise —
week-to-week variation swamps the effect being measured. Being rigorous about source
evidence while sloppy about one's own instrument is the worse of the two failures, so
the floor and the interval are enforced here in code rather than left to discipline.

Two rules this module exists to hold:

- **Refuse to compute a slope below the item floor.** Return "insufficient data",
  never a number that looks precise.
- **Report an interval, never a bare point.** A score without one invites reading
  noise as signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

#: Below this the slope is not computed at all (ADR 0002).
MIN_GOLDEN_SET = 150

#: A decline must exceed this to count, pre-registered rather than chosen afterwards.
PRE_REGISTERED_MARGIN_PP = 10.0

#: ...and be sustained across at least this many measurements.
MIN_SUSTAINED_MEASUREMENTS = 3


@dataclass(frozen=True)
class Score:
    correct: int
    total: int
    lower: float
    upper: float

    @property
    def point(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def __str__(self) -> str:
        return f"{self.point:.1%} [{self.lower:.1%}-{self.upper:.1%}] (n={self.total})"


def wilson(correct: int, total: int, z: float = 1.96) -> Score:
    """Wilson score interval.

    Chosen over the normal approximation because it behaves at the extremes — and a
    golden set that is going well lives at the extreme, which is exactly where the
    naive interval stops being trustworthy.
    """
    if total == 0:
        return Score(0, 0, 0.0, 0.0)
    p = correct / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return Score(correct, total, max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class SlopeVerdict:
    computable: bool
    reason: str
    triggered: bool = False
    decline_pp: float | None = None

    def __str__(self) -> str:
        if not self.computable:
            return f"slope not computed — {self.reason}"
        return (
            f"decline {self.decline_pp:.1f}pp — "
            f"{'TRIGGER MET' if self.triggered else 'below threshold'}: {self.reason}"
        )


def slope_verdict(
    series: list[Score],
    *,
    min_items: int = MIN_GOLDEN_SET,
    margin_pp: float = PRE_REGISTERED_MARGIN_PP,
    min_measurements: int = MIN_SUSTAINED_MEASUREMENTS,
) -> SlopeVerdict:
    """Decide whether the tenure trigger has fired.

    Returns a *decision prompt*, never an instruction: the trigger opens a review and
    a human decides. That posture is deliberate given how weak the underlying
    evidence for the tenure effect actually is (§4.2).
    """
    if not series:
        return SlopeVerdict(False, "no measurements yet")
    if any(s.total < min_items for s in series):
        smallest = min(s.total for s in series)
        return SlopeVerdict(
            False,
            f"golden set has {smallest} items; the floor is {min_items}. "
            f"Below it, week-to-week noise swamps the effect being measured.",
        )
    if len(series) < min_measurements:
        return SlopeVerdict(
            False,
            f"{len(series)} measurement(s); {min_measurements} are required before a "
            f"decline counts as sustained",
        )

    recent = series[-min_measurements:]
    decline_pp = (series[0].point - recent[-1].point) * 100
    monotonic = all(b.point <= a.point for a, b in pairwise(recent))
    triggered = decline_pp >= margin_pp and monotonic

    return SlopeVerdict(
        True,
        (
            f"declined {decline_pp:.1f}pp against a pre-registered margin of {margin_pp}pp, "
            f"sustained across {min_measurements} measurements. This is a DECISION PROMPT: "
            f"confirm with the failure taxonomy that retrieval dominates before building."
            if triggered
            else f"declined {decline_pp:.1f}pp; the margin is {margin_pp}pp"
        ),
        triggered=triggered,
        decline_pp=decline_pp,
    )
