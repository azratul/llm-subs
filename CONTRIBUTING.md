# Contributing

Thanks for your interest in improving `llm-subs`. This is a focused command-line tool;
see the **Scope, non-goals and limitations** section of the [README](README.md) before
proposing large features, so effort isn't spent on things that are intentionally out of scope.

## Development setup

Requires Python ≥ 3.11, [`uv`](https://docs.astral.sh/uv/), and `ffmpeg`/`ffprobe` on PATH.

```bash
uv sync                 # install deps (incl. dev tools) into the project venv
uv run llm-subs --help
```

## Before opening a pull request

All must pass; CI enforces them on Python 3.11–3.13:

```bash
uv run ruff check translate_subs/ tests/
uv run ruff format --check translate_subs/ tests/   # run `ruff format` (no --check) to fix
uv run mypy translate_subs/
uv run pytest -q
```

New behaviour needs tests. Bug fixes should come with a test that fails without the fix.

## Conventions

- **All code is in English** — identifiers, CLI strings, comments and docstrings. (Issues and
  discussion may be in any language; the code is not.)
- **Comments explain the *why*** (non-obvious decisions, invariants, edge cases), never the
  *what* the code already states. No commented-out code.
- **Never send the raw subtitle file to an LLM.** Parse it, extract visible text with stable
  IDs, send `[ID] text`, and reinsert by ID. This invariant drives the whole design — keep the
  deterministic core (parsing/reinsertion/validation) free of any provider's quirks.
- Adding a translation backend means adding a runner (`prompt -> text`) behind
  `CliTranslationProvider`; it must not leak into parsing or reinsertion.

## Commit messages

Write clear, imperative one-line summaries in English (e.g. "Add --strict-lang to translate").
Keep unrelated changes in separate commits.
