"""The memory model.

Six required fields, not fourteen (BLUEPRINT.md §7.1). The two that carry the most
weight are the two an earlier design was missing entirely:

- ``volatility``       — determines decay, re-confirmation cadence, and expiry. A single
  global decay curve is wrong for most facts about a person, and the failure it causes
  is concrete: confidently reporting a stack you migrated off six weeks ago.
- ``provenance_class`` — replaces LLM confidence floats, which are poorly calibrated and
  cannot be audited by anyone. A categorical class with a *written* precedence ordering
  can be (ADR 0001).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum


class MemoryType(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PREFERENCE = "preference"
    PROCEDURAL = "procedural"
    TASK = "task"


class Volatility(StrEnum):
    """How fast a claim goes stale. Drives decay, re-confirmation, and expiry."""

    IMMUTABLE = "immutable"  # "I was born in March" — never decays
    SLOW = "slow"  # "I prefer concise summaries" — revisit on contradiction
    VOLATILE = "volatile"  # "I'm using Postgres here" — re-confirm aggressively
    EPHEMERAL = "ephemeral"  # "I'm blocked on the auth bug" — expire by default


class ProvenanceClass(StrEnum):
    """Where a claim came from. Ordering is defined in ADR 0001, not inferred here."""

    DIRECT_USER_STATEMENT = "direct-user-statement"
    AUTHORITATIVE_DOCUMENT = "authoritative-document"
    VERIFIED_ENVIRONMENT_OUTCOME = "verified-environment-outcome"
    THIRD_PARTY_DOCUMENT = "third-party-document"
    INFERRED_FROM_BEHAVIOR = "inferred-from-behavior"
    AGENT_SPECULATION = "agent-speculation"


#: Trust tier is a HARD GATE evaluated before volatility or recency (ADR 0001).
#: An earlier rule ranked by precedence then recency, which let a fresh claim tagged
#: ``volatile`` override trusted evidence — and since volatility is assigned by an
#: extractor reading attacker-controlled text, that was an injection path.
TRUSTED: frozenset[ProvenanceClass] = frozenset(
    {
        ProvenanceClass.DIRECT_USER_STATEMENT,
        ProvenanceClass.AUTHORITATIVE_DOCUMENT,
        ProvenanceClass.VERIFIED_ENVIRONMENT_OUTCOME,
    }
)

#: Highest first. Written down because a categorical class is only auditable if its
#: ordering is explicit — otherwise it is just an unauditable float by another name.
PRECEDENCE: tuple[ProvenanceClass, ...] = (
    ProvenanceClass.DIRECT_USER_STATEMENT,
    ProvenanceClass.AUTHORITATIVE_DOCUMENT,
    ProvenanceClass.VERIFIED_ENVIRONMENT_OUTCOME,
    ProvenanceClass.THIRD_PARTY_DOCUMENT,
    ProvenanceClass.INFERRED_FROM_BEHAVIOR,
    ProvenanceClass.AGENT_SPECULATION,
)


def precedence_rank(pc: ProvenanceClass) -> int:
    return PRECEDENCE.index(pc)


def is_trusted(pc: ProvenanceClass) -> bool:
    return pc in TRUSTED


class Status(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    TOMBSTONED = "tombstoned"


class Capture(StrEnum):
    """How a revision came to be recorded.

    ``RECONCILED`` matters: it means the transition was never witnessed by the write
    protocol, so its transaction time is an *interval*, not a point. Recording a
    precise timestamp there would be a lie the audit trail then rests on.
    """

    MEDIATED = "mediated"
    RECONCILED = "reconciled"
    IMPORTED = "imported"


class Disposition(StrEnum):
    """What may be served for a memory, from ``serving_disposition`` (BLUEPRINT §6.6).

    Note there is no ``RECOVERING``. An earlier design had one and it deadlocked:
    with ``CONTESTED`` checked first, a crash mid-resolution left an op record that
    was never reached and resolution stalled forever. Recovery is a separate pass
    that runs unconditionally *before* this one — a different question, not a
    different answer to the same question.
    """

    QUARANTINED = "quarantined"
    CONTESTED = "contested"
    INTERRUPTED = "interrupted"
    UNWITNESSED = "unwitnessed"
    SETTLED = "settled"


#: Default expiry per volatility class, in days. ``None`` means no automatic expiry.
DEFAULT_EXPIRY_DAYS: dict[Volatility, int | None] = {
    Volatility.IMMUTABLE: None,
    Volatility.SLOW: None,
    Volatility.VOLATILE: 180,
    Volatility.EPHEMERAL: 14,
}

#: Proposals that are never confirmed and never cited lapse. This inverts the
#: accretion dynamic that degrades memory systems over months (BLUEPRINT §8.1).
PROPOSAL_DECAY_DAYS = 30


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """ISO-8601 at millisecond precision.

    Seconds is too coarse for an audit trail: the system can record several
    revisions inside one second, and a transaction-time *interval* whose bounds
    both round to the same value stops being an interval.
    """
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Evidence:
    """A pointer into preserved source material, with an optional span.

    The span is what makes a claim checkable rather than merely attributed.
    """

    ref: str  # event:<id> | artifact:<digest> | chunk:<id>
    span_start: int | None = None
    span_end: int | None = None

    def __str__(self) -> str:
        if self.span_start is None:
            return self.ref
        return f"{self.ref}#L{self.span_start}-{self.span_end}"

    @classmethod
    def parse(cls, raw: str) -> Evidence:
        """Parse ``ref#L<start>-<end>``.

        Tolerant of a repeated ``L`` on the end bound (``#L4-L8``), because that is
        how a human writes it and how most code hosts render it. A pointer that
        fails to parse degrades to a bare ref rather than raising — losing the span
        is recoverable, losing the evidence link is not.
        """
        if "#L" not in raw:
            return cls(raw)
        ref, _, span = raw.partition("#L")
        start, _, end = span.partition("-")
        end = end.lstrip("Ll")
        try:
            return cls(ref, int(start), int(end) if end else int(start))
        except ValueError:
            return cls(raw)


@dataclass
class Memory:
    """One memory: required fields, then everything optional."""

    id: str
    type: MemoryType
    provenance_class: ProvenanceClass
    volatility: Volatility
    valid_from: date
    evidence: list[Evidence]
    body: str = ""

    # Optional / system-assigned.
    status: Status = Status.CONFIRMED
    workspace: str = "default"
    owner: str | None = None
    valid_to: date | None = None
    supersedes: str | None = None
    tags: list[str] = field(default_factory=list)
    review_by: date | None = None
    sensitivity: str | None = None
    recorded_at: datetime = field(default_factory=utcnow)

    @property
    def trusted(self) -> bool:
        return is_trusted(self.provenance_class)

    @property
    def expiry_days(self) -> int | None:
        if self.status is Status.PROPOSED:
            return PROPOSAL_DECAY_DAYS
        return DEFAULT_EXPIRY_DAYS[self.volatility]


@dataclass(frozen=True)
class RevisionMeta:
    """Metadata on one entry in a memory's append-only revision log.

    ``recorded_from``/``recorded_to`` bound transaction time. For a mediated write
    they are equal. For a reconciled one they bracket an unwitnessed transition,
    which is the honest representation and the only one the audit trail can stand on.
    """

    memory_id: str
    revision_no: int
    content_hash: str
    predecessor_hash: str | None
    capture: Capture
    recorded_from: datetime
    recorded_to: datetime
    opid: str | None = None
    actor: str | None = None
    session: str | None = None
    reason: str | None = None

    @property
    def witnessed(self) -> bool:
        return self.capture is Capture.MEDIATED
