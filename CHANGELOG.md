# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **`batch --pre-analyze` now honours `--strict-lang`.** The flag reached the translate pass but
  not the analysis pass, so a wrong-language source (e.g. the only embedded track is Spanish when
  `--lang en` was requested) would have its characters and glossary merged into the shared series
  memory before the translate pass rejected that same source — persistent memory contamination
  from a language the user explicitly refused. The analysis pass now applies the same strict check
  and records the episode as failed instead.
- **Output-staleness digest now covers the full `.ass` output surface.** For `.ass` the output
  preserves more than the translatable units: `[Script Info]` headers (PlayResX/PlayResY rescale
  every coordinate), the Aegisub project section, embedded attachments (`[Fonts]`/`[Graphics]`),
  `[V4+ Styles]` definitions (all fields, including tertiary colour and alpha level), each event's
  layout metadata (layer, actor, margins, effect), and the non-translatable events (drawings,
  comments) copied through verbatim. The source fingerprint only hashed the units, so a restyle, a
  resolution change, a margin edit, or a drawing change left the output looking up to date. `.srt`
  is flat and prunes those, so its digest stays units-only. Existing `.ass` manifests are re-flagged
  stale once (regenerate with `--force`).

### Added
- **Automatic input-encoding detection** with a `--encoding` override. Subtitle sidecars are
  frequently CP1252 (Western), Shift-JIS (Japanese `.srt`) or UTF-16, which pysubs2's UTF-8 default
  turned into mojibake or a hard parse error. `translate`, `batch`, `analyze`, `review`, `tighten`
  and `validate` now sniff the bytes first — a BOM (UTF-8/16/32) and strict UTF-8 are handled
  directly; for the legacy remainder **`--lang` acts as a codepage hint** (en/es/fr/… → CP1252,
  pl/cs/hu/… → CP1250, ja → CP932, ru/uk/… → CP1251, and so on), trusted only when the hinted codec
  decodes the bytes strictly — near-identical codepages (CP1250 vs CP1252 differ in a few positions
  like €/Ł) are genuinely beyond byte statistics, and the user's declared source language is a
  stronger prior. Files without a usable hint are resolved statistically via **charset-normalizer**
  (added as a dependency; a strict decode ladder can't tell CP1252 from Shift-JIS and silently
  corrupts one). Pass `--encoding cp1252` (etc.) as the definitive override; an unknown codec
  name is now a short `unknown encoding '...'` error instead of a traceback. `review --encoding`
  applies to the **source only** — the translated file is this tool's own UTF-8 output and is always
  auto-detected, so a legacy source encoding can't corrupt it.
- `project-status <project> [--target ...]`: show a project's stored state on disk — glossary/
  character/conflict counts, and per-episode whether it was analyzed, whether a checkpoint *file*
  is present (not whether it will resume — that depends on run-time settings this offline view
  can't check), and which output paths are tracked (read from each manifest, so legacy/corrupt
  manifests are ignored). No LLM call and no source access; output staleness is not recomputed here
  (run `batch` for that).
- `--json` on `doctor`, `validate`, `batch` and `project-status` for machine-readable output
  (scripting/automation), emitted on stdout with the same exit codes as the human view.
- The output manifest now records the **llm-subs version** and a **content hash of the output
  file**. On a later run `translate`/`batch` detect that an output was **edited by hand** since it
  was generated (its hash no longer matches) and refuse to overwrite it without `--force`; `batch`
  reports such an episode as `modified`. Protects manual corrections from being silently clobbered.
- CI enforces a branch-coverage floor (`--cov-branch --cov-fail-under=85`) as a regression net.
- The whole package now type-checks cleanly under **strict mypy** (0 errors, down from 73). Strict
  mode is set once in `[tool.mypy]` (`strict = true`), so a plain `mypy translate_subs/` — in CI, the
  release workflow, and the docs — is strict with no per-invocation flag to drift. The shipped
  `py.typed` API is now fully annotated:
  command callbacks return `-> None` and generic containers are parameterised. The injected
  dependency seams (`resolve_source_fn`, `provider_factory`, `validate_output_fn`,
  `ai_runner_factory`, `discover_inputs_fn`) are typed as **`Protocol`s** with explicit `__call__`
  signatures (`workflows/seams.py`), so mypy verifies the keyword arguments at every call site *and*
  that the concrete facade functions match — a check the earlier `Callable[..., X]` aliases skipped.
  The per-episode `translate_fn`/`analyze_fn` batch seams stay `Callable[..., X]` by design (`batch_*`
  forwards an arbitrary `**kwargs` a fixed signature couldn't express). No runtime behaviour changed.
- The output manifest now records the **resolved model** the runner will actually use, not the
  (possibly unset) `--model` flag: with `--model` omitted, a CLI provider's built-in default (e.g.
  `claude-opus-4-8`) is stored instead of an empty string, so a later change to that default flags
  affected outputs stale. `identity`/`file-handoff` (no model) still record `""`. A manifest written
  before this change (empty model) is flagged stale once against the now-known default — an honest
  "can't prove the old output used this model", never a data loss.
- The same resolved-model provenance now applies to **analysis** (`analyze` / `batch --pre-analyze`):
  the analysis signature records the model the runner actually used, so an analysis done with a
  Claude default is re-run when that default changes, instead of being trusted on a matching source
  fingerprint alone.

### Changed
- **CI covers the dependency promises.** The audit job now syncs `--all-extras`, so `pip-audit`
  covers litellm's dependency tree instead of only the core install, and a new `minimum-versions`
  job resolves every direct dependency to its lowest declared version (`--resolution
  lowest-direct`, Python 3.11) and runs the full suite against it — the floors are a tested
  contract, not an aspiration. That exposed one dishonest floor: pydantic 2.7 is uninstallable on
  CPython 3.14 (no pydantic-core wheels, and the old core doesn't build there), so the pydantic
  requirement is now per-Python (`>=2.7` below 3.14, `>=2.12` from 3.14 on).
- `batch` now **isolates per-episode content failures** instead of aborting the whole season on any
  `ProviderError`. Each failure carries a cause (`content`, `auth`, `config`, `quota`, `service`,
  or `unknown`): a **content/protocol** fault — an unparseable reply or wrong ids for one episode —
  records that episode as `failed` and continues, since the next episode's content is independent.
  Every systemic cause (auth/config, quota/rate-limit, a service outage) — and any unclassified
  error, which stays systemic by default — still aborts the run, since retrying the season would
  just repeat it. The cause is preserved through the retry wrapper so `batch` can see it after
  retries are exhausted.
- The output manifest is now **per artifact**, named by a hash of the output's *resolved path*
  (`<hash>.manifest.json`), not one fixed file per episode. Distinct outputs of the same episode —
  an `.ass` and an `.srt`, or the same basename written to two different directories — kept a single
  shared manifest, so force-refreshing one silently marked the others up to date; each output now
  tracks its own provenance and records its format and full path. Hashing the full path (not the
  basename) also avoids colliding two same-named outputs and keeps the filename within the
  filesystem's length limit. Pre-existing `output.manifest.json` files are treated as legacy/absent
  (the output is skipped, never wrongly flagged stale) until its next `--force` rewrite.
- `review --apply` now applies a **term-level fix surgically**. A glossary/proper-name/honorific
  correction must change exactly one contiguous span **and that span must be short** (≤16 chars — a
  term, not a phrase); if the model's suggested line also reworded surrounding text, or expanded the
  term into a phrase (`Sword` → `Espada legendaria`), the fix is rejected and left for a human
  instead of overwriting the whole line. The size cap also bounds the space-less CJK case, where the
  whole line is a single token. Gender/empty-line/missing-id fixes still replace the line, as they
  legitimately may.

### Fixed
- Corrected stale `CONTRIBUTING.md` claims that contradicted the code: the *output-manifest*
  fingerprint **does** cover timing/style (a re-timed source is flagged stale — only the
  *episode-context* fingerprint is content-only), and `batch` no longer aborts on every
  `ProviderError` (content faults are per-episode). Softened the "no extra isolation needed" note.
- Resolved a `README.md` contradiction: the "non-goals" section claimed coverage is "measured and
  reported, not gated" while CI enforces an 85% floor. The README now describes the actual policy —
  a **low floor as a regression net**, but deliberately no strict 90–95% gate.
- `review --apply`/`tighten --apply` no longer make a later `translate`/`batch` wrongly report the
  output as **hand-edited** (`modified`). Those commands legitimately rewrite the translated file,
  but did not update its content hash in the manifest, so the next run saw a mismatch and refused to
  overwrite without `--force`. They now re-record the manifest's `output_hash` after writing, so only
  edits made *outside* the tool are flagged.
- `batch --json` now emits JSON on **every** path. A run that failed before starting (e.g. the input
  argument is not a directory) printed a Rich-formatted error to stdout, corrupting the JSON a script
  was parsing; the error is now a `{"error": ...}` object on stdout. The antigravity weak-isolation
  warning is likewise routed to stderr under `--json` so it can't contaminate the JSON on stdout.
- A **corrupt/unreadable output manifest** is no longer silently treated as "up to date" and
  skipped. When the manifest file exists but can't be parsed, `translate`/`batch` can't verify the
  output is current, so they surface it (reported `stale`, never overwritten without `--force`)
  instead of hiding it. A genuinely *absent* manifest (legacy/pre-feature output) is still skipped
  as before — only an existing-but-broken one is flagged.

### Security
- Stronger, louder guidance to prefer a local `ollama` model for untrusted material: `doctor
  --provider antigravity` emits an explicit weak-isolation warning, and **every command that runs
  the LLM** (`translate`, `batch` — including its `--pre-analyze` provider —, `analyze`, `review`,
  `tighten`, and `compact-memory --provider`) warns at runtime when the antigravity backend is
  actually used; the README security note calls it out too. No behaviour change to the sandboxing
  itself.

## [0.6.0] - 2026-07-01

### Security
- State and cache **directories** are now created owner-only (0700), matching the 0600 already used
  for the files inside them. Series memory, episode state and the extracted-track cache can carry
  subtitle text, so their directories must not be traversable by other users. Directories left 0755
  by an older release are repaired on the next write (and on `doctor`).
- `doctor` now audits state permissions: it reports any file or directory under the projects and
  cache roots that is still group/other-accessible (e.g. files written 0644 by an older release),
  with the `chmod` command to fix them. It is a warning, never a hard failure.

### Added
- The output manifest now fingerprints the **series memory and episode context** that steered the
  translation (`memory_hash`), so `batch` reports an output as **stale** when the glossary,
  characters, style guide or episode context changed — a prompt change the source fingerprint alone
  can't see. Editing the glossary and re-running `batch` now flags affected outputs instead of
  skipping them. Manifests written before this field are tolerated (no spurious staleness).
- `analyze`/`batch --pre-analyze` now record the **analysis provenance** (prompt version +
  provider/model) on each episode context and re-analyze when it changed, instead of skipping on a
  matching source fingerprint alone. Bumping the analysis prompt or switching backend refreshes
  cached contexts; contexts written before this field are trusted as current (not force-refreshed).
- `batch --fail-on-stale`: exit non-zero when any output was flagged **stale**
  (source/model/prompt/memory changed since it was written), for personal automation that wants a
  stale output to break the run rather than only warn. Symmetric with `--fail-on-untranslated`;
  stale remains a warning by default.

### Changed
- Widen `rich` support to include the 15.x series (dependency cap raised to `<16`).
- Two internal `assert` invariants (retry exhaustion, post-save validation) are now explicit
  `raise`s, so the guards hold under `python -O` (which strips `assert`).

### Fixed
- Docs: `.srt` output preserves whole-line **italic/underline**, not bold — pysubs2's SRT writer
  doesn't emit `<b>`. The README previously claimed "italic/bold".

## [0.5.0] - 2026-07-01

### Added
- `batch` now detects **stale outputs**. `translate` records a small manifest next to each
  episode's state (source fingerprint + target + provider/model + reasoning effort + prompt
  version). On a later `batch` run an existing output is reported as `stale` — surfaced as a
  warning, never silently overwritten — when its source, provider/model, reasoning or prompt
  changed since it was written, instead of being skipped as up to date. Unchanged outputs are
  still skipped; use `--force` to retranslate.
  Outputs produced before this release have no manifest and are treated as up to date (skipped),
  not stale. (An explicit `--model` change is detected; relying on a provider's built-in default
  and that default later changing is not.)
- Property-based tests (`hypothesis`) for the deterministic extraction/reinsertion core: generated
  events (plain text, whole-line ASS override blocks, CJK/accented Unicode, speakers, overlapping
  timings) assert the round-trip invariants — sequential unique ids, one unit per translatable
  event, identity round-trip preserves text/speaker, and `flatten_overlaps` both leaves no timed
  overlap and preserves the exact set of visible lines active at every instant (temporal/textual
  equivalence, not just absence of overlaps).
- `CONTRIBUTING.md` now documents the deliberate design decisions and known limitations so settled
  trade-offs aren't repeatedly re-reported as bugs.
- Dependabot config (`.github/dependabot.yml`): weekly grouped update PRs for the Python
  dependencies and the GitHub Actions used in CI.
- `review --apply` and `tighten --apply` now **preview the diff and ask for confirmation** before
  overwriting any line, since each fix/compaction is a whole-line replacement. Pass
  `--non-interactive`/`--yes`/`-y` to apply without the prompt (as before). `tighten` gained the
  `--yes`/`-y` alias for this.
- The test suite now treats warnings as errors (`filterwarnings`), so a `DeprecationWarning` from a
  dependency (or our own code) fails CI instead of scrolling past unnoticed. `ResourceWarning` is
  exempt, since it fires non-deterministically from garbage collection rather than signalling a
  defect.

### Changed
- Runtime dependencies gained upper caps (`pydantic<3`, `pysubs2<2`, `typer<1`, `rich<15`) so the
  next breaking major is adopted deliberately with a test run rather than resolved in silently.
- `EpisodeContext` (the saved `episode.context.json`) now carries a `schema_version` field so a
  future format change can detect and migrate older files. The model stays liberal about unknown
  keys (it validates the raw model reply), and legacy files without the field load as version 1.

### Fixed
- Stale-output detection now catches **timing and style** changes, not just text. The manifest's
  source fingerprint (`output_source_digest`) covers each cue's timing, style and leading override
  block, so a re-timed or re-restyled source — which leaves the existing output desynchronised while
  it still looked up to date — is flagged stale; `--force` then re-renders it, reusing the cached
  translations (keyed on text/context, not timing). The context-staleness check is unchanged
  (content only), since re-timing doesn't invalidate the character/glossary analysis.
- Corrected documentation contradictions: the README no longer lists Dependabot as out of scope (it
  is now configured), the CI comment no longer claims ffmpeg is fully mocked (a real integration
  test runs when ffmpeg is present), and the `flatten_overlaps` comment no longer overstates its
  complexity as O(n log n) *total* (active-set upkeep is O(n log n); stacking text is bounded by
  output size, quadratic in the pathological dense case, as `CONTRIBUTING.md` already noted).
- Documented honestly that `codex --sandbox read-only` still permits file *reads* (the throwaway
  cwd and denied network are the real limits) and that `antigravity` is the weakest backend, not
  recommended for material from an unknown source.
- The Ollama host is now validated: an explicit scheme is accepted only when it is `http`/`https`
  (`file://`, `ftp://`, etc. are rejected with a clear error), while a bare `host:port` still gets
  `http://`. Previously any string starting with `http` was passed through and other schemes were
  silently mangled into a request.
- Corrected two stale documentation claims: the README non-goal now says only *fuzzing the parser*
  is out of scope (the project does property-test its own extraction/reinsertion core), and
  `CONTRIBUTING.md` now states the `litellm` extra is smoke-imported in CI rather than not exercised.
- Multiline cues are no longer ambiguous in the translation prompt. A cue with an internal line
  break carries a real newline, which previously split the `[ID] Speaker: text` line into an
  unlabeled second physical line the model couldn't attribute to an id. The break is now serialized
  as the literal token `\n` (one physical line per cue) and decoded back to a real newline on the
  way out, so two-line cues survive translation intact.
- `review`'s line-length check now measures on-screen **display width** (`display_width`) instead
  of `len()`, so it agrees with `tighten` and no longer undercounts CJK/fullwidth text at one
  column per glyph.
- `analyze` now persists the episode context file **after** merging findings into series memory,
  not before. Previously a crash between the two left a context with a current `source_hash`, so a
  later `batch --pre-analyze` would treat the episode as already analyzed (`skip_if_current`) and
  never merge its characters/glossary into memory.

## [0.4.0] - 2026-06-30

### Fixed
- SRT output now keeps a cue's whole-line italic/underline (narration, songs, flashbacks) instead
  of flattening it to plain text — `flatten_overlaps` carries the emphasis through, computed from
  the final `\i`/`\u`/`\b` toggle state (so `{\i1}{\i0}` is correctly *not* italic), and the writer
  renders it as `<i>`/`<u>`. (Bold is carried but pysubs2's SRT writer doesn't emit `<b>`; partial
  inline emphasis can't survive, since the translation reorders words.)
- Output filenames no longer collide between variants of one language. `lang_code` keeps the
  region/script for multi-subtag targets, so `es-latam` → `<base>.es-latam.ass` and `es-ES` →
  `<base>.es-es.ass` (likewise `zh-Hans`/`zh-Hant`) instead of both producing `<base>.es.ass`. A
  bare language is unchanged (`es` → `<base>.es.ass`). **Note:** the default `es-latam` target now
  writes `<base>.es-latam.ass`.
- Multi-subtag language suffixes (`es-latam`, `pt-BR`, `zh-Hans`) are now recognized consistently
  across naming **and** sidecar resolution via one shared `is_lang_suffix` helper. Previously only
  the output filename kept the region: `base_stem` left the suffix on (so re-translating
  `ep.es-latam.srt` produced `ep.es-latam.fr-fr.ass`) and a `movie.es-latam.srt` sidecar was not
  detected.
- `doctor --provider ollama` no longer crashes on a 200 response with an unexpected JSON shape
  (`[]`, `{"models": null}`, `{"models": [null]}`); it warns instead.
- Readability metrics measure on-screen **display width** instead of `len()`: combining marks add
  no column and CJK/fullwidth glyphs count as two, so limits stay meaningful for
  Japanese/Chinese/accented subtitles.
- `review` reports no longer claim a provider/model when no LLM ran (deterministic-only or
  re-segmented-SRT runs now record `(none)`), and both `review` and `tighten` record the resolved
  model instead of `(default)`.

### Added
- `purge-cache` command: deletes the cache of subtitle tracks extracted from containers
  (`$XDG_CACHE_HOME/llm-subs/work`). Per-series memory, episode context and reports are not
  touched. Pass `--yes`/`-y` to skip the confirmation prompt.
- `doctor` now reports the installed `llm-subs` version, and `doctor --provider ollama --model X`
  verifies the model is actually installed on the server (not just that the server answers).
- The `tighten` readability report records the provider and model that produced the compactions,
  matching `review`'s provenance manifest.

### Security
- Internal state that may contain subtitle text — series memory
  (`memory.json`/`glossary.json`/`style_guide.json`/`conflicts.json`), `settings.json`, episode
  context, block checkpoints, file-handoff job files, and the `review`/`readability` reports — is
  now written **owner-only (0600)** instead of widened to the umask. The final translated subtitle
  keeps normal permissions so a media server (Jellyfin/Plex) under another account can read it.

### Changed
- CI now also runs the test suite on Python 3.14, exercises a real identity translation against the
  installed wheel (not just `--help`), and installs the package with `pip` plus the `litellm` extra
  to catch fresh-resolution and optional-dependency breakage.
- PyPI publishing migrated from a long-lived `PYPI_TOKEN` to Trusted Publishing (OIDC).
- Added a `CODE_OF_CONDUCT.md`, expanded `CONTRIBUTING.md` (architecture overview, how to add a
  provider, per-provider troubleshooting), issue and pull-request templates, and a documented
  versioning/compatibility policy.

## [0.3.0] - 2026-06-30

### Fixed
- `batch --out-dir` no longer overwrites same-named episodes from different folders. Each input's
  sub-directory relative to the batch root is now mirrored under `--out-dir`
  (`Season 1/Episode 01.mkv` → `<out>/Season 1/Episode 01.es.ass`), so
  `Season 1/Episode 01` and `Season 2/Episode 01` produce distinct files instead of colliding.
- `review --apply` now writes the report's `Translated fingerprint` from the file *after* the safe
  fixes are applied, so the provenance manifest matches what is on disk instead of being
  immediately stale.
- `tighten` gained a `--source` flag: when the translated file lives in a separate `--out-dir`,
  pass the original input so the readability report is keyed to the same episode directory as the
  checkpoint/context (which are keyed off the source) instead of a divergent one.

### Security
- `opencode` provider now denies **all** of opencode's built-in tools via an inline deny-all
  permission config (`OPENCODE_CONFIG_CONTENT`). Previously only `--pure` was passed, which merely
  disables external plugins and left built-in tools (read/bash/webfetch/websearch) allowed — so a
  crafted subtitle cue could have made the agent read absolute paths (e.g. `~/.ssh`) and exfiltrate
  them. Translation needs no tools, so denying everything closes the hole and gives opencode
  containment comparable to `claude --disallowedTools`.

### Added
- `translate` and `batch` accept `--parallel <n>` to set how many blocks translate concurrently
  (default `4` for the `ollama`/`litellm` APIs, `1` for the agent CLIs); lower it to avoid
  saturating a local Ollama server.
- `translate` and `batch` accept `--timeout <seconds>` to bound each provider call (default
  `600`).

### Changed
- Default style-guide tone is now the neutral `natural` instead of `anime-natural`, since the tool
  translates films and non-anime series too; an anime register can still be set per project. The
  character-dedup prompt no longer assumes the series is anime.
- `analyze`, `review`, `tighten` and `batch` now report a malformed project `settings.json` as a
  short error and exit non-zero, instead of leaking a raw traceback (the settings load happened
  before each command's error handling).
- `pipeline.py`, documented as the stable public API, now type-checks cleanly under
  `mypy --strict` (the rest of the package stays under the project's standard mypy config).
- Documented that image-based subtitle tracks (PGS/VobSub) are unsupported and require external
  OCR, in the README's known-limitations section.

## [0.2.8] - 2026-06-29

### Fixed
- Parallel translation (`--parallel > 1`) now cancels blocks that have not yet started when a
  block fails or the run is interrupted (Ctrl-C), instead of draining the thread pool and
  spending further provider calls. Completed blocks remain in the checkpoint and resume on the
  next run.

### Added
- Shipped a `py.typed` marker so type checkers honour the package's inline type hints when
  `llm-subs` is installed as a dependency (`pipeline.py`/`cli.py` are documented as a stable API).

### Changed
- Documentation: a web UI is now stated consistently as a deliberate non-goal (a focused CLI),
  removing the earlier contradiction between the scope and roadmap sections.

## [0.2.7] - 2026-06-29

### Fixed
- `validate_target` now rejects malformed language tags with leading, trailing or consecutive
  hyphens (`-es`, `es-`, `es--latam`) instead of accepting them, and returns the normalised tag
  (whitespace trimmed, underscores folded to hyphens). The normalised value is now propagated by
  `translate`, `review`, `tighten` and `ProjectSettings`, so a target like `es_latam` is stored
  and used as `es-latam` rather than steering memory/output to an inconsistent directory.
- `merge_alias` removes the alias character case-insensitively and rewrites other characters'
  relationship keys using the stored canonical name's casing, so an alias differing only in case
  (`ALICE` vs `Alice Chambers`) is merged correctly instead of left behind. Merging a name into
  itself (`Alice`/`alice` resolving to the same character) is now rejected rather than deleting
  the character.
- Readability and review report fingerprints now include each cue's start/end timing, so a
  change that only shifts timing (which alters chars-per-second and the report) no longer keeps a
  stale fingerprint.
- The `--provider` help for `analyze`, `review`, `tighten`, `config` and `compact-memory` now
  lists `ollama` and `litellm` alongside the agent CLIs, matching the providers those commands
  actually accept.
- Documented that `analyze`, `review` and `tighten` resolve their shared options from per-project
  `settings.json` defaults, correcting the README claim that they only take options explicitly.

## [0.2.6] - 2026-06-28

### Fixed
- `llm-subs --version` and the backwards-compatible `translate-subs --version` alias now report
  the installed `llm-subs` distribution version instead of `0.0.0+source`.
- CI and release smoke tests now install the built wheel and require both command aliases to
  report the exact version declared in `pyproject.toml`; `uv lock --check` also prevents a stale
  root-package version from reaching a release.
- Runtime and documentation naming now consistently use `llm-subs`. New installations use the
  `llm-subs` XDG data/cache directories and `$LLM_SUBS_HOME`, while the old
  `translate-subs` directories, command alias and `$TRANSLATE_SUBS_HOME` remain supported for
  backwards compatibility.
- Restored the missing 0.2.4/0.2.5 changelog history and corrected comparison links after the
  repository rename.

## [0.2.5] - 2026-06-28

### Changed
- Renamed the PyPI distribution and canonical command to `llm-subs`, retained `translate-subs`
  as a backwards-compatible command alias, and updated repository/install documentation.
- Bumped the distribution from 0.2.4 to 0.2.5 because PyPI package filenames are immutable.

## [0.2.4] - 2026-06-28

### Added
- Tagged releases now publish the built wheel and source distribution to PyPI in addition to
  attaching them to the GitHub Release.

### Fixed
- `review` now pairs source and target lines by `unit.event_index` instead of sequential
  position, so non-translatable ASS events (drawings, comments) preserved verbatim in the
  output no longer shift the pairing. For SRT targets (where `prune_to_units` and
  `flatten_overlaps` have removed those events) the pairing falls back to sequential
  position. Comment events with visible text are no longer falsely reported as `extra_event`;
  drawing events with non-empty path commands are also excluded from that check.
- `review` on an SRT target that has been re-segmented by `flatten_overlaps` now skips the
  LLM linguistic pass and surfaces a `srt_resegmented` structural finding instead. The guard
  triggers on both a cue-count mismatch and on timestamp mismatches in the sequential
  pairing, so it catches the case where `flatten_overlaps` produces the same number of cues
  but with shifted boundaries. Running the LLM on misaligned pairs would produce meaningless
  findings; the report advises reviewing the `.ass` output for precise analysis.
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
- `tighten --apply` no longer measures or rewrites ASS drawing events and comment events.
  The readability loop now filters with `is_translatable`, so path commands inside `{\p1}`
  blocks and staff annotation comments are never sent to the LLM or written back.
- An explicit `--track` flag now bypasses sidecar discovery entirely. Previously a
  language-matching sidecar would be returned before even probing the container, silently
  ignoring the user's choice of embedded track.
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

[Unreleased]: https://github.com/azratul/llm-subs/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/azratul/llm-subs/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/azratul/llm-subs/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/azratul/llm-subs/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/azratul/llm-subs/compare/v0.2.8...v0.3.0
[0.2.8]: https://github.com/azratul/llm-subs/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/azratul/llm-subs/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/azratul/llm-subs/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/azratul/llm-subs/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/azratul/llm-subs/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/azratul/llm-subs/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/azratul/llm-subs/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/azratul/llm-subs/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/azratul/llm-subs/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/azratul/llm-subs/releases/tag/v0.1.0
