"""Off-device replication of the tombstone ledger (BLUEPRINT.md §11.5.3).

The tombstone ledger is the one record class that is **never erased**, which makes
git's immutability — disqualifying everywhere else in this design — exactly the right
property here (ADR 0005).

Three things a naive implementation gets wrong, all found in review:

1. **``receive.denyNonFastForwards`` is client-side config that GitHub ignores**, and
   pre-push hooks are locally bypassable (``--no-verify``, a second clone, the API).
   Neither is a control. Protection must be a server-side ruleset, verified at init.
2. **The push is not the acknowledgement.** After pushing, re-read the remote ref and
   confirm it contains what was just written. Only that read is the ack. A push whose
   outcome is uncertain — a timeout after send — is resolved by re-reading, never by
   assuming either way.
3. **Identity comes from the configured endpoint**, never from a field in the ack. A
   self-asserted identity is satisfiable by a duplicate endpoint or a fabricated
   value, which would let one replica count twice toward quorum.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from ..config import Paths
from . import ledger
from .deletion import Replicator

REF = "refs/heads/ledger"


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class GitLedgerReplicator(Replicator):
    """Replicate ``tombstones.jsonl`` to a remote git ref."""

    def __init__(self, paths: Paths, remote: str | None = None) -> None:
        self.paths = paths
        self.repo = paths.ledger_git
        self.remote = remote or self._configured_remote()
        # Identity is derived from the endpoint, deterministically, so two configured
        # copies of the same remote cannot masquerade as two replicas.
        self.identity = (
            f"git:{hashlib.sha256(self.remote.encode()).hexdigest()[:16]}"
            if self.remote
            else "unconfigured"
        )

    def _configured_remote(self) -> str:
        if not self.repo.exists():
            return ""
        try:
            return _git(self.repo, "remote", "get-url", "origin", check=False)
        except (GitError, OSError, subprocess.TimeoutExpired):
            return ""

    # -- setup -----------------------------------------------------------------

    def init_repo(self, remote: str) -> None:
        """Create the bare ledger repo and point it at ``remote``."""
        self.repo.mkdir(parents=True, exist_ok=True)
        if not (self.repo / "HEAD").exists():
            _git(self.repo, "init", "--bare", "--quiet", str(self.repo))
        _git(self.repo, "remote", "remove", "origin", check=False)
        _git(self.repo, "remote", "add", "origin", remote)
        self.remote = remote
        self.identity = f"git:{hashlib.sha256(remote.encode()).hexdigest()[:16]}"

    def verify_protection(self) -> bool:
        """Confirm the remote ref is protected against force-push and deletion.

        Returns False when protection cannot be *verified*. A caller must fail loudly
        rather than proceed with a ledger that merely looks protected — an unprotected
        append-only log provides no monotonicity at all.
        """
        if not self.remote:
            return False
        slug = _slug(self.remote)
        if not slug:
            return False
        proc = subprocess.run(
            ["gh", "api", f"repos/{slug}/rulesets", "--jq", ".[].name"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0 and "brain-ledger-append-only" in proc.stdout

    def ensure_protection(self) -> bool:
        """Create the append-only ruleset. Returns whether it is verifiably in place."""
        if self.verify_protection():
            return True
        slug = _slug(self.remote)
        if not slug:
            return False
        payload = (
            '{"name":"brain-ledger-append-only","target":"branch","enforcement":"active",'
            '"conditions":{"ref_name":{"include":["~ALL"],"exclude":[]}},'
            '"rules":[{"type":"non_fast_forward"},{"type":"deletion"}]}'
        )
        subprocess.run(
            ["gh", "api", "--method", "POST", f"repos/{slug}/rulesets", "--input", "-"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return self.verify_protection()

    # -- replication -----------------------------------------------------------

    def push_and_ack(self, paths: Paths, subject_id: str) -> bool:
        """Push the ledger, then **re-read the remote ref** and record what was verified."""
        if not self.remote or not self.repo.exists():
            return False
        seq, chain_head = ledger.head(paths.tombstones)
        if seq == 0:
            return False

        try:
            blob = self._write_blob(paths.tombstones)
            tree = self._write_tree(blob)
            commit = self._commit(tree, f"ledger seq {seq}")
            _git(self.repo, "update-ref", REF, commit)
            _git(self.repo, "push", "origin", f"{REF}:{REF}", timeout=120)
        except (GitError, OSError, subprocess.SubprocessError):
            # An uncertain outcome is resolved by reading the remote below, never by
            # assuming either way. Replication failing must never crash the caller:
            # the deletion is already durable and already suppressed, and only the
            # receipt is at stake.
            pass

        remote_sha = self._read_remote_ref()
        if not remote_sha:
            return False
        if not self._remote_contains(remote_sha, paths.tombstones):
            return False

        ledger.append(
            paths.acks,
            ledger.ack_payload(
                subject_id,
                seq,
                chain_head,
                replica_identity=self.identity,
                remote_sha=remote_sha,
                ref=REF,
                protection_verified=self.verify_protection(),
            ),
        )
        return True

    def _write_blob(self, path: Path) -> str:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), "hash-object", "-w", "--stdin"],
            input=path.read_bytes(),
            capture_output=True,
            timeout=60,
            check=True,
        )
        return proc.stdout.decode().strip()

    def _write_tree(self, blob: str) -> str:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), "mktree"],
            input=f"100644 blob {blob}\ttombstones.jsonl\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return proc.stdout.strip()

    def _commit(self, tree: str, message: str) -> str:
        # `rev-parse <ref>` prints the REF NAME when the ref does not exist, so a
        # truthiness check passes and `-p refs/heads/ledger` is then handed to
        # commit-tree, which fails. `--verify <ref>^{commit}` returns nothing and a
        # non-zero status instead, which is what "no parent yet" should look like.
        parent = _git(
            self.repo, "rev-parse", "--verify", "--quiet", f"{REF}^{{commit}}", check=False
        )
        args = ["commit-tree", tree, "-m", message]
        if _is_sha(parent):
            args += ["-p", parent]
        env_repo = self.repo
        proc = subprocess.run(
            ["git", "--git-dir", str(env_repo), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env=_commit_env(),
        )
        return proc.stdout.strip()

    def _read_remote_ref(self) -> str:
        """The acknowledgement: what the remote actually holds, read back."""
        out = _git(self.repo, "ls-remote", "origin", REF, check=False, timeout=60)
        return out.split()[0] if out else ""

    def _remote_contains(self, remote_sha: str, local: Path) -> bool:
        """Confirm the remote commit carries the exact ledger bytes we appended."""
        try:
            content = _git(self.repo, "show", f"{remote_sha}:tombstones.jsonl", check=False)
        except (GitError, OSError, subprocess.TimeoutExpired):
            return False
        return content.strip() == local.read_text().strip()


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _slug(remote: str) -> str:
    """owner/repo from an https or ssh GitHub URL."""
    r = remote.removesuffix(".git")
    if "github.com" not in r:
        return ""
    tail = r.split("github.com", 1)[1].lstrip(":/")
    return tail if tail.count("/") == 1 else ""


def _commit_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "brain")
    env.setdefault("GIT_AUTHOR_EMAIL", "brain@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "brain")
    env.setdefault("GIT_COMMITTER_EMAIL", "brain@localhost")
    return env
