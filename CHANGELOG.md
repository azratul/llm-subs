# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `review` now pairs source and target lines by `unit.event_index` instead of sequential
  position, so non-translatable ASS events (drawings, comments) preserved verbatim in the
  output no longer shift the pairing. For SRT targets (where `prune_to_units` and
  `flatten_overlaps` have removed those events) the pairing falls back to sequential
  position. Comment events with visible text are no longer falsely reported as `extra_event`.
- `review --apply` guards against stale fixes: a safe replacement is skipped when the
  translated line has been edited since the review was generated, preventing a fix derived
  from stale context from corrupting a hand-edited line.
- `translate` now falls back to a wrong-language sidecar when the embedded track probe
  raises `MediaToolError` (e.g. an empty/corrupt container), not only when it raises
  `SourceError`. The sidecar priority chain — exact-match sidecar → embedded track →
  wrong-language sidecar — is now enforced correctly in all cases.
- ASS output no longer deletes drawings, comment events, and other non-translatable events.
  `prune_to_units` is now called only for SRT output; ASS output preserves all source
  events verbatim and `validate_output` compares by `event_index` rather than total count.
- Alias detection in `compact-memory` no longer raises `AttributeError` when the LLM
  returns a malformed `duplicates` list (non-dict items, truncated JSON). The entire parse
  and loop is now inside a single `try/except` that surfaces all failures as a retryable
  `ProviderError`.
- Source language matching now recognises ISO 639-2/B codes reported by ffprobe (`rus`,
  `ara`, `pol`, `ces`, `hun`, `ukr`, `tur`, and ~45 more), mapping them to their ISO 639-1
  equivalents so track selection and sidecar matching work correctly for those languages.
- Release workflow now runs the full test/lint/type-check suite and verifies that the pushed
  tag matches `pyproject.toml` version before building artifacts.

## [0.2.3] - 2026-06-24

### Fixed
- Translation prompts no longer grow unboundedly with series history. Relationship pairs
  injected per block are now capped at 20 (speaker-involved pairs first, then text-mentioned),
  so token cost stays constant regardless of how many episodes have been analyzed. Bidirectional
  pairs (A→B stored in A's entry AND B→A stored in B's entry) are now deduplicated before
  injection, keeping the most informative description; this halved the raw pair count for
  typical long-running series. Together the two changes reduce relationship context overhead
  by up to 82% for a fully-analyzed series.
- Fixed two mypy errors introduced in 0.2.2: a variable name reuse across `EpisodeCharacter`
  and `CharacterMemory` loop variables in `build_memory_rules` (now uses distinct names), and
  a mismatched callback type annotation for `alias_confirm` in `compact_memory` (now typed as
  `Callable[..., str]` instead of `ConflictPrompt`).
- ETA in `batch` output now uses exponential moving average (α=0.3) instead of a simple mean,
  so a single slow episode does not skew the estimate for the rest of the run.

## [0.2.2] - 2026-06-23

### Added
- `compact-memory` now accepts `--provider` / `--model` to run an LLM pass that detects
  character aliases — entries for the same character stored under different name forms (e.g.
  given name only vs. full name). The model receives every character's complete profile (gender,
  speech style, relationships) and is asked to identify high-confidence duplicate pairs; name
  overlap alone is not enough evidence. Each detected pair is presented interactively for
  confirmation before the merge is applied (`-y` / `--non-interactive` to auto-apply). On merge,
  the alias entry is removed, its relationships are folded into the canonical entry, and all other
  characters' relationship references to the alias are rewritten to the canonical name.
- Per-episode timing in `batch`: starting from the second episode, the progress line shows how
  long the previous episode took and an ETA based on a rolling average. The final summary line
  shows total elapsed time for the phase. With `--pre-analyze`, each phase is timed independently.
- `batch --pre-analyze` skips episodes whose `episode.context.json` is already current (source
  hash matches), so re-running after a partial failure only re-analyzes what is missing. The
  per-episode table and summary now include a "skipped" count alongside analyzed/failed.

### Changed
- `batch` (translate and analyze phases) now aborts immediately on `ProviderError` instead of
  recording it as a per-episode failure and continuing. A `ProviderError` signals a systemic
  condition — rate limit, quota exhausted, wrong model name, authentication failure — that will
  affect every subsequent episode. Per-episode errors that are not provider failures (bad subtitle
  file, missing track, etc.) still continue as before.

## [0.2.1] - 2026-06-23

### Added
- `batch` now shows per-episode timing while running: starting from the second episode, the
  progress line includes how long the previous episode took and an ETA for the remaining ones
  (computed as a rolling average of completed episodes). The final summary line includes the
  total elapsed time for the phase.
- `batch --pre-analyze` skips episodes whose `episode.context.json` is already current (source
  hash matches), so re-running after a partial analyze phase does not re-analyze episodes that
  succeeded — only the ones that failed or are new get a fresh LLM call. The per-episode table
  and the summary line now include a "skipped" count alongside analyzed/failed.
- `batch --pre-analyze` now shows a per-episode results table after the analysis phase, listing
  each episode's status and the full error message for failures — identical in style to the
  translation summary table.

### Changed
- `batch` (translate and analyze phases) now aborts immediately when a `ProviderError` propagates
  out of an episode instead of recording it as a per-episode failure and continuing. A
  `ProviderError` signals a systemic condition — rate limit, quota exhausted, wrong model name,
  authentication failure — that will affect every subsequent episode, so stopping early and
  surfacing the original provider message is more useful than silently accumulating failures across
  a multi-hour run. Per-episode errors that are not provider failures (bad subtitle file, missing
  track, etc.) still continue as before.

### Fixed
- Analysis prompt now instructs the model to use the most complete form of a character's name
  (family + given for Japanese names; consistent with any prior-known entry), preventing the same
  character from being recorded under both a short form and a full form across episodes.
- Analysis prompt now explicitly requests all prose fields (episode_summary, speech_style,
  relationship descriptions) in the target language, so the memory files no longer mix languages
  when the model arbitrarily chose English for some episodes.
- CLI provider adapters (`agy`, `codex`, `opencode`) now raise a proper `ProviderError` when the
  subprocess exits successfully but produces no output, including any stderr content in the message.
  Previously an empty stdout was silently passed through and surfaced as a confusing
  "not valid JSON: … char 0" error with no hint of the real cause (rate limit, timeout, etc.).

## [0.2.0] - 2026-06-23

### Added
- `batch --pre-analyze`: runs a full `analyze` pass over every episode before translating, so
  the complete series memory (characters, glossary, style guide) is available from the very first
  episode rather than accumulating incrementally. Failed analyses are noted and skipped; translation
  proceeds regardless.
- Per-project `analyze_provider`, `analyze_model` and `analyze_reasoning` settings: set them once
  with `config --analyze-provider / --analyze-model / --analyze-reasoning` to use a different
  (typically stronger) model for analysis than for the high-volume translation pass.
- Visual before/after diff table when `review --apply` or `tighten --apply` writes changes: a
  Rich table with red/green columns shows exactly which lines were rewritten.
- Parallel block translation for `ollama` and `litellm` (4 workers, thread-safe checkpoint): API
  providers that are pure HTTP now translate up to four blocks concurrently; CLI providers
  (`claude`, `codex`, etc.) remain sequential.
- Spinners for blocking operations that have no progress bar: `analyze`, `review`, `tighten`,
  and the extract step inside `translate` now show a spinner so the terminal is never silently
  frozen.

### Changed
- `flatten_overlaps` (SRT overlap merging) replaced the O(n²) sequential scan with an
  O(n log n) sweep-line algorithm; negligible on typical episodes, measurable on dense fansub files.

## [0.1.0] - 2026-06-22

First tagged release.

### Fixed
- Review: a model returning the JSON string `"false"` for `auto_safe` no longer reads as truthy
  (Python's `bool("false")` is `True`), so a finding the model marked not-auto-safe can no longer
  slip past the safe-fix gate. Only a real boolean `true` or the string `"true"` counts as auto.
- Interactive track selection now reports a friendly error for non-numeric input instead of
  letting a raw `ValueError` propagate.
- A path-like `target` in a project's `settings.json` is now rejected when the file is loaded
  (same validation as the workflows), not silently carried until translate time.
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
- `analyze`, `review` and `tighten` now resolve unset options (target, provider, model,
  reasoning, lang) from the project's `settings.json`, matching `translate`/`batch` — so a
  per-series default set once with `config` applies to the whole workflow. `tighten` gains a
  `--target` flag.
- Agent CLIs are now invoked with their own built-in restrictions, since subtitle text is
  untrusted input fed to a tool-capable agent: `codex --sandbox read-only`; `claude` denies every
  filesystem/exec/network/subagent tool (`--disallowedTools`) and ignores MCP servers
  (`--strict-mcp-config`); `antigravity` (`agy`) runs `--print --sandbox`; `opencode --pure` (no
  external plugins, and never `--dangerously-skip-permissions`). Each CLI also runs from an empty
  throwaway working directory so a crafted subtitle cannot steer the agent at the user's files.
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
- Review provenance manifest is now complete: alongside the source/translated filenames, target
  and source fingerprint, the report records a fingerprint of the translated content and the
  provider/model used, so a report can be matched against the exact translated file it reviewed.
- Replaced the `gemini` provider with `antigravity` (the `agy` CLI that supersedes the standalone
  Gemini CLI). It runs `agy --print --sandbox` with the prompt on stdin; models use `agy`'s
  descriptive names (e.g. `"Gemini 3.5 Flash (Low)"`). **Breaking:** `--provider gemini` and a
  `gemini` value in `settings.json` are no longer accepted — use `antigravity`. Security note:
  `agy` is agentic and has no read-only/no-tools mode (its `--sandbox` only restricts the
  terminal), so unlike the other agent CLIs its only containment is the throwaway working
  directory; `--dangerously-skip-permissions` is never passed.
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

[Unreleased]: https://github.com/azratul/translate-subs/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/azratul/translate-subs/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/azratul/translate-subs/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/azratul/translate-subs/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/azratul/translate-subs/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/azratul/translate-subs/releases/tag/v0.1.0
