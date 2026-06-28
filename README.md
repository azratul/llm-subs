# llm-subs

[![CI](https://github.com/azratul/llm-subs/actions/workflows/ci.yml/badge.svg)](https://github.com/azratul/llm-subs/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/azratul/llm-subs?sort=semver)](https://github.com/azratul/llm-subs/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

Contextual subtitle translator **from any language to any language** using an LLM through its
CLI (`claude`, `codex`, `antigravity`, `opencode`) or a model API (`ollama`, `litellm`). Unlike
line-by-line machine translation, it leverages **context**: character gender,
formality/register, relationships, per-series glossary, and tone.

- **Input:** any format `pysubs2` reads (`.ass`/`.srt`/`.sub`/…) or a video container (`.mkv`/…)
  whose embedded subtitle track is demuxed.
- **Output:** `<base>.<lang>.<format>` (`<lang>` derived from `--target`). The default format is
  **`.ass`**; with `--format srt` it exports `.srt`. See [Output format](#output-format-ass-vs-srt).
- **Design invariant:** the raw file is never sent to the LLM. It is parsed, only the *visible
  text* is extracted with stable IDs (`0001`, `0002`, …), and the model only sees
  `[ID] Speaker: text`, returning the same IDs. On reinsertion the **whole-line leading override
  block** (`{\an8\pos(..)\c..}`) is restored, so in `.ass` position/color/scale/fade match the
  original; the event's **style** (alignment, color, font) is kept too. Inside-text tags and
  karaoke are dropped. `.srt` has no positioning, so only basic italic/bold survives.

In normal use, you point it at one movie/episode and get a translated subtitle next to the
original file, ready for your player to pick up. For a series, you can optionally build a small
per-series memory so names, genders, formality and recurring terms stay consistent across
episodes.

## Requirements

- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).
- `ffmpeg`/`ffprobe` (for embedded tracks inside containers).
- At least one backend to actually translate: an agent CLI (`claude`, `codex`, `antigravity`,
  `opencode`) installed and authenticated, a local [Ollama](https://ollama.com) server
  (`--provider ollama --model qwen3:4b`, host from `$OLLAMA_HOST`), or [LiteLLM]
  (https://docs.litellm.ai) (`uv sync --extra litellm`, then `--provider litellm --model
  ollama/qwen3:4b`). (The `identity` provider does not translate: it copies the text and is used
  to verify the round-trip.)

## Installation

### As a global command (recommended for use)

Install it once and run `llm-subs` from any directory (it does not need a checkout):

```bash
# From PyPI:
uv tool install llm-subs   # or: pipx install llm-subs

# From a clone:
uv tool install .

# Directly from GitHub (no clone):
uv tool install "git+https://github.com/azratul/llm-subs"

llm-subs --help
```

`ffmpeg`/`ffprobe` are **system** dependencies (not installed by pip); install them with your
package manager. For `--provider litellm`, install the extra with
`uv tool install "llm-subs[litellm]"` (or `uv tool install ".[litellm]"` from a clone).

Per-series memory and other state are stored under the standard user data directory
(`$XDG_DATA_HOME/llm-subs`, i.e. `~/.local/share/llm-subs` on Linux), and extracted
tracks under `$XDG_CACHE_HOME/llm-subs`. Override the whole data root with `$LLM_SUBS_HOME`
(e.g. `export LLM_SUBS_HOME=/path/to/data`). The former `$TRANSLATE_SUBS_HOME` variable and
existing `translate-subs` XDG directories remain supported for backwards compatibility; when
both old and new directories exist, `llm-subs` takes precedence. Translated subtitles are written
**next to the input file** by default (`--out-dir`/`--output` to change that), not under the data
dir.

### From a tagged release

Each GitHub release attaches a built wheel and sdist. To install a specific version without a
clone (replace `vX.Y.Z` with the release tag):

```bash
# Pin a release tag straight from the repository:
uv tool install "git+https://github.com/azratul/llm-subs@vX.Y.Z"
# or from the attached wheel:
pipx install "https://github.com/azratul/llm-subs/releases/download/vX.Y.Z/llm_subs-X.Y.Z-py3-none-any.whl"
```

### For development (from a checkout)

```bash
uv sync                       # install deps into the project venv
uv run llm-subs --help  # run via uv without a global install
```

If the entry point fails with `No module named 'translate_subs'`, the editable install is stale:
`uv sync --reinstall-package llm-subs`.

### Build distributable artifacts

```bash
uv build   # writes a wheel and sdist to dist/
```

## Privacy and cost

The tool sends the **visible text** of your subtitles to the backend you pick with
`--provider`. What that means for privacy and cost:

- **Remote backends** — the agent CLIs (`claude`, `codex`, `antigravity`, `opencode`) and `litellm`
  pointed at a hosted model — transmit that text to a third party, subject to that provider's
  retention and pricing. Cost is typically per token, or covered by your CLI subscription.
- **Local backends** — `ollama` (and `litellm` pointed at a local model) — keep everything on
  your machine at no per-use cost.

For sensitive material, or to avoid per-token billing, use a local backend. See
[SECURITY.md](SECURITY.md) for the threat model, including prompt-injection notes when routing
untrusted subtitles through an agent CLI that has tool access.

## Quick start

If you installed the tool globally, use `llm-subs`. If you are running from a checkout, use
`uv run llm-subs` in the examples below.

### 1. Check your setup

Run `doctor` first. It tells you whether Python, `ffmpeg`/`ffprobe`, the data directories and
optionally your translation backend are ready.

```bash
llm-subs doctor
llm-subs doctor --provider ollama      # or claude / codex / litellm / ...
```

### 2. Pick a backend

For personal use there are two common paths:

- **Local and private:** run an Ollama model on your machine. No per-use API cost, and the subtitle
  text stays local.
- **Remote and usually stronger:** use an authenticated agent CLI such as `claude` or `codex`.
  This can give better quality, but the visible subtitle text is sent to that provider.

`identity` is only for testing the round-trip; it copies the source text without translating.

### 3. Translate one movie or episode

For a sidecar subtitle file:

```bash
llm-subs translate "/media/Movies/Movie.en.srt" \
  --provider ollama --model qwen3:4b \
  --lang en --target es-latam \
  --non-interactive
```

For a video file with embedded subtitles, first list the tracks if you are unsure which one to use:

```bash
llm-subs probe "/media/TV Shows/Show/Season 1/Episode 01.mkv"
```

Then translate it:

```bash
llm-subs translate "/media/TV Shows/Show/Season 1/Episode 01.mkv" \
  --provider ollama --model qwen3:4b \
  --lang en --target es-latam \
  --non-interactive
```

By default the result is written next to the input as `.ass`, for example:

```text
/media/TV Shows/Show/Season 1/Episode 01.es.ass
```

Use `--format srt` if you specifically want `.srt`, or `--out-dir` / `--output` to place the file
somewhere else.

### Better series consistency: analyze and translate with memory

For one-off movies, `translate` is enough. For a series, run `analyze` before `translate` and keep
the same `--project` name. The analysis creates or updates memory for character names, gender,
relationships, recurring terms and tone.

```bash
# 1) Analyze the episode and create/update the series memory
llm-subs analyze "/media/TV Shows/Show/Season 1/Episode 01.mkv" \
  --provider claude \
  --lang en --target es-latam \
  --project "Show" \
  --non-interactive

# 2) Translate using the episode context and accumulated memory
llm-subs translate "/media/TV Shows/Show/Season 1/Episode 01.mkv" \
  --provider ollama --model qwen3:4b \
  --lang en --target es-latam \
  --project "Show" \
  --non-interactive
```

For the following episodes, repeat `analyze → translate` keeping
`--project "Show"`. Each analysis updates the memory and each translation uses what has been
learned so far. A practical setup is to use a stronger remote model for `analyze` / `review`, and
a cheaper local model for the high-volume `translate` step.

`analyze` stores a fingerprint of the analyzed subtitle in `episode.context.json`. If you later
`translate` or `review` against a changed version of that subtitle, the tool **warns** that the
context sheet may be stale (re-run `analyze` to refresh it) — it never blocks, and older context
files without the fingerprint are left alone.

### Review and readability (optional)

```bash
# Review and apply only safe fixes (confirmed gender, glossary, names…)
llm-subs review "$EP" "$OUT" --provider claude \
  --project "Your project" --apply --non-interactive

# Readability control: compact lines that exceed the on-screen limits
llm-subs tighten "$OUT" --provider claude \
  --project "Your project" --apply

# Validate the final file
llm-subs validate "$OUT"
```

`analyze`, `translate`, `review`, and `tighten` let you pick the CLI with `--provider`/`--model`;
`--reasoning` applies to Codex. Transient backend/protocol failures retry twice by default with
backoff, jitter and `Retry-After` support; permanent authentication/configuration failures stop
immediately (`--retries 0` disables retries).

## Any language → any language

`--lang` is the **source** language (label and track selection); `--target` is the **destination**
(it drives the rules, the prompt, and the file name).

```bash
# Japanese -> English   => ep.en.ass
llm-subs translate ep.mkv --provider ollama --model qwen3:4b --lang ja --target en

# English -> French     => ep.fr.ass
llm-subs translate ep.en.srt --provider ollama --model qwen3:4b --lang en --target fr-FR
```

## Output format (`.ass` vs `.srt`)

`--format` controls the output container (and the file extension):

- **`ass` (default):** keeps **style-level** positioning. This matters when the original subtitle
  shows **two simultaneous texts in different positions** — e.g. a translator note on top and the
  dialogue at the bottom, both with the same timestamp. In `.ass` each one keeps its style
  (alignment/color/font) and they display without colliding.
- **`srt`:** flat, universal format, with no positioning. To avoid losing those cases,
  **overlapping events are merged**: the timeline is split at every cue boundary and each interval
  becomes a single subtitle that **stacks** the active texts (the top-aligned text comes first).
  This way two simultaneous cues end up in a single two-line subtitle instead of colliding (most
  players, faced with two cues at the same time, show only one).

When the translated text is reinserted, the **whole-line leading override block** (e.g.
`{\an8\pos(..)\c&H..&}` at the very start of an event) is preserved, so on an `.ass` export the
line keeps its original position, colour, scale and fade. Tags that sit *inside* the text are
tied to the original wording and are dropped, as is karaoke (`\k`, per-syllable). `.ass` also
preserves the event's style (alignment/colour/font). On a flat `.srt` the writer strips
positioning anyway, so only basic italic/bold survive.

```bash
# .srt output (instead of the default .ass)
llm-subs translate "$EP" --provider ollama --model qwen3:4b \
  --lang en --target es-latam --format srt --non-interactive
```

## Commands

| Command | What it does |
|---|---|
| `probe <media>` | Lists the embedded subtitle tracks of a container. |
| `translate <input>` | Translates and exports `<base>.<lang>.<format>` (`.ass` by default, `.srt` with `--format srt`). |
| `batch <directory>` | Translates every matching file in a directory (`--glob`, default `*.mkv`; `-r` recurses), skipping done episodes and continuing past per-episode failures. Pass `--pre-analyze` to analyze all episodes first and build full series memory before translating any of them. |
| `config <project>` | Shows or sets per-project default options (provider, model, target, lang, format, reasoning) in `settings.json`. |
| `analyze <input>` | Generates `episode.context.json` and updates the series memory. |
| `review <source> <translated>` | Quality review → `episode.review.md` (with `--apply`, applies the safe fixes). |
| `tighten <translated>` | Flags and compacts subtitles that break the readability limits. |
| `update-memory <input>` | Re-merges an existing `episode.context.json` into the memory (no LLM call). |
| `compact-memory <project>` | Prunes redundant memory (identity glossary terms, duplicate/info-less characters). |
| `resolve-conflicts <project>` | Walks flagged `conflicts.json` entries interactively (keep stored / use suggested / skip). |
| `validate <subtitle>` | Structural validation (parseable, timings, no leftover markup). |
| `doctor [--provider <name>]` | Checks the environment: media tools (ffprobe/ffmpeg), writable data/cache dirs, and — with `--provider` — that provider's backend (CLI on PATH, reachable Ollama server, or installed litellm). |

### Providers (`--provider`)

`identity` (passthrough, no translation) · `file-handoff` (writes the job protocol to fill in by
hand) · `claude` · `codex` · `antigravity` · `opencode` · `ollama` (local server) · `litellm` (router
SDK).

- `ollama` POSTs to `$OLLAMA_HOST` (default `http://localhost:11434`) `/api/chat` with
  `format=json` and thinking **off** (translation isn't reasoning-heavy, and a thinking model
  like qwen3 is far slower with it on; non-thinking models ignore the flag). It suits
  cheap/local models for the high-volume `translate` step (keep a strong model on
  `analyze`/`review`). `litellm` routes to any backend via its SDK with the provider
  prefix in `--model` (e.g. `ollama/qwen3:4b`, `gpt-4o-mini`); install it with
  `uv sync --extra litellm`.
- `--model <id>` sets the provider's model (e.g. `--model qwen3:4b` for ollama, or a model name
  supported by your agent CLI/API provider). For `ollama`/`litellm` it is required.
- `--reasoning <minimal|low|medium|high|xhigh>` tunes the reasoning effort of **codex**
  (default `low`: translating doesn't need `xhigh`, which is slower and costlier).
- `--retries <n>` controls retries on agent failures, invalid JSON, or wrong IDs (default `2`).

### Cross-cutting flags

`--non-interactive` / `--yes` / `-y` · `--lang` (source language) · `--target` (target language) ·
`--on-conflict {ask,keep,overwrite,flag}` · `--project` (series name). `translate` also takes
`--format {ass,srt}` (default `ass`), `--strict-lang` (refuse a different-language subtitle),
`--fail-on-untranslated` (exit non-zero if any line kept the source text — useful in batch
scripts; the file is still written so you can inspect it), and `--no-resume` (see below).

### Resume, caching and progress

Translating a full episode is dozens of slow LLM calls. Each block's result is checkpointed to
`<project>/<target>/<episode>/translations.checkpoint.json` as soon as it returns, keyed by a hash of
everything that steers that block — target, rules, its lines **and the before/after context lines
sent with it**. So:

- **Resume:** if a run crashes (or you Ctrl-C it) on block 38 of 40, rerunning picks up from the
  checkpoint and only translates what's left — the finished blocks are reused.
- **Cache:** if you re-translate after editing a few lines, only the affected blocks are redone;
  the rest are reused verbatim. Because the context is part of the key, editing a line also
  re-translates the neighbouring blocks that saw it as context, so a stale-context translation is
  never reused.
- The checkpoint is scoped to the `provider|model` signature, so switching backend (e.g. from a
  local Ollama model to `claude`) re-translates rather than inheriting the old output.
- The translation prompt has an explicit version included in each block hash, so prompt changes
  invalidate stale cached translations even when the subtitle text itself is unchanged.
- `--no-resume` ignores any saved checkpoint and re-translates every block from scratch.

On a terminal, `translate` shows a live progress bar (current block, count and ETA); in a pipe
or CI log it stays quiet. Only the CLI/API providers are checkpointed — `identity` is instant and
`file-handoff` writes all its jobs up front.

### Translating a whole season

`batch` runs `translate` over every matching file in a directory, sharing one `--project`:

```bash
llm-subs batch "TV Shows/Show/Season 1" --project "Show" --target es-latam --provider claude
llm-subs batch . --glob '*.mkv' --glob '*.mp4' -r          # several patterns, recurse
```

It selects files with `--glob` (default `*.mkv`, repeatable; `-r`/`--recursive` descends into
subdirectories) and skips any file that already looks like one of its own outputs. Each episode
is independent: one whose output already exists is **skipped** (pass `--force` to redo it), and one
that errors is **failed** and the run moves on — a single bad episode never aborts the season. A
summary table reports translated/skipped/failed, and the command exits non-zero if any episode
failed (or, with `--fail-on-untranslated`, if any line was left untranslated). Because each
episode still checkpoints per block, interrupting a season and rerunning resumes mid-episode.

#### Building series memory before translating

For the best translation quality, run `--pre-analyze` to analyze every episode first and build
the shared project memory (characters, genders, glossary, style guide) before any translation
begins:

```bash
llm-subs batch "TV Shows/Show/Season 1" --project "Show" --target es-latam \
  --provider claude --pre-analyze
```

Without `--pre-analyze`, each episode is translated with only the memory accumulated from the
episodes that came before it in the batch — episode 20 benefits from episodes 1–19, but episode
1 has nothing. With `--pre-analyze`, all episodes contribute to the shared memory first, so
every episode (including the first) benefits from the full series context. The command runs in
two clearly labeled phases:

```
Phase 1/2: Analyzing episodes…
[Analyze 1/24] Episode 01.mkv
[Analyze 2/24] Episode 02.mkv
…
Analyzed 24 episode(s).
Phase 2/2: Translating episodes…
[1/24] Episode 01.mkv
…
```

A failed analysis is noted and skipped (translation still proceeds with whatever memory was
built). For very long series, analyzing just the first 5–10 episodes is usually enough to
capture the main cast and glossary; run the analysis manually with `analyze` for those episodes,
then use `batch` without `--pre-analyze` for the rest.

### Per-project defaults

Instead of repeating `--provider`, `--model`, `--target`, etc. for every episode of a series, pin
them once with `config`:

```bash
llm-subs config "Show" --provider ollama --model qwen3:4b --target es-latam
llm-subs config "Show"                       # show current defaults
llm-subs config "Show" --unset model         # clear a field back to the built-in default
```

These are stored in `<project>/settings.json` (next to the memory files; hand-editable too) and
used by `translate` and `batch` as defaults: an explicit flag always wins, then the project
setting, then the tool's built-in default. The auxiliary commands (`analyze`, `review`,
`tighten`) still take their options explicitly.

### What `--project` actually does

`--project "Series Name"` identifies the memory shared across episodes. It is not an input
directory, it does not discover files, and it does not trigger analysis automatically.

- `analyze --project ...` creates or updates `data/projects/<series>/<target>/`, the
  episode's context card, the character memory, and the glossary.
- `translate --project ...` only loads that memory and context if they already exist.
- `review --project ...` uses the same information to review the translation.

Therefore, running only `translate --project "Series"` produces the output file, but does not
create `data/projects/` or accumulate memory. To get contextual translation across episodes you
must first run `analyze` for each episode, or use `batch --pre-analyze` to do both phases in one
command:

```bash
# Single episode: analyze first, then translate
llm-subs analyze episode.mkv \
  --provider claude --project "Series" --non-interactive

llm-subs translate episode.mkv \
  --provider ollama --model qwen3:4b --project "Series" --non-interactive

# Whole season: analyze all episodes first, then translate all (recommended)
llm-subs batch "Season 1/" --project "Series" \
  --provider ollama --model qwen3:4b --pre-analyze --non-interactive
```

## Memory and conflicts

The memory created by `analyze` lives under `data/projects/<series>/<target>/` (segmented by the
full target — `es-latam` and `es-ES` are kept apart — so a glossary built for one never steers
another). When `--project` is omitted the series name is taken from the source's folder, skipping a
season/specials subfolder (`Season 1`, `S02`, `Specials`) in favour of the series folder above it:

- `memory.json` — characters (name, gender, style, relationships).
- `glossary.json` — fixed terms (organizations, places, techniques, titles…).
- `style_guide.json` — locale/variant, honorifics, tone, formality policy.
- `conflicts.json` — conflicts flagged for manual review.

`compact-memory`, `resolve-conflicts` and `update-memory` take `--target` to choose which
language's memory to act on (default `es-latam`). Per-episode state (context, checkpoint, reports)
lives in a subdirectory keyed by the source file's name **and** a short hash of its folder, so two
same-named episodes in different season folders never share context or checkpoints.

These persisted files use strict, versioned schemas. Legacy unversioned files are still read for
compatibility; malformed files fail at load time with the offending path instead of surfacing
later as an unrelated runtime error.

A new suggestion **never silently overwrites** a stored discrete decision (confirmed gender or
glossary rendering). `--on-conflict` decides: `flag` (non-interactive default: keep and record),
`keep`, `overwrite`, or `ask` (interactive default: prompt). Relationship descriptions are free
text rather than discrete decisions, so the most informative description is kept. Series
decisions take **precedence** over episode ones when translating.

### Token efficiency

Memory only grows as a series is analyzed, so dumping it whole into every prompt would make token
cost climb with every episode. To avoid that, `translate` injects the memory **per block,
filtered by relevance**: each block only carries the glossary terms and characters that its own
lines mention (identity mappings `term -> term` are dropped entirely). Cost stays bounded by
episode content instead of series history — on a real run this cut the rules payload by ~94%.

`compact-memory <project>` is the housekeeping companion: it removes identity/duplicate glossary
terms and duplicate or info-less characters from `data/projects/<series>/`. Run it whenever the
memory has accumulated noise:

```bash
llm-subs compact-memory "Your Project"
```

Contradicting suggestions are recorded in `conflicts.json` rather than silently overwriting a
stored decision. Only **discrete** decisions are conflict-eligible — glossary renderings (compared
with whitespace/case/trailing punctuation folded, so trivial differences are ignored) and
confirmed gender; relationship descriptions are free text and never flagged. To clear the backlog,
`resolve-conflicts <project>` walks each flagged conflict and asks whether to keep the stored value,
use the suggested one, or skip it (leaving it in the log):

```bash
llm-subs resolve-conflicts "Your Project"
```

You also don't need to `analyze` every episode: the cast and glossary stabilize after the first
several, and `translate` keeps using the accumulated memory regardless. Analyzing a subset and
translating the rest saves the per-episode analysis transcripts.

## Readability

Recommended limits (configurable in `tighten`): **42** characters per line, **2** lines,
**18** characters per second. Lines that exceed them receive an LLM compaction pass bounded by
the per-second character budget of each subtitle's duration. Compaction replies must map every
requested ID to a non-empty string; arrays, objects and other non-text values are rejected.

The review report also checks source/target structure before linguistic quality: missing or extra
events, duplicate internal IDs, timestamp/order mismatches and relevant ASS style differences are
reported explicitly. Automatic fixes remain disabled unless the files map safely by position and
timing.

## Development

```bash
uv run pytest -q                                                   # the whole suite
uv run pytest tests/test_round_trip.py::test_blocks_have_context   # a single test
```

This README is authoritative for current behaviour. Translation is decoupled behind a provider
abstraction: the deterministic core (parsing/reinsertion/validation) is never mixed with the
quirks of each CLI.

`translate_subs/pipeline.py` and `translate_subs/cli.py` are stable compatibility facades.
Application logic is grouped by use case under `translate_subs/workflows/`, while Typer callbacks
live under `translate_subs/commands/`. Existing imports, command names, options and output remain
on the facades so integrations do not depend on the internal module layout.

## Scope, non-goals and limitations

`llm-subs` is a focused, single-user command-line tool. The items below are **deliberate
decisions**, documented here so they are not repeatedly filed as defects. They are choices about
scope, not oversights.

### Positioning: a focused community CLI, not a production platform

This project is deliberately built as a **focused community CLI** that you install and run on your
own machine — not as a hosted product that third parties depend on in production. That choice is
what makes the items below *out of scope rather than missing*: each is only worth its ongoing
maintenance cost once other people's production systems depend on this tool, which is explicitly
**not** the goal.

Concretely, the recurring "to reach 100/100" asks — an automated linguistic-quality benchmark,
heavyweight supply-chain tooling (SBOM, signing, provenance, CodeQL, Dependabot), multi-process
locking, and full agent sandboxing — are excluded for the same underlying reason:

- **No third party depends on this in production.** Locking solves multi-writer races you don't
  have when one person runs one project at a time; supply-chain attestation and signing matter
  when others pull your artifacts into their builds, not for a `pipx install git+…`.
- **The high-value alternative is cheaper and already present.** Quality is guarded by the
  `review`/`tighten` passes, the deterministic contract and human reading, rather than a
  research-grade eval harness (a serious golden corpus with blind human evaluation is a project in
  itself). Untrusted-input risk is mitigated by *using a local inference backend* (`ollama`) and
  not granting agents tool access — see [SECURITY.md](SECURITY.md) — rather than by building a
  sandbox around CLIs you installed yourself.
- **Maintenance has a real cost.** Each of these is continuous upkeep (corpus curation, release
  signing infra, lock contention handling) that would slow the parts that actually improve the
  tool for its users — see *Planned, not yet implemented* below for where effort goes instead.

Adopting these would mean re-scoping the project from "focused CLI" to "production platform"; that
is a different product with a much higher maintenance floor, and it is not the direction here. The
specific items follow.

### Out of scope by design

- **Heavyweight release / supply-chain tooling.** The repo ships a `CHANGELOG`, `CONTRIBUTING`,
  a `SECURITY` threat model, a CI that builds and smoke-tests the wheel, and GitHub Actions
  pinned by commit SHA. It does **not** add SBOM, provenance attestation, artifact signing,
  CodeQL, Dependabot/Renovate or fully automated SemVer release pipelines — that machinery is
  maintenance overhead with no benefit for a community CLI distributed via a tagged release and
  `pipx install git+…`.
- **An automated translation-quality benchmark / "golden" corpus.** This is repeatedly raised as
  the headline gap, so to be explicit: there is **no** golden-corpus, blind human-eval harness or
  per-provider/prompt linguistic-regression suite, and there won't be — a serious one is a
  research project of its own. Quality is guarded by the `review` and `tighten` passes, by the
  deterministic contract (stable IDs, ID/timestamp validation, glossary/gender consistency
  checks) and by human reading. Prompt changes are reviewed by their effect on real episodes, not
  by an automated score.
- **A hard coverage threshold in CI.** Coverage is measured and reported, not gated. A strict 90–95%
  gate tends to incentivise filler tests that just exercise framework glue code — testing whether
  Typer prints the right error when a file is missing, for instance. Effort goes to tests that
  exercise real deterministic behaviour (extraction, reinsertion) instead of chasing a number.
- **Fuzz / property-based testing of ASS/SRT.** Parsing is delegated to `pysubs2`; the project
  tests its own extraction/reinsertion logic, not the parser's robustness to malformed input.
- **OS-level sandboxing of agent CLIs.** Each agent CLI is invoked with its own built-in
  restriction so untrusted subtitle text can't talk it into touching your files: `codex` runs
  `--sandbox read-only`, `claude` denies every filesystem/exec/network/subagent tool and ignores
  all MCP servers (`--strict-mcp-config`), `antigravity` (`agy`) runs `--print --sandbox`, and
  `opencode` runs `--pure` (no external plugins); none is ever given `--dangerously-skip-permissions`.
  On top of those flags, each agent CLI is launched from an empty throwaway working directory, so a
  crafted subtitle can't steer it toward files in your real cwd. Caveat: `antigravity` replaced the
  Gemini CLI and is agentic — its `--sandbox` only restricts the terminal (no read-only/no-tools
  mode like the old `--approval-mode plan`), so the throwaway cwd is its primary containment.
  Full OS isolation (containers/seccomp) is still out of scope; the strongest mitigation — use a
  local backend (`ollama`) for sensitive subtitles — is documented in [SECURITY.md](SECURITY.md).
- **Token-aware block sizing and map-reduce analysis.** Blocks are sized by line count. Subtitle
  lines are inherently short (it's a subtitle), so a fixed line budget rarely strains a model's
  context; a token-budget scheduler and hierarchical analysis for very long inputs are not
  warranted (the `analyze` cap with a notice covers the rare overflow).
- **Multi-writer coordination for memory.** The memory store assumes a single writer (one project
  processed at a time). There is no file locking or concurrent-update merging; atomic writes
  prevent *corruption*, not two processes racing on the same project.
- **Persistent TOML config, structured/JSON logging, and a web UI.** Configuration is via flags;
  output is human-readable text.

### Known limitations (accepted trade-offs)

- **`opencode` receives the prompt as a process argument** (visible in process listings, bounded
  by `ARG_MAX`). Translation blocks are small (a few KB), so this is accepted; the other CLI
  backends use stdin.
- **`flatten_overlaps` does not cap stacking.** It uses a sweep-line pass for the timeline, but a
  moment where many cues overlap will still produce a single cue with that many stacked lines.
  Only relevant to `--format srt`; the default `.ass` keeps cues positioned and is unaffected.
- **`analyze` caps the transcript** (currently 4000 lines) to bound prompt size; longer inputs
  are truncated and the command prints a notice saying how many trailing lines were dropped.
- **`review --apply` only writes fixes when the target maps 1:1 to the source.** That holds for
  the default `.ass` output. On a `.srt` whose overlapping cues were merged (counts differ),
  `--apply` is **automatically skipped with a notice** — the report is still written for reading —
  so a fix is never applied to the wrong cue. Re-run review against the `.ass` to apply fixes.
  Doing string surgery inside merged multi-line `.srt` cues to map an ID back to a portion of
  text is extremely fragile and risks silent data corruption. The `.ass` format is the lossless
  default and the correct choice when high-fidelity revision is needed — applied review fixes
  and readability compactions preserve whole-line leading ASS override tags.
- **`file-handoff` does not hash or version jobs.** It is a manual escape hatch; make sure you
  fill the matching `*.out.json`.

### Planned, not yet implemented

- Audio-assisted gender and a simple web UI.

## License

[GPL-3.0-or-later](LICENSE). You may use, modify, and redistribute it, but derivative works
that you distribute must also be released under the GPL and ship their source.
