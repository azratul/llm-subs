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
  karaoke are dropped. `.srt` has no positioning, so only whole-line italic/underline survive
  (pysubs2's SRT writer doesn't emit `<b>`, so bold is lost).

In normal use, you point it at one movie/episode and get a translated subtitle next to the
original file, ready for your player to pick up. For a series, you can optionally build a small
per-series memory so names, genders, formality and recurring terms stay consistent across
episodes.

## Requirements

- Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).
- `ffmpeg`/`ffprobe` (for embedded tracks inside containers).
- At least one backend to actually translate: an agent CLI (`claude`, `codex`, `antigravity`,
  `opencode`) installed and authenticated, a local [Ollama](https://ollama.com) server
  (`--provider ollama --model qwen3:4b`, host from `$OLLAMA_HOST`), or
  [LiteLLM](https://docs.litellm.ai) (`uv sync --extra litellm`, then `--provider litellm --model
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
package manager — both ship in the same package:

```bash
sudo apt install ffmpeg          # Debian/Ubuntu
sudo dnf install ffmpeg          # Fedora
sudo pacman -S ffmpeg            # Arch
brew install ffmpeg              # macOS (Homebrew)
winget install Gyan.FFmpeg       # Windows (or: choco install ffmpeg)
```

`llm-subs doctor` confirms both are found. For `--provider litellm`, install the extra with
`uv tool install "llm-subs[litellm]"` (or `uv tool install ".[litellm]"` from a clone).

Tab completion for bash/zsh/fish comes built in — install it once with
`llm-subs --install-completion` (restart the shell afterwards; `--show-completion` prints the
script to inspect or place manually).

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
  your machine at no per-use cost. Note "local" follows the server, not the provider name:
  `$OLLAMA_HOST` can point at a remote box, and then the text is sent there over HTTP
  (`doctor --provider ollama` flags a non-loopback host).

For sensitive material, or to avoid per-token billing, use a local backend. See
[SECURITY.md](SECURITY.md) for the threat model, including prompt-injection notes when routing
untrusted subtitles through an agent CLI that has tool access.

> **⚠️ For material from an untrusted source, prefer a local `ollama` model.** The agent CLIs are
> each launched with their own containment (denied tools, read-only sandbox, an empty throwaway
> working directory), but **`antigravity` is the weakest** of them: its `--sandbox` restricts only
> the terminal, not the agent's tools, so its sole guard is that throwaway cwd. `doctor --provider
> antigravity` and every command that runs the LLM (`translate`, `batch`, `analyze`, `review`,
> `tighten`, `compact-memory --provider`) warn about this at runtime. When in doubt, translate with
> `ollama` on the default local host — the subtitle text never leaves your machine and no agent
> tools are in reach.

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
`file-handoff` writes the translation jobs to disk for you (or an agent) to fill in by hand —
useful when no backend fits.

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
/media/TV Shows/Show/Season 1/Episode 01.es-latam.ass
```

The language code keeps the region/script of a variant (`es-latam` → `.es-latam.ass`,
`es-ES` → `.es-es.ass`), so two variants of one language never overwrite each other; a bare
language stays bare (`--target es` → `.es.ass`).

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
# Review and apply only safe fixes (confirmed gender, glossary, names…).
# --apply previews the diff and asks before writing; --non-interactive/--yes skips the prompt.
llm-subs review "$EP" "$OUT" --provider claude \
  --project "Your project" --apply --non-interactive

# Readability control: compact lines that exceed the on-screen limits.
# --apply previews the diff and asks before overwriting (add --yes to skip the prompt).
# (pass --source "$EP" when "$OUT" lives in a separate --out-dir, so the report is filed
# under the same episode directory as the checkpoint/context)
llm-subs tighten "$OUT" --provider claude \
  --project "Your project" --apply

# Validate the final file
llm-subs validate "$OUT"
```

`analyze`, `translate`, `review`, and `tighten` let you pick the CLI with `--provider`/`--model`;
`--reasoning` applies to Codex. Transient backend/protocol failures retry twice by default with
backoff, jitter and `Retry-After` support; permanent authentication/configuration failures stop
immediately (`--retries 0` disables retries).

## Recipes

Copy-paste starting points for the common cases. Adjust paths, `--provider`/`--model`, `--lang`
and `--target`.

### Local and private (Ollama)

Keep everything on your machine — no per-use cost, subtitle text never leaves the host. Verify the
model is installed, then translate:

```bash
llm-subs doctor --provider ollama --model qwen3:4b     # checks the server AND that the model exists
llm-subs translate "Movie.en.srt" --provider ollama --model qwen3:4b \
  --lang en --target es-latam --non-interactive
```

Point at a remote Ollama box with `OLLAMA_HOST=http://box:11434` (the server that produced an
output is recorded in the manifest) — then the subtitle text is of course sent to that box, and
`doctor --provider ollama` marks the non-local host so the "private" note above isn't overread. A good split for a series: a strong CLI (`claude`) for the
cheap `analyze`/`review` passes and a local model for the high-volume `translate`.

### Anime episode, keep positioning (.ass)

`.ass` is the default and the right choice for anime: it preserves style-level positioning, so a
top-aligned translator note and the bottom dialogue at the same timestamp don't collide. Japanese
audio with an embedded English track:

```bash
llm-subs probe "Episode 01.mkv"                        # find the English subtitle track
llm-subs translate "Episode 01.mkv" --track 2 \
  --provider ollama --model qwen3:4b \
  --lang en --target es-latam --non-interactive        # -> Episode 01.es-latam.ass
```

### Movie for a flat player (.srt)

When the target player has no `.ass` support, ask for `.srt` — overlapping cues are merged into
stacked lines and whole-line italics survive as `<i>`:

```bash
llm-subs translate "Movie.en.srt" --format srt \
  --provider ollama --model qwen3:4b \
  --lang en --target es-latam --non-interactive        # -> Movie.es-latam.srt
```

### A whole series, with consistent memory

Build series memory first (characters, gender, glossary, tone), then translate the season sharing
one `--project` so every episode benefits from it — best done with `batch --pre-analyze`, which
analyzes every episode before translating any:

```bash
llm-subs config "Show" --provider ollama --model qwen3:4b --target es-latam   # per-project defaults
llm-subs batch "Show/Season 1" --project "Show" --glob '*.mkv' -r --pre-analyze
```

See [Building series memory before translating](#building-series-memory-before-translating) for the
manual `analyze` → `translate` flow and how conflicts are handled.

### Recovering after a failure or an edit

Runs are resumable and outputs are protected:

```bash
# A crashed/Ctrl-C'd run: just re-run the same command — finished blocks are reused from the
# checkpoint, only the missing ones are re-translated. Add --no-resume to force a clean redo.
llm-subs batch "Show/Season 1" --project "Show" --glob '*.mkv' -r

# Get the per-episode results (translated/skipped/stale/modified/failed) as machine-readable JSON.
# Note: --json only changes the output format — the batch still translates and writes files.
llm-subs batch "Show/Season 1" --project "Show" --glob '*.mkv' -r --json

# You edited the source (re-timed, fixed a line): the affected outputs are reported stale and never
# silently overwritten. Re-render them (reusing cached translations where the text is unchanged):
llm-subs batch "Show/Season 1" --project "Show" --glob '*.mkv' -r --force
```

If you hand-edited a *translated* file, a later run refuses to clobber it (reported `modified`);
`--force` overrides. `review --apply`/`tighten --apply` are the sanctioned ways to change a
translated file in place.

## Any language → any language

`--lang` is the **source** language (label and track selection); `--target` is the **destination**
(it drives the rules, the prompt, and the file name).

```bash
# Japanese -> English   => ep.en.ass
llm-subs translate ep.mkv --provider ollama --model qwen3:4b --lang ja --target en

# English -> French     => ep.fr-fr.ass  (region kept so variants never collide)
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
positioning anyway, so only whole-line italic/underline survive (bold is dropped: pysubs2's SRT
writer doesn't emit `<b>`).

```bash
# .srt output (instead of the default .ass)
llm-subs translate "$EP" --provider ollama --model qwen3:4b \
  --lang en --target es-latam --format srt --non-interactive
```

## Input encoding

Real-world sidecars are not always UTF-8: Western `.srt` files are often **CP1252**, Japanese ones
**Shift-JIS**, and some tools emit **UTF-16**. The encoding is **auto-detected** — a BOM
(UTF-8/16/32) and strict UTF-8 are handled directly; for legacy files **`--lang` doubles as a
codepage hint** (`--lang en` prefers CP1252, `--lang pl` CP1250, `--lang ja` Shift-JIS, …), used
only when that codec decodes the bytes cleanly. Anything else is resolved statistically with
[charset-normalizer](https://github.com/jawah/charset_normalizer). The hint matters because
near-identical codepages (CP1250 vs CP1252 differ in a few positions like €/Ł) are beyond byte
statistics — your declared source language settles it. No configuration is needed for the common
cases.

When detection still guesses wrong, force the codec — the definitive override:

```bash
llm-subs translate movie.srt --encoding cp1252 --lang en --target es-latam
```

`--encoding` is available on `translate`, `batch`, `analyze`, `review`, `tighten` and `validate`
(an unknown codec name is reported as a short error). On `review` it applies to the **source only** —
the translated file is this tool's own UTF-8 output and is always auto-detected. Output is always
written as UTF-8.

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
| `project-status <project>` | Shows a project's stored state for a target: glossary/character/conflict counts, and per-episode whether it was analyzed, whether a checkpoint file is present, and which output paths are tracked (no LLM call). |
| `projects` | Lists every stored project with its targets and on-disk size — what `purge-project` would free (`--json` for scripts). |
| `purge-project <project>` | Deletes a project's stored state: memory, episode contexts, checkpoints, reports, settings. With `--target`, only that target's memory subtree; the whole project otherwise. Translated subtitles next to your media are never touched. |
| `validate <subtitle>` | Structural validation (parseable, timings, no leftover markup). |
| `doctor [--provider <name>] [--fix]` | Checks the environment: media tools (ffprobe/ffmpeg), writable data/cache dirs, owner-only state permissions, and — with `--provider` — that provider's backend: for a CLI, its path, its `--version`, and the model a run would actually use (the runner's built-in default when `--model` is omitted; codex/antigravity/opencode pick internally, which is said explicitly); for Ollama, a reachable server (plus a warning on a non-loopback host); for litellm, the installed package. `--fix` repairs what it can: state/cache files left group/other-readable by an older release are tightened to owner-only. |
| `purge-cache` | Deletes the cache of subtitle tracks extracted from containers (`$XDG_CACHE_HOME/llm-subs/work`). Series memory and reports are not touched. |

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
- `--parallel <n>` sets how many blocks translate concurrently (default `4` for the API providers
  `ollama`/`litellm`, `1` for the agent CLIs). Lower it to avoid saturating a local Ollama server.
- `--timeout <seconds>` bounds each provider call (default `600`). Raise it for slow local models
  on long blocks, or lower it to fail fast. Both `--parallel` and `--timeout` also work on `batch`.

### Cross-cutting flags

`--non-interactive` / `--yes` / `-y` · `--lang` (source language) · `--target` (target language) ·
`--encoding` (force the input text encoding; auto-detected otherwise) ·
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
that errors is **failed** and the run moves on — a single bad episode never aborts the season. That
applies to a **content/protocol** failure too (an unparseable model reply or wrong ids for that
episode): it is recorded `failed` and the run continues. A **systemic** failure, on the other hand —
auth/config, quota/rate-limit, or a service outage — aborts the whole run, since retrying every
remaining episode would just repeat it. If
an existing output is **stale** — its source, provider/model or prompt changed since it was written
— it is reported as such (a warning, not an error) rather than skipped, so you know to rerun it with
`--force`; the existing file is never overwritten on its own. An output you **edited by hand** since
it was generated is reported **modified** and likewise never overwritten without `--force`, so your
manual corrections are safe (edits made by the tool's own `review --apply`/`tighten --apply` don't
count — those re-bless the manifest, so only outside edits are flagged). A summary table reports
translated/skipped/stale/modified/failed, and
the command exits non-zero if any episode failed (or, with `--fail-on-untranslated`, if any line was
left untranslated; or, with `--fail-on-stale`, if any output was flagged stale). Pass `--json` for
a machine-readable summary instead of the table (also on `doctor`, `validate` and `project-status`).
Because each episode still checkpoints per block, interrupting a season and rerunning resumes
mid-episode.

With `--out-dir`, each input's sub-directory relative to the batch root is mirrored under it
(`Season 1/Episode 01.mkv` → `<out-dir>/Season 1/Episode 01.es-latam.ass`), so two same-named
episodes in different season folders never collapse onto one output filename and overwrite each
other.

#### Building series memory before translating

For the best translation quality, run `--pre-analyze` to analyze every episode first and build
the shared project memory (characters, genders, glossary, style guide) before any translation
begins:

```bash
llm-subs batch "TV Shows/Show/Season 1" --project "Show" --target es-latam \
  --provider claude --pre-analyze
```

Without `--pre-analyze`, translation uses whatever series memory already exists (from earlier
`analyze` runs or a previous `--pre-analyze`) — the translate pass itself never updates memory,
so a plain `batch` over an unanalyzed series translates every episode without any series context.
With `--pre-analyze`, all episodes are analyzed first — that is what builds the memory — so every
translation benefits from the full series context. The command runs in two clearly labeled
phases:

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
used by all commands as defaults: an explicit flag always wins, then the project setting, then
the tool's built-in default. This applies to `translate`, `batch`, and the auxiliary commands
`analyze`, `review`, and `tighten` (for their shared options: `provider`, `model`, `target`,
`lang`, `reasoning`).

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

### Versioning and compatibility policy

The project follows [Semantic Versioning](https://semver.org/). While on `0.x`, the public surface
is the **CLI** (command names, options, output filename convention) and the facades in
`pipeline.py`/`cli.py`; the internal module layout is not a stable API.

- **Patch** (`0.x.Y`): bug fixes and docs, no behaviour change you need to act on.
- **Minor** (`0.X.0`): new commands/flags and changes to *default* behaviour (called out in the
  changelog).
- A flag or command is **deprecated** before removal: it keeps working for at least one minor
  release while emitting a warning, and the removal is noted in `CHANGELOG.md`. The
  `translate-subs` command remains as a permanent alias for `llm-subs`.

Every user-visible change is recorded in [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]` first.

### Local data and privacy

Everything runs locally; the only data that leaves your machine is the text sent to whichever
**provider** you choose (a remote agent CLI or model API — `ollama`/`litellm` against a local
server send nothing externally). On disk the tool stores:

- **Per-series memory** under `$XDG_DATA_HOME/llm-subs/projects/<series>/<target>/`: glossary,
  characters (with gender/register), style guide, conflicts, per-episode context, block
  checkpoints (which contain translated text), `settings.json`, and the `review`/`readability`
  reports. These are written **owner-only (0600)**, in **owner-only (0700) directories**, since
  they may contain subtitle text. **`llm-subs projects`** lists what is stored and how much space
  each project takes; **`llm-subs purge-project <name>`** deletes a finished project's state
  (or one target's subtree with `--target`) — the supported way to remove a series' traces,
  since this state can carry its subtitle text. The state is plain versioned JSON, so moving or
  backing up a project's memory is just copying its directory (no export/import command needed).
- **Extracted subtitle tracks** under `$XDG_CACHE_HOME/llm-subs/work/`: only a demux cache so a
  rerun skips re-extraction, kept in an owner-only (0700) directory. Clear it any time with
  **`llm-subs purge-cache`** (memory and reports are not touched).

The **final translated subtitle** is written with your normal umask (not 0600) so a media server
running under another account (Jellyfin/Plex) can read it.

Directories left group/other-readable by an **older release** are tightened on the next write.
Run **`llm-subs doctor`** to audit them: it flags any state or cache path still readable by other
users and prints the `chmod` to fix leftovers it can't repair automatically.

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
heavyweight supply-chain tooling (SBOM, signing, provenance, CodeQL), multi-process
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
  a `SECURITY` threat model, a CI that builds and smoke-tests the wheel, GitHub Actions pinned by
  commit SHA, and a Dependabot config that keeps those dependencies and Actions current. It does
  **not** add SBOM, provenance attestation, artifact signing, CodeQL or fully automated SemVer
  release pipelines — that machinery is maintenance overhead with no benefit for a community CLI
  distributed via a tagged release and `pipx install git+…`.
- **An automated translation-quality benchmark / "golden" corpus.** This is repeatedly raised as
  the headline gap, so to be explicit: there is **no** golden-corpus, blind human-eval harness or
  per-provider/prompt linguistic-regression suite, and there won't be — a serious one is a
  research project of its own. Quality is guarded by the `review` and `tighten` passes, by the
  deterministic contract (stable IDs, ID/timestamp validation, glossary/gender consistency
  checks) and by human reading. Prompt changes are reviewed by their effect on real episodes, not
  by an automated score.
- **A high coverage gate in CI.** CI enforces a **low floor** (`--cov-fail-under=85`) purely as a
  regression net — a sharp drop means a whole area lost its tests, and that should fail the build.
  But there is deliberately **no** strict 90–95% gate: chasing that number tends to incentivise
  filler tests that just exercise framework glue code (whether Typer prints the right error when a
  file is missing, for instance). Effort goes to tests that exercise real deterministic behaviour
  (extraction, reinsertion) instead of chasing coverage.
- **Fuzzing the parser against malformed ASS/SRT.** Parsing is delegated to `pysubs2`, so the
  project does not fuzz it for robustness to broken input. It *does* property-test its own
  extraction/reinsertion invariants (`tests/test_properties.py`) against generated well-formed
  events — that deterministic core is what it owns and must never corrupt.
- **OS-level sandboxing of agent CLIs.** Each agent CLI is invoked with its own built-in
  restriction so untrusted subtitle text can't talk it into touching your files: `codex` runs
  `--sandbox read-only`, `claude` denies every filesystem/exec/network/subagent tool and ignores
  all MCP servers (`--strict-mcp-config`), `antigravity` (`agy`) runs `--print --sandbox`, and
  `opencode` runs `--pure` (no external plugins) **plus an inline deny-all permission config** so
  none of its built-in tools (read/bash/webfetch/websearch) can run — `--pure` alone leaves those
  tools allowed, so the deny-all config is what actually stops a crafted cue from reading and
  exfiltrating files; none is ever given `--dangerously-skip-permissions`.
  On top of those flags, each agent CLI is launched from an empty throwaway working directory, so a
  crafted subtitle can't steer it toward files in your real cwd. Be honest about what each flag
  buys: `codex --sandbox read-only` blocks writes, exec and network but still permits *reads*, so
  the throwaway cwd (and the denied network, which blocks exfiltration) is what limits it, not the
  read-only flag alone. `antigravity` is the weakest — it replaced the Gemini CLI and is agentic;
  its `--sandbox` only restricts the terminal (no read-only/no-tools mode like the old
  `--approval-mode plan`), so the throwaway cwd is its *sole* containment. Prefer `claude`/`codex`
  over `antigravity` for material from an unknown source. Full OS isolation (containers/seccomp) is
  still out of scope; the strongest mitigation — use a local backend (`ollama`) for sensitive
  subtitles — is documented in [SECURITY.md](SECURITY.md).
- **Token-aware block sizing and map-reduce analysis.** Blocks are sized by line count. Subtitle
  lines are inherently short (it's a subtitle), so a fixed line budget rarely strains a model's
  context; a token-budget scheduler and hierarchical analysis for very long inputs are not
  warranted (the `analyze` cap with a notice covers the rare overflow).
- **Multi-writer coordination for memory.** The memory store assumes a single writer (one project
  processed at a time). There is no file locking or concurrent-update merging; atomic writes
  prevent *corruption*, not two processes racing on the same project.
- **Persistent TOML config, structured/JSON logging, and a web UI.** Configuration is via flags
  and output is human-readable text; a web UI is a deliberate non-goal — this is a focused CLI.

### Known limitations (accepted trade-offs)

- **Image-based subtitle tracks (PGS/VobSub) are not supported.** Blu-ray and some anime releases
  carry subtitles as bitmaps (`hdmv_pgs_subtitle`, `dvd_subtitle`) rather than text. The tool
  works on text it can parse into translatable units, so a container whose only subtitle track is
  image-based is rejected with a clear message — extracting that text would require an OCR step
  (e.g. Tesseract via SubtitleEdit), which is out of scope. Use a text sidecar (`.srt`/`.ass`) or
  OCR the track to text yourself first.
- **Standalone `analyze` and `review` expose no `--strict-lang`.** They can resolve a subtitle
  track from a video container, but they intentionally do not expose `--strict-lang`; adding the
  flag across those commands would expand the CLI for a rare personal-use case. When the
  container's automatic language choice is ambiguous, use `probe` and pass an explicit `--track`
  (or analyze a sidecar subtitle directly). `batch --strict-lang` does apply to its
  `--pre-analyze` pass, so a wrong-language source fails the episode instead of feeding the
  series memory.
- **Mixed ASS drawing/text events are treated as drawings.** An event containing drawing mode
  (`{\p1}` or higher) is skipped as a whole, even if it later switches back with `{\p0}` and
  contains visible text. Such mixed events are uncommon, while extracting only the text portion
  would require a stateful ASS override parser. If one must be translated, split or clean that
  event in the source subtitle first. Pure drawing events remain preserved verbatim in ASS output.
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

- Audio-assisted gender detection.

## License

[GPL-3.0-or-later](LICENSE). You may use, modify, and redistribute it, but derivative works
that you distribute must also be released under the GPL and ship their source.
