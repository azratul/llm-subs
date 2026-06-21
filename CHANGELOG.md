# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-06-21

First tagged release. Everything below shipped in 0.1.0.

### Added
- Per-target memory layout: series memory, glossary, style guide, episode context and checkpoints
  now live under `<project>/<lang>`, so a glossary built for one target language no longer leaks
  into a different target. `compact-memory`/`resolve-conflicts`/`update-memory` gain `--target`.
- Episodes are disambiguated by source folder: the per-episode directory key is the source stem
  plus a short hash of its containing directory, so two same-named files in different folders (e.g.
  `Season 1/Episode 01` and `Season 2/Episode 01`) under one project no longer share context or
  translation checkpoints; the same file still maps to the same directory so resume is stable.
- Stale-context detection: `analyze` records a fingerprint of the analyzed subtitle in
  `episode.context.json`; `translate` and `review` warn (never block) when the subtitle has changed
  since it was analyzed. Legacy context files without the fingerprint are not flagged.
- Command-layer wiring tests for every CLI command plus direct workflow tests, lifting overall
  coverage to ~91% (the `commands/` and `workflows/` packages were the thin spots after the split).
- Translation now uses the analyzed per-character speech style/register (relevance-filtered per
  block, like gender) and the episode summary (in the always-sent base rules) — context that was
  recorded by `analyze` but previously ignored when translating.

### Added
- `analyze`, `review` and `tighten` now resolve unset options (target, provider, model,
  reasoning, lang) from the project's `settings.json`, matching `translate`/`batch` — so a
  per-series default set once with `config` applies to the whole workflow. `tighten` gains a
  `--target` flag.
- Agent CLIs are now invoked with their own built-in restrictions, since subtitle text is
  untrusted input fed to a tool-capable agent: `codex --sandbox read-only`, `claude` denies every
  filesystem/exec/network/subagent tool (`--disallowedTools`) and ignores MCP servers
  (`--strict-mcp-config`), `gemini --approval-mode plan` (read-only), and `opencode --pure` (no
  external plugins, and never `--dangerously-skip-permissions`).

### Fixed
- `.ass` output validation now also checks fidelity: each event must keep its source style and its
  whole-line leading override block (`{\an8\pos(..)}`), so a silently dropped position/colour/
  alignment fails validation and nothing is written (the check is scoped to the translate path,
  where output events come from the same units; `review`'s translated-vs-source comparison is
  unaffected).
- The `--target` can no longer steer a write outside its directory: it is validated as a language
  tag up front (path separators, `..` and empty values are rejected), the output-filename language
  code is reduced to alphanumerics, and `translate` additionally asserts the resolved output stays
  inside the intended directory. Previously a crafted target (e.g. `../../tmp/x`) flowed unsanitized
  into the output filename.
- Agent CLIs now run from an empty throwaway working directory, so on top of each CLI's read-only
  sandbox a crafted subtitle cannot nudge the agent toward whatever files happen to sit in the
  user's real working directory.
- `review` and `tighten` no longer send a whole long episode to the model in a single prompt:
  lines are chunked into blocks (40), like `translate`, which avoids truncation/timeouts and keeps
  the model's attention focused (each block still carries the episode-spanning glossary/gender
  sheet). Findings/compactions are merged across blocks.
- `review` and `tighten` reports carry a provenance manifest (source/translated file names, target,
  and a content fingerprint), so a report left behind from an earlier run is distinguishable from
  one matching the current subtitle.
- `tighten` writes its readability report to the same per-episode directory as the rest of that
  episode's state (`<project>/<target>/<episode-key>/`), resolving project/episode/target the way
  `translate` and `review` do, instead of a divergent `<project>/<lang-from-filename>/<stem>/`
  location that lost the project, variant and episode-key.
- `review --apply` no longer lets two safe fixes on the same line clobber each other: since each is
  a whole-line replacement, a line with more than one distinct suggestion is left for a human
  rather than silently keeping only the last.
- Per-series memory is now segmented by the **full target**, not the collapsed language code:
  `es-latam` and `es-ES` (or any two variants of one language) get separate memory subtrees instead
  of sharing `<project>/es/`, so a Castilian glossary can't contaminate a Latin-American run.
- When `--project` is omitted, a season/specials subfolder (`Season 1`, `S02`, `Specials`, …) is
  skipped in favour of the series folder above it, so two unrelated series sitting in their own
  `Season 1` folders no longer default to the same project and share memory.
- The stale-context fingerprint now includes each line's **speaker**, so reassigning a line to a
  different character (which can flip gender/register) is flagged as a changed source instead of
  silently passing the old context as still valid.
- `review --apply` validates a `proper_name` fix deterministically: the suggested line must contain
  a character name known to series memory, otherwise it stays a suggestion (with no known names, no
  proper_name fix is auto-applied) — the same treatment glossary fixes already get.
- `ReadabilityLimits` rejects non-positive values up front instead of producing nonsensical
  budgets.
- Generated files (translated subtitles, review/readability reports, memory and checkpoints) now
  respect the process umask instead of always being created `0600`, so a media server or another
  user (Jellyfin/Plex) can read the output.
- The per-block translation checkpoint is keyed on the model the runner actually used, not just the
  `--model` flag: when `--model` is omitted the runner's own default (e.g. `claude-opus-4-8`) goes
  into the signature, so changing that default later re-translates instead of reusing blocks from
  the previous model.
- `review --apply` auto-applies a `glossary` fix only when the suggested line actually contains the
  expected glossary rendering — a deterministic check, rather than trusting the model's `auto_safe`
  label on a fix that carries no glossary term.
- `tighten --apply` writes a compaction only when it is a real readability improvement; one that
  introduces a new limit violation (e.g. splits a long line into three) or grows the text is
  reported but left out of the file, so `--apply` can never make a subtitle worse.
- `batch` records a pre-existing output as *skipped* through a typed `OutputExistsError` instead of
  matching `"already exists"` in the error text, so an unrelated error that happens to contain that
  phrase is correctly counted as *failed*.
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

[Unreleased]: https://github.com/azratul/translate-subs/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/azratul/translate-subs/releases/tag/v0.1.0
