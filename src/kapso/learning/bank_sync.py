"""Two writers on one shared bank: the remote's lessons are brought into
the bank home before a lesson starts and again before its push, so a push
is a fast-forward and `kapso experience update` never strands a bare
repository on a diverged main. Design: docs/experience/share.mdx."""

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List

from kapso.learning.bank_remote import git_env, bank_origin
from kapso.learning.update_frame import BANK_COMMITTER, DERIVED_INDEX_FILES, compile_derived_index

# Line journals and listings both writers append to: a merge keeps both
# sides' lines rather than calling two appends a conflict.
UNION_MERGED = ("log.md", "sightings.md", "index.md")
REMOTE_MAIN = "refs/remotes/origin/main"


class BankSyncError(RuntimeError):
    """The bank and its remote cannot be merged without a person: both
    sides changed the same card."""


@dataclass(frozen=True)
class BankSync:
    """What a sync did to the bank home's main. `state` is `local` (no
    origin), `even`, `ahead` (local lessons the remote lacks), `pulled`
    (fast-forwarded) or `merged`; `taken` counts the remote's lessons that
    came in and `unpushed` the local commits the remote does not have yet."""

    state: str
    before: str
    after: str
    taken: int = 0
    unpushed: int = 0


def _git(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--git-dir", str(home), *args], capture_output=True, text=True, env=git_env(),
    )


def _rev(home: Path, ref: str) -> str:
    result = _git(home, "rev-parse", ref)
    if result.returncode != 0:
        raise RuntimeError(f"bank {home} has no {ref}: {result.stderr.strip()}")
    return result.stdout.strip()


def _count(home: Path, spec: str) -> int:
    result = _git(home, "rev-list", "--count", spec)
    if result.returncode != 0:
        raise RuntimeError(f"bank {home}: git rev-list {spec} failed: {result.stderr.strip()}")
    return int(result.stdout)


def sync_bank(home: Path) -> BankSync:
    """Bring the remote's lessons into the bank home's main, so a lesson
    starts from the team's bank and its push is a fast-forward. Behind
    fast-forwards; diverged merges in a scratch clone and lands the merge
    on the home: different cards merge, `UNION_MERGED` files keep both
    sides' lines, the derived index is recompiled from the merged cards.
    A card both sides changed raises `BankSyncError` with the home
    untouched. Never pushes: the caller does."""
    before = _rev(home, "main")
    origin = bank_origin(home)
    if origin is None:
        return BankSync("local", before, before)
    fetched = _git(home, "fetch", "--quiet", "origin", f"+refs/heads/main:{REMOTE_MAIN}")
    if fetched.returncode != 0:
        raise RuntimeError(f"bank {home} could not fetch main from {origin}:\n{fetched.stderr.strip()}")
    remote = _rev(home, REMOTE_MAIN)
    taken = _count(home, f"main..{remote}")
    unpushed = _count(home, f"{remote}..main")
    if not taken:
        return BankSync("ahead" if unpushed else "even", before, before, unpushed=unpushed)
    if not unpushed:
        moved = _git(home, "update-ref", "refs/heads/main", remote, before)
        if moved.returncode != 0:
            raise RuntimeError(f"bank {home}: main moved during the sync: {moved.stderr.strip()}")
        return BankSync("pulled", before, remote, taken=taken)
    after = _merge_remote(home, remote, origin)
    return BankSync("merged", before, after, taken=taken, unpushed=_count(home, f"{remote}..main"))


def _merge_remote(home: Path, remote: str, origin: str) -> str:
    with tempfile.TemporaryDirectory(prefix="kapso-bank-sync-") as scratch:
        work = Path(scratch) / "bank"
        cloned = subprocess.run(
            ["git", "clone", "--quiet", str(home), str(work)], capture_output=True, text=True, env=git_env(),
        )
        if cloned.returncode != 0:
            raise RuntimeError(f"bank {home} could not be cloned to merge: {cloned.stderr.strip()}")
        (work / ".git" / "info" / "attributes").write_text(
            "".join(f"{name} merge=union\n" for name in UNION_MERGED)
        )

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", str(work), *args], capture_output=True, text=True, env=git_env())

        fetched = git("fetch", "--quiet", str(home), REMOTE_MAIN)
        if fetched.returncode != 0:
            raise RuntimeError(f"bank {home}: the fetched remote main is not readable: {fetched.stderr.strip()}")
        merged = git(*BANK_COMMITTER, "merge", "--no-ff", "--no-commit", remote)
        conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        unresolved: List[str] = [path for path in conflicted if path not in DERIVED_INDEX_FILES]
        if unresolved:
            raise BankSyncError(
                f"your bank at {home} and {origin} both changed {', '.join(unresolved)}; "
                "the bank was left as it was. Merge them by hand in a clone of the bank, "
                "push the merge to the bank, then learn or update again."
            )
        if merged.returncode != 0 and not conflicted:
            raise RuntimeError(f"bank {home} could not merge {origin}'s main: {merged.stderr.strip()}")
        compile_derived_index(work)
        for step in (
            ("add", "-A"),
            (*BANK_COMMITTER, "commit", "--quiet", "-m", f"merge the lessons from {origin}"),
            ("push", "--quiet", "origin", "main"),
        ):
            result = git(*step)
            if result.returncode != 0:
                raise RuntimeError(f"bank {home}: git {' '.join(step)} failed during the merge: {result.stderr.strip()}")
        return git("rev-parse", "HEAD").stdout.strip()
