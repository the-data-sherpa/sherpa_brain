"""Pre-write credential scanner (BLUEPRINT.md §11.4, ADR 0006).

**Reject, never redact.** A redacted secret has already been written to disk, and on
a copy-on-write filesystem it may survive in snapshots indefinitely. Rejection happens
before any bytes reach the store.

Scope is credentials and keys only. Financial identifiers, government IDs, third-party
personal data, and health/legal matters were considered and deliberately excluded —
they *can* be persisted here. That is a defensible narrowing for a single-user,
full-disk-encrypted store, and ADR 0006 records both the narrowing and what would
reverse it. It would not be defensible for a shared deployment.

False positives matter as much as false negatives: a scanner that rejects legitimate
content makes ``brain remember`` unusable, and an unusable write path produces the
emptiness failure mode that kills personal knowledge systems. ULIDs, SHA-256 digests,
long paths, and prose must all pass — asserted in the tests, not assumed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("aws-secret-key", re.compile(r"(?i)aws_?secret_?access_?key\s*[:=]\s*\S{20,}")),
    ("gcp-service-account", re.compile(r'"type"\s*:\s*"service_account"')),
    ("gcp-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("azure-storage-key", re.compile(r"(?i)AccountKey\s*=\s*[A-Za-z0-9+/]{60,}={0,2}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|admin-|svcacct-)?[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("ssh-private-key", re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("uri-with-password", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]{3,}@")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_\-]?key|secret|passwd|password|token|client[_\-]?secret)\b"
            r"\s*[:=]\s*[\"']?([A-Za-z0-9+/_\-]{16,})[\"']?"
        ),
    ),
)

# Shapes that are high-entropy by nature and carry no secret. Checked before the
# entropy heuristic so that ordinary memory content does not trip it.
_BENIGN = (
    re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$"),  # ULID
    re.compile(r"^[0-9a-f]{7,64}$"),  # hex digest / git sha
    re.compile(r"^[0-9a-fA-F-]{36}$"),  # UUID
    re.compile(r"^[A-Za-z0-9_\-]+\.(?:md|py|txt|json|yaml|yml|toml|rs|go|ts|js)$"),
    re.compile(r"^https?://"),
    re.compile(r"^[\w./\-]+/[\w./\-]+$"),  # paths
)

_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_ENTROPY_THRESHOLD = 4.2


@dataclass(frozen=True)
class Finding:
    kind: str
    line: int
    hint: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.kind} ({self.hint})"


class SecretFound(ValueError):
    """A credential was detected. The content is rejected, not stored."""

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        detail = "; ".join(str(f) for f in findings)
        super().__init__(
            f"refusing to store: {detail}. "
            "Credentials are rejected, never redacted — a redacted secret has already "
            "been written to disk. Use a keyring or secret manager instead."
        )


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_benign(token: str) -> bool:
    return any(p.match(token) for p in _BENIGN)


def scan(text: str) -> list[Finding]:
    """Return every credential-like finding. Empty list means the text is clean.

    Note the deliberate asymmetry in reporting: findings name the *pattern class*
    and never echo the matched value, because an error message is itself a place a
    secret can end up.
    """
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _PATTERNS:
            if pattern.search(line):
                findings.append(Finding(kind, lineno, "matched known credential pattern"))
        for token in _TOKEN.findall(line):
            if _is_benign(token):
                continue
            if shannon_entropy(token) >= _ENTROPY_THRESHOLD:
                findings.append(
                    Finding(
                        "high-entropy-token",
                        lineno,
                        f"{len(token)} chars, entropy {shannon_entropy(token):.2f}",
                    )
                )
    return findings


def assert_clean(text: str) -> None:
    """Raise ``SecretFound`` if ``text`` contains anything credential-shaped."""
    if findings := scan(text):
        raise SecretFound(findings)


REDACTION = "[REDACTED: {kind}]"


def redact(text: str) -> tuple[str, list[Finding]]:
    """Mask credentials on the way OUT. Returns the masked text and what was found.

    §11.4 requires scanning before persistence **and before model output**, and the
    right response differs at each end:

    - **On write, reject.** Nothing is on disk yet, so refusing keeps it that way. A
      redacted secret has already been written.
    - **On read, redact.** The bytes are already stored — refusing to serve would
      hide the problem while the secret sits on disk. Masking stops it reaching the
      model *and* leaves the finding visible so it can be purged for real.

    This matters for anything that entered before the scanner existed, and for
    ingested artifacts, which are stored verbatim by design: an imported document is
    evidence, so it is never rewritten on the way in.
    """
    findings = scan(text)
    if not findings:
        return text, []

    out = text
    for _, pattern in _PATTERNS:
        out = pattern.sub(lambda m: REDACTION.format(kind="credential"), out)
    masked_lines = []
    for line in out.splitlines():
        rebuilt = line
        for token in _TOKEN.findall(line):
            if not _is_benign(token) and shannon_entropy(token) >= _ENTROPY_THRESHOLD:
                rebuilt = rebuilt.replace(token, REDACTION.format(kind="high-entropy"))
        masked_lines.append(rebuilt)
    return "\n".join(masked_lines), findings
