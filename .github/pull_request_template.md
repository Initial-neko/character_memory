## Summary

Describe the change and the user/developer problem it solves.

## Scope

List the main behavior or files changed. Call out anything intentionally out of scope.

## Validation

- [ ] Focused tests passed
- [ ] `uv run pytest -q` passed, or the reason it was not run is documented
- [ ] Browser/live-model validation was performed when relevant

## Documentation

Docs-Impact: <updated or none>
Docs-Reason: <explain the durable contract change, or why behavior/config/persistence/lifecycle are unchanged>
Docs-Contracts: <comma-separated changed maintained documents; omit for none>

Run `python scripts/check_docs.py`; CI checks the fields above on each push and PR body edit. An updated contract must be present in the diff. Behavioral accuracy and owning-domain relevance still require review.


- [ ] `README.md` still reflects the public project entry points
- [ ] Relevant `docs/current/` contracts were updated for durable behavior changes
- [ ] No secrets, local model assets, generated agent plans, or machine-specific paths were committed

## Known limitations

List follow-up work, compatibility breaks, or environment-specific constraints.
