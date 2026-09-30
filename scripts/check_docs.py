"""Check maintained-document links/index and explicit PR documentation impact.

No network, model calls, or automatic prose generation. Run from any directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENTRY_DOCS = ("README.md", "AGENTS.md", "CONTRIBUTING.md", "docs/README.md")
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[\"'][^\n]*?[\"'])?\s*\)")
REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*<?([^\s>]+)>?", re.MULTILINE)


def prose(text: str) -> str:
    # Examples in fenced blocks are not active document links.
    result = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            result.append("")
        else:
            result.append("" if fence else line)
    return "\n".join(result)


def targets(text: str):
    text = prose(text)
    for match in LINK.finditer(text):
        yield match.group(1) or match.group(2)
    yield from REFERENCE.findall(text)


def maintained_files(root: Path) -> list[Path]:
    return [root / name for name in ENTRY_DOCS if (root / name).is_file()] + sorted(
        (root / "docs/current").glob("*.md")
    )


def check_structure(root: Path) -> list[str]:
    errors = []
    for path in maintained_files(root):
        for target in targets(path.read_text(encoding="utf-8")):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            destination = (path.parent / unquote(url.path)).resolve()
            if not destination.exists():
                errors.append(f"{path.relative_to(root)}: missing local target {target}")
    index = root / "docs/README.md"
    indexed = set()
    if index.exists():
        indexed = {(index.parent / unquote(urlsplit(target).path)).resolve()
                   for target in targets(index.read_text(encoding="utf-8"))
                   if not urlsplit(target).scheme}
    for path in sorted((root / "docs/current").glob("*.md")):
        if path.resolve() not in indexed:
            errors.append(f"{path.relative_to(root)}: missing from docs/README.md")
    return errors


def check_impact(root: Path, changed: list[str], body: str) -> list[str]:
    # Tests/refactors can legitimately leave contracts untouched, but every PR
    # must make that judgment reviewable instead of silently skipping it.
    fields = {}
    for key in ("Impact", "Reason", "Contracts"):
        match = re.search(rf"^Docs-{key}:\s*(.*?)\s*$", body, re.MULTILINE | re.IGNORECASE)
        fields[key] = match.group(1).strip() if match else ""
    impact = fields["Impact"].lower()
    errors = []
    if impact not in {"updated", "none"}:
        errors.append("PR body needs Docs-Impact: updated or Docs-Impact: none")
    reason = fields["Reason"]
    if len(reason) < 20 or re.search(r"\b(TODO|TBD|placeholder)\b|<[^>]+>", reason, re.IGNORECASE):
        errors.append("Docs-Reason must explain the actual contract change or why none is needed (20+ characters)")
    if impact == "updated":
        contracts = [item.strip().strip('`') for item in fields["Contracts"].split(",") if item.strip()]
        # Compare POSIX forms on both sides: maintained paths come from the local
        # filesystem (backslashes on Windows) while `changed` comes from
        # `git diff --name-only` (always forward slashes). Without this every
        # declaration reads as "not a maintained document" on Windows.
        maintained = {path.relative_to(root).as_posix() for path in maintained_files(root)}
        changed_paths = {Path(item).as_posix() for item in changed}
        if not contracts:
            errors.append("Docs-Contracts must name the updated maintained documents, separated by commas")
        for contract in contracts:
            if contract not in maintained or contract not in changed_paths:
                errors.append(f"Docs-Contracts: {contract} must be a maintained document changed in this PR")
    return errors


def changed_paths(base: str) -> list[str]:
    return subprocess.check_output(
        ["git", "diff", "--name-only", "-z", f"{base}...HEAD"], cwd=ROOT,
    ).decode().rstrip("\0").split("\0")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Git base ref for local PR impact checks")
    parser.add_argument("--pr-body", type=Path, help="UTF-8 PR body file; required with --base")
    parser.add_argument("--event", type=Path, help="GITHUB_EVENT_PATH; PR body is data, never shell code")
    args = parser.parse_args()
    errors = check_structure(ROOT)
    if args.event:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        if "pull_request" in event:
            pr = event["pull_request"]
            errors += check_impact(ROOT, changed_paths(pr["base"]["sha"]), pr.get("body") or "")
    elif args.base:
        if not args.pr_body:
            parser.error("--pr-body is required with --base")
        errors += check_impact(ROOT, changed_paths(args.base), args.pr_body.read_text(encoding="utf-8"))
    for error in errors:
        print(f"ERROR: {error}")
    print(f"Documentation checks: {len(errors)} error(s). Behavioral truth and owning-domain relevance still require review.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
