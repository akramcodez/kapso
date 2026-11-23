# bank_sync: two writers on one shared bank. Every remote is a local bare
# repository: real git, no network.

import subprocess
from pathlib import Path

import pytest
import yaml

from kapso.learning.bank_remote import connect_bank
from kapso.learning.bank_sync import BankSyncError, sync_bank
from kapso.learning.update_frame import init_bank
from tests.test_bank_retriever import card_text


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def head(git_dir: Path) -> str:
    return git("--git-dir", str(git_dir), "rev-parse", "main")


def team(tmp_path):
    """Alice shares her bank; Bob's bank home is a clone of the share."""
    share = tmp_path / "share.git"
    git("init", "--bare", "-q", "-b", "main", str(share))
    alice = tmp_path / "alice" / "bank.git"
    init_bank(str(alice))
    connect_bank(alice, str(share))
    bob = tmp_path / "bob" / "bank.git"
    git("clone", "-q", "--bare", str(share), str(bob))
    git("--git-dir", str(bob), "remote", "set-url", "origin", str(share))
    return share, alice, bob


def lesson(home: Path, card: str, body: str = "Lesson body.\n") -> None:
    """What the update crew does to a bank home: clone, write a card, list it,
    journal it, push to the home."""
    work = home.parent / f"work-{card}-{len(list(home.parent.iterdir()))}"
    git("clone", "-q", str(home), str(work))
    (work / "insights" / f"{card}.md").write_text(card_text(card, body=body))
    with open(work / "insights" / "index.md", "a") as handle:
        handle.write(f"- [{card}]({card}.md)\n")
    with open(work / "log.md", "a") as handle:
        handle.write(f"- lesson {card}\n")
    git("add", "-A", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", card, cwd=work)
    git("push", "-q", "origin", "main", cwd=work)


def show(home: Path, path: str) -> str:
    return git("--git-dir", str(home), "show", f"main:{path}")


def test_behind_fast_forwards(tmp_path):
    # Regression: a bank that only lacks the team's lessons takes them as is.
    share, alice, bob = team(tmp_path)
    lesson(alice, "alice-card")
    git("--git-dir", str(alice), "push", "-q", "origin", "main")
    synced = sync_bank(bob)
    assert (synced.state, synced.taken, synced.unpushed) == ("pulled", 1, 0)
    assert head(bob) == head(share)


def test_ahead_is_left_alone(tmp_path):
    # Regression: unpushed local lessons are reported, never dropped.
    share, alice, _ = team(tmp_path)
    lesson(alice, "alice-card")
    before = head(alice)
    synced = sync_bank(alice)
    assert (synced.state, synced.unpushed) == ("ahead", 1)
    assert head(alice) == before


def test_diverged_lessons_merge_and_push(tmp_path):
    # Regression (#245): the second writer's push was rejected and
    # `experience update` could not recover on a bare home. Lessons on
    # different cards merge, both sides' journal and index lines survive,
    # the derived index covers both cards, and the push fast-forwards.
    share, alice, bob = team(tmp_path)
    lesson(alice, "alice-card", body="See [bob-card](insights/bob-card.md).\n")
    git("--git-dir", str(alice), "push", "-q", "origin", "main")
    lesson(bob, "bob-card")
    bob_lesson = head(bob)

    synced = sync_bank(bob)

    assert (synced.state, synced.taken, synced.unpushed) == ("merged", 1, 2)
    assert git("--git-dir", str(bob), "merge-base", "--is-ancestor", bob_lesson, "main") == ""
    assert {"alice-card.md", "bob-card.md"} <= set(git("--git-dir", str(bob), "ls-tree", "--name-only", "main:insights").split())
    assert "- lesson alice-card" in show(bob, "log.md") and "- lesson bob-card" in show(bob, "log.md")
    index = show(bob, "insights/index.md")
    assert "[alice-card]" in index and "[bob-card]" in index
    assert {"from": "alice-card", "to": "insights/bob-card.md", "type": "reference"} in yaml.safe_load(show(bob, "index/edges.yaml"))
    git("--git-dir", str(bob), "push", "-q", "origin", "main")
    assert head(share) == head(bob)


def test_same_card_on_both_sides_refuses_and_leaves_the_bank(tmp_path):
    # Regression: Kapso never picks a side of a card both writers changed.
    share, alice, bob = team(tmp_path)
    lesson(alice, "shared-card", body="Alice's reading.\n")
    git("--git-dir", str(alice), "push", "-q", "origin", "main")
    lesson(bob, "shared-card", body="Bob's reading.\n")
    before = head(bob)
    with pytest.raises(BankSyncError, match="insights/shared-card.md"):
        sync_bank(bob)
    assert head(bob) == before
