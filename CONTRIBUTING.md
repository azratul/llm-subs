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

All must pass; CI enforces them on Python 3.11–3.14:

```bash
uv run ruff check translate_subs/ tests/
uv run ruff format --check translate_subs/ tests/   # run `ruff format` (no --check) to fix
uv run mypy translate_subs/          # strict mode (configured in pyproject [tool.mypy])
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

## Architecture in one screen

The flow, end to end: **resolve** a source (sidecar or an embedded track demuxed with ffmpeg,
`translate_subs/io/`) → **parse** with `pysubs2` and **extract** only each event's visible text
with a stable id, keeping the whole-line leading override block aside
(`translate_subs/subs/extractor.py`) → **build blocks** of `[ID] Speaker: text` with before/after
context, folding in relevance-filtered series memory and episode context
(`translate_subs/ai/blocks.py`, `translate_subs/memory/`) → **translate** through a provider,
checkpointed per block so a crash resumes (`translate_subs/ai/`) → **reinsert** by id, restore the
leading override block, prune non-translatable events, and **export** `.ass` (keeps positioning and
style) or `.srt` (flat; overlapping cues merged, `translate_subs/subs/reinserter.py`) →
**validate** before writing — nothing is written if validation fails
(`translate_subs/subs/validator.py`).

Layering: use-case orchestration lives in `translate_subs/workflows/`, Typer callbacks in
`translate_subs/commands/`, and `pipeline.py`/`cli.py` are **stable public facades** — keep their
imports, signatures and command/option names stable.

## How to add a translation provider

A provider is a callable `runner(prompt: str) -> str` behind the same abstraction.

1. **Implement** a small dataclass: a subprocess CLI goes in `translate_subs/ai/cli_adapters.py`
   (model an existing one like `CodexCli`), an HTTP/model API in
   `translate_subs/ai/api_adapters.py`. It only turns a prompt into text — the caller builds the
   prompt and parses the reply.
2. **Harden it.** Subtitle text is **untrusted**: launch any agent CLI from the empty throwaway
   `cwd` that `_run` provides and deny its tools/filesystem/network with the tool's own switches
   (see how `claude`/`codex`/`opencode` are locked down). A runner that can be talked into reading
   local files is not acceptable.
3. **Register** it in the `_RUNNERS` map so `make_runner` builds it, and add it to the provider
   help strings.
4. **Wire diagnostics** in `translate_subs/diagnostics.py` so `doctor --provider <name>` verifies
   the backend (binary on PATH / reachable server / installed package).
5. **Test** argv/behaviour with a fake `subprocess.run` (see `tests/test_phase6.py`) — no live
   calls; live-verify by hand and note it in the PR.

## Troubleshooting by provider

Run `llm-subs doctor --provider <name>` first; it checks each backend without an LLM call.

- **claude / codex / antigravity / opencode** — `… CLI not found on PATH`: install the agent CLI
  and put it on `PATH`; auth lives in the CLI's own config (run it once to log in). `antigravity`
  (`agy`) has the weakest isolation (terminal-only sandbox) — prefer `claude`/`codex` for untrusted
  input.
- **ollama** — `no server at …`: `ollama serve` or set `$OLLAMA_HOST`. `model '…' not installed`:
  `ollama pull <model>` (verify with `llm-subs doctor --provider ollama --model <model>`).
- **litellm** — `package not installed`: `uv sync --extra litellm` (or
  `pip install 'llm-subs[litellm]'`). The model id carries the provider prefix, e.g.
  `ollama/qwen3:4b`.
- **Embedded track issues** — `llm-subs probe <media>` to list tracks, then `--track <n>` or pass a
  sidecar directly. Image tracks (PGS/VobSub) are unsupported (they need OCR).

## Deliberate design decisions — please don't re-report these

The items below are recurring review findings that are **intentional** and have been audited. They
are listed here so a reviewer (human or automated) can tell a settled trade-off from a fresh bug.
If you believe one is actually wrong, open an issue with a concrete reproducing case rather than a
general "this looks unsafe" — the reasoning is what a PR needs to overturn.

- **`opencode` keeps `--pure` *and* an inline deny-all permission config.** `--pure` only drops
  external plugins; built-in tools (read/bash/webfetch) stay allowed, so the deny-all is what
  actually contains an untrusted cue. Do not "simplify" it to `--pure` alone.
- **Every agent CLI runs from an empty throwaway `cwd` with its own tool/sandbox switches.**
  `claude` denies all tools + `--strict-mcp-config`, `codex` is `--sandbox read-only`,
  `antigravity` gets `--sandbox` (terminal-only — the weakest, hence discouraged for untrusted
  input). These built-in switches are the containment; no extra container is used. That is a
  deliberate trade-off, not a claim of perfect isolation — `antigravity` in particular leans on the
  throwaway cwd alone, so prefer a local `ollama` model for material from an untrusted source.
- **Internal state is written owner-only (0600); the final subtitle is widened to the umask.**
  State can carry subtitle text and stays private; the output must be readable by a media server
  running as another user (Jellyfin/Plex). The two modes are intentionally different.
- **Series memory is segmented by the *full* target** (`es-latam` ≠ `es-es`), so variants of one
  language never share glossary/characters/conflicts.
- **Batch isolates per-episode failures; only systemic ones abort.** A parse error, or a
  content/protocol `ProviderError` (unparseable reply / wrong ids for *that* episode), is recorded
  as `failed` and the run continues. A systemic `ProviderError` — auth/config, quota/rate-limit, a
  service outage, or an unclassified (`unknown`) one — aborts the run, since retrying every
  remaining episode would just repeat it (`ProviderError.category`, propagated through
  `retry_provider_call`). `--strict-lang` stays translation-only (not on `analyze`/`review`).
- **Two fingerprints, two scopes — don't conflate them.** The *episode-context* fingerprint
  (`source_digest`) hashes only translation-relevant content (id/speaker/text): a timing-only edit
  doesn't change the translation, so it deliberately does **not** flag the context stale. The
  *output-manifest* fingerprint (`output_source_digest`) additionally covers timing, style and the
  leading override block, so a re-timed or re-styled source **is** flagged as a stale output — the
  file is desynchronised even though the translation is unchanged.
- **`.ass` output preserves non-translatable events verbatim** (drawings, comments, empty cues);
  only `.srt` prunes them, because SRT has no positioning.

## Known limitations — already tracked

Real gaps we know about. Don't silently close them, and don't re-file them as new:

- **`tighten --apply` replaces the whole line** and checks only that a compaction fits the character
  budget, not that meaning was preserved. `review --apply` now guards term fixes (glossary/name/
  honorific): a suggestion that changes more than one contiguous span is rejected, not applied.
  Neither is *silent*: by default both preview the diff and ask before writing (skip with `--yes`),
  so review the diff before confirming.
- **`flatten_overlaps` has a quadratic worst case** on pathologically dense `.srt` files; ordinary
  subtitles are unaffected.
- **The `litellm` extra is only smoke-tested in CI.** The main test job runs without extras; a
  separate job installs `.[litellm]` and imports the runner, so the install path is checked but the
  adapter's request/response handling is covered only by argv/mock tests, not a live model call.

## Good first issues

Well-bounded areas if you want to contribute without touching the deterministic core:

- **A language's legacy codepage mapping** — `_LANG_LEGACY_ENCODINGS` in
  `translate_subs/subs/document.py` maps `--lang` to the conventional legacy encoding. If your
  language's subtitles commonly use one that isn't mapped, adding it is a two-line change plus a
  test in `tests/test_encoding.py` (write bytes in that codepage, load, assert the text).
- **A linguistic regression fragment** — `tests/test_linguistic_regression.py` runs realistic
  fragments through the full prompt/reply/reinsert contract with no LLM. If your language has a
  feature that could break serialization (script direction, combining characters, unusual
  punctuation), add a `_FRAGMENTS` entry; the test harness does the rest.
- **A provider quirk** — the CLI adapters (`translate_subs/ai/cli_adapters.py`) are thin argv
  builders covered by `tests/test_cli_adapters.py`. If a backend CLI changes a flag, the fix and
  its test are usually under ten lines each. See "How to add a translation provider" above for
  a whole new backend.
- **A `doctor` check** — `translate_subs/diagnostics.py` is a list of independent, no-LLM
  `Check` functions. New environment pitfalls (a missing tool, a bad config value) slot in
  without touching anything else. `doctor` must never throw.

Please read "Deliberate design decisions" and "Known limitations" first so an intentional
behaviour doesn't come back as a bug report.

## Support and compatibility

Maintained on a best-effort basis. See the **Versioning and compatibility policy** in the
[README](README.md) for what a patch/minor release may change and how deprecations work. By
participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Commit messages

Write clear, imperative one-line summaries in English (e.g. "Add --strict-lang to translate").
Keep unrelated changes in separate commits.
