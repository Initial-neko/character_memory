"""Behavioral tests for the dependency-free local/CI documentation gate."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("check_docs", Path(__file__).resolve().parents[1] / "scripts/check_docs.py")
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class DocumentationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs/current").mkdir(parents=True)
        (self.root / "docs/current/VOICE.md").write_text("# Voice\n", encoding="utf-8")
        (self.root / "docs/README.md").write_text("[Voice](current/VOICE.md)\n", encoding="utf-8")

    def test_valid_index_and_local_links(self):
        self.assertEqual(checks.check_structure(self.root), [])

    def test_new_contract_requires_index_entry(self):
        (self.root / "docs/current/NEW.md").write_text("# New", encoding="utf-8")
        self.assertTrue(any("NEW.md: missing from" in error for error in checks.check_structure(self.root)))

    def test_moved_document_leaves_broken_reference(self):
        (self.root / "docs/current/VOICE.md").unlink()
        self.assertTrue(any("missing local target" in error for error in checks.check_structure(self.root)))

    def test_examples_and_remote_links_are_not_fetched(self):
        with (self.root / "docs/README.md").open("a", encoding="utf-8") as file:
            file.write("\n[Web](https://example.invalid/foo)\n[Heading](#heading)\n```md\n[Example](missing.md)\n```\n")
        self.assertEqual(checks.check_structure(self.root), [])

    def test_reference_links_and_url_encoded_paths(self):
        (self.root / "docs/current/with space.md").write_text("# Other", encoding="utf-8")
        with (self.root / "docs/README.md").open("a", encoding="utf-8") as file:
            file.write("\n[Other][other]\n[other]: current/with%20space.md\n")
        self.assertEqual(checks.check_structure(self.root), [])

    def test_undocumented_runtime_pr_fails(self):
        self.assertTrue(checks.check_impact(self.root, ["src/feature.py"], "fix runtime"))

    def test_placeholder_reason_fails(self):
        self.assertTrue(checks.check_impact(self.root, ["src/feature.py"], "Docs-Impact: none\nDocs-Reason: <explain why no documentation is needed>"))

    def test_no_contract_change_can_be_explicitly_explained(self):
        self.assertEqual(checks.check_impact(self.root, ["src/feature.py"], "Docs-Impact: none\nDocs-Reason: Internal cache refactor; API, persistence and lifecycle contracts are unchanged."), [])

    def test_updated_requires_the_named_contract_to_change(self):
        body = "Docs-Impact: updated\nDocs-Reason: Voice provider routing now preserves a failed result.\nDocs-Contracts: docs/current/VOICE.md"
        self.assertTrue(checks.check_impact(self.root, ["src/feature.py"], body))
        self.assertEqual(checks.check_impact(self.root, ["src/feature.py", "docs/current/VOICE.md"], body), [])

    def test_unmaintained_or_unknown_contract_cannot_satisfy_gate(self):
        body = "Docs-Impact: updated\nDocs-Reason: Historical research was adjusted after implementation.\nDocs-Contracts: docs/research/VOICE.md"
        self.assertTrue(checks.check_impact(self.root, ["docs/research/VOICE.md"], body))
