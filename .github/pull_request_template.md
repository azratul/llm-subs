## What and why

Briefly describe the change and the motivation. Link any related issue (`Fixes #123`).

## Checklist

- [ ] `uv run pytest -q` passes
- [ ] `uv run ruff check translate_subs/ tests/` and `uv run ruff format --check translate_subs/ tests/` pass
- [ ] `uv run mypy translate_subs/` passes (strict mode, configured in pyproject)
- [ ] Behaviour changes are covered by a test
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (if user-visible)
- [ ] Code is in English (identifiers, comments, CLI strings); comments explain *why*, not *what*

## Notes for reviewers

Anything non-obvious: a design trade-off, a deliberate scope limit, or a follow-up left for later.
