# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

Nothing has been released yet; everything below is unreleased. The first tagged release will
move these entries under a dated version heading.

## [Unreleased]

### Added
- Per-target memory layout: series memory, glossary, style guide, episode context and checkpoints
  now live under `<project>/<lang>`, so a glossary built for one target language no longer leaks
  into a different target. Existing installs (files directly under `<project>/`) keep working for
  the default target via a read/write fallback, so nothing is orphaned; a `migrate-memory` command
  to relocate them is planned. `compact-memory`/`resolve-conflicts`/`update-memory` gain `--target`.
- Stale-context detection: `analyze` records a fingerprint of the analyzed subtitle in
  `episode.context.json`; `translate` and `review` warn (never block) when the subtitle has changed
  since it was analyzed. Legacy context files without the fingerprint are not flagged.
- Command-layer wiring tests for every CLI command plus direct workflow tests, lifting overall
  coverage to ~91% (the `commands/` and `workflows/` packages were the thin spots after the split).
- Translation now uses the analyzed per-character speech style/register (relevance-filtered per
  block, like gender) and the episode summary (in the always-sent base rules) — context that was
  recorded by `analyze` but previously ignored when translating.

### Fixed
- `batch --no-resume` is now honoured: the flag was defined but never forwarded to each episode's
  translation, so checkpoints were always reused.
- `translate`/`batch` refuse to write the output over the file they are reading from (a misaimed
  `--output`/`--out-dir`, even with `--force`), so the source subtitle can't be destroyed.
- Model output is sanitized before reinsertion: an ASS override block returned inside a line
  (e.g. `{\b1}`) is stripped instead of becoming a live tag; literal braces in dialogue are kept.
- Sidecar/output language suffixes now cover the full ISO 639-1 set, so a file in any language
  (e.g. `episode.ru.srt`) is detected, not only a hardcoded handful.
- `ollama` and `litellm` reject null/non-text model content with a retryable provider error
  instead of passing `None` downstream (which raised a traceback).
- Raised dependency floors to the versions actually tested (`typer>=0.13`, `pysubs2>=1.8`); older
  typer mishandled this CLI's combined option flags.
- `review --apply` and `tighten --apply` preserve whole-line leading ASS override tags instead of
  dropping positioning, alignment, colour and related formatting when replacing visible text.
- Empty model suggestions are never treated as safe automatic review fixes, and modified subtitle
  files are structurally validated before the atomic replacement.
- Project memory, glossary, conflicts and translation checkpoints now use strict versioned schemas;
  malformed persisted values are rejected early, while legacy unversioned files remain readable.
- Ollama HTTP/JSON/UTF-8 failures are normalized as provider errors; retries distinguish transient
  failures from permanent authentication/configuration errors, honour `Retry-After`, and add
  bounded jitter to exponential backoff.
- Readability compaction rejects non-string values instead of coercing arrays/objects into bogus
  subtitle text.
- Review reports extra/missing events, duplicate stable IDs, timestamp/order mismatches and
  relevant ASS style differences.

### Added
- Deterministic round-trip, episode analysis, per-series memory, automatic review, readability
  control, and the full CLI with agent-CLI and local-model (Ollama/LiteLLM) providers.
- `doctor` command: a no-LLM environment check (media tools on PATH, writable data/cache dirs,
  and — with `--provider` — the provider's backend), exiting non-zero on any hard failure.
- Capped exponential backoff with jitter between transient provider retries
  (`retry_provider_call`), including `Retry-After` support for rate limits.
- `translate --fail-on-untranslated`: exit non-zero when any line kept the source text (provider
  returned empty), so a batch/script can detect a partial translation; the file is still written.
- Per-block translation checkpoint (resume + content cache): each block's result is persisted as
  it returns, keyed by a hash of its input, so a crash on the last block no longer discards the
  rest and a rerun reuses unchanged blocks. Scoped to the `provider|model` signature; `--no-resume`
  re-translates from scratch. Applies to the CLI/API providers, not `identity`/`file-handoff`.
- A live progress bar on `translate` (current block, count and ETA) when run on a terminal.
- `batch` command: translate every matching file in a directory (`--glob`, default `*.mkv`,
  repeatable; `-r` recurses), sharing one `--project`. Files that look like the tool's own output
  are skipped at discovery; an already-translated episode is skipped (unless `--force`) and a
  per-episode failure is recorded and stepped past, so one bad episode never aborts the season.
  A summary reports translated/skipped/failed and the command exits non-zero on any failure.
- `config` command and per-project `settings.json`: pin default provider/model/target/lang/format/
  reasoning for a series once. `translate` and `batch` resolve each option as explicit flag >
  project setting > built-in default. Intentionally narrow (a few per-project keys), not a global
  config file.
- `--strict-lang` to refuse a different-language subtitle instead of falling back silently,
  plus a warning when a fallback happens and a notice of the selected sidecar.
- CI dependency vulnerability scan (`pip-audit`) and least-privilege `GITHUB_TOKEN` permissions.

### Changed
- Refactored the large pipeline and CLI modules into focused `workflows/` and `commands/`
  packages. The original modules remain stable compatibility facades with unchanged public
  function parameters, command options and help output.
- Translation checkpoint hashes include an explicit prompt version, invalidating cached blocks
  whenever the translation instructions change.
- The per-block translation checkpoint now hashes each block's surrounding context (the
  before/after lines), not just its own lines, so editing a line re-translates the neighbouring
  blocks that saw it as context instead of reusing a now stale-context translation
  (`CHECKPOINT_VERSION` is now 3 after subsequent schema/prompt-version hardening, invalidating
  older checkpoints).
- `settings.json` is validated: `provider`, `format` and `reasoning` must be known values, so an
  invalid setting (via `config` or a hand-edited file) is rejected up front rather than failing
  at translate time.
- More error-path coverage for the backend adapters (missing binary, non-zero exit, timeout,
  Claude error envelope, Ollama connection failure).
- Adopted `ruff format` as the code formatter (one-time reformat of the tree) and added a
  `ruff format --check` gate to CI alongside the existing `ruff check` lint.
- Added a tag-heavy `.ass` round-trip suite (real file parsed from disk: leading `\an8`/`\pos`,
  inline colour, mid-text animation, karaoke, drawing, comment) and ffmpeg-gated integration tests
  that mux a subtitle track into an `.mkv` and probe/extract it back.
- `translate` now defaults to the `claude` provider (matching the other commands) instead of
  `identity`, so it no longer silently produces a passthrough copy and reports success;
  `identity` remains available but only when chosen explicitly.
- `file-handoff` validates each output file's `block_id` and that it carries exactly the ids of
  its block, rejecting stale or misplaced `*.out.json` results.
- `review --apply` now requires matching timestamps (not just equal counts) before applying
  fixes, so a same-length but reordered/retimed target can't receive a fix on the wrong cue.
- Every file the tool writes — `translate` output, `review --apply` and `tighten --apply` —
  goes through a shared atomic helper (unique temp + optional validation + `os.replace`), so an
  interrupted/invalid run never leaves a corrupt file and concurrent writers can't collide on a
  predictable temp name.
- CI uses the committed `uv.lock` (`uv sync --frozen`); the sdist ships `SECURITY`, `CHANGELOG`
  and `CONTRIBUTING`, and the README is the single authoritative source for current behaviour.
- Extracted subtitle tracks are cached under a name keyed by a hash of the source
  (path/size/mtime/track) and written atomically, so different videos with the same filename
  can't collide and a crash never leaves a partial track; an unchanged file is reused.
- Job-protocol models (`job_protocol.py`) reject unknown keys (`extra="forbid"`), and a block
  reply with a non-string translation value is rejected instead of being coerced with `str()`.
- CI runs the test suite on Linux (Python 3.11–3.13), macOS and Windows, backing the
  "OS Independent" classifier.
- `translate_subtitle()` defaults to the `claude` provider (matching the CLI and the other
  pipeline functions) instead of `identity`. `file-handoff` writes its `*.in.json` atomically.
  The shared atomic-write helper now lives in `translate_subs/fsutil.py`.
- Stricter memory schema: character `gender` is a `Literal`, models reject unknown keys
  (`extra="forbid"`) and validate on assignment; unexpected LLM gender values fold to `unknown`
  instead of entering memory.
