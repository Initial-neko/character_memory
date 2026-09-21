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


def test_agent_workflow_artifacts_are_not_part_of_the_repository():
    offenders = [str(path.relative_to(ROOT)) for path in FORBIDDEN_TRACKED_PATHS if path.exists()]
    assert offenders == [], (
        "Agent workflow artifacts must stay in PRs/issues or local untracked notes, "
        f"not in the maintained repository: {offenders}"
    )


def test_generated_agent_doc_names_are_not_committed():
    offenders = []
    for path in ROOT.rglob("*.md"):
        if path.name.lower() in FORBIDDEN_DOC_FILENAMES:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"Generated agent workflow documents are not allowed: {offenders}"
