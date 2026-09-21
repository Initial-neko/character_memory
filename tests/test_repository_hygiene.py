"""Agent workflow artifacts are allowed locally; they must not be committed.

Both checks ask git what it tracks rather than what is on disk. The distinction
is the whole point: ``.superpowers/sdd/`` carries its own ``.gitignore`` holding
``*``, so a contributor's progress ledger is invisible to git by design -- and
"local untracked notes" are explicitly allowed. An existence check would fail
for exactly that contributor while passing in CI on a fresh clone, which means
it would measure the working copy instead of the repository both tests are
named for.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_TRACKED_PATHS = (
    ROOT / "docs" / "superpowers",
    ROOT / ".superpowers",
)

FORBIDDEN_DOC_FILENAMES = {
    "task-brief.md",
    "progress-ledger.md",
    "review-scratchpad.md",
}


def _tracked(relative: str) -> list[str]:
    """The tracked paths matching ``relative`` -- a file, or a directory's tree."""

    result = subprocess.run(
        ["git", "ls-files", "-z", "--", relative],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [name for name in result.stdout.split("\0") if name]


def test_agent_workflow_artifacts_are_not_part_of_the_repository():
    offenders = [
        str(path.relative_to(ROOT))
        for path in FORBIDDEN_TRACKED_PATHS
        if _tracked(str(path.relative_to(ROOT)))
    ]
    assert offenders == [], (
        "Agent workflow artifacts must stay in PRs/issues or local untracked notes, "
        f"not in the maintained repository: {offenders}"
    )


def test_generated_agent_doc_names_are_not_committed():
    offenders = sorted(
        name
        for name in _tracked("*.md")
        if Path(name).name.lower() in FORBIDDEN_DOC_FILENAMES
    )
    assert offenders == [], f"Generated agent workflow documents are not allowed: {offenders}"
