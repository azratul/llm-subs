# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability" on the repository's *Security* tab) rather than a public issue.
You can expect an initial response within a few days.

## Threat model

`llm-subs` is a local command-line tool that you run on your own machine, on media you
choose. Keep two things in mind.

### 1. Subtitle content is untrusted input sent to a translation backend

The tool extracts the *visible text* of each line and sends it to the backend you select
(`--provider`). That text leaves your machine whenever the backend is a remote service:

- **Agent CLIs** (`claude`, `codex`, `antigravity`, `opencode`) and **`litellm`** with a hosted
  model send the text to that provider. Treat this like any third-party API: the content may
  be transmitted, logged, or retained per that provider's policy.
- **`ollama`** (and `litellm` pointed at a local model) keeps everything on your machine —
  **provided the server is local**. `$OLLAMA_HOST` can just as well point at a remote server,
  and then the text is sent to it over HTTP like any other remote backend;
  `doctor --provider ollama` flags a non-loopback host for exactly this reason.

If your subtitles are sensitive, prefer a local backend.

### 2. Prompt injection when using agent CLIs with tool access

A subtitle file from an untrusted source can embed adversarial text ("ignore your
instructions and …"). If you route translation through an **agent CLI that has access to a
shell, the filesystem, or other tools**, a crafted subtitle could in principle try to induce
unintended actions.

Mitigations, in order of effectiveness:

- For bulk translation of subtitles you did not create yourself, prefer a **pure-inference
  backend** (`ollama`, or `litellm` to an inference endpoint). These have no tools to abuse.
- Each agent CLI is already invoked with its own built-in restriction so it cannot act on a
  crafted subtitle: `codex` runs `--sandbox read-only`; `claude` denies every
  filesystem/exec/network/subagent tool (`--disallowedTools`) and ignores all MCP servers
  (`--strict-mcp-config`); `antigravity` (`agy`) runs `--print --sandbox`; `opencode` runs
  `--pure` (no external plugins) and is never passed `--dangerously-skip-permissions`. These are
  the CLIs' own flags, not OS isolation — keep each CLI updated and don't override them.
- **`antigravity` is the exception**: it is agentic and, unlike the Gemini CLI it replaced (which
  ran read-only via `--approval-mode plan`), has no read-only/no-tools switch. Its `--sandbox`
  only restricts the terminal — commands can still run — so its only real containment is the
  throwaway working directory below. It is a weaker, more implicit guarantee than the other CLIs.
- Each agent CLI is launched from an empty throwaway working directory, so even within its
  sandbox a crafted subtitle cannot point the agent at files in your real working directory. For
  `antigravity` this throwaway cwd is the primary containment, not a backstop.
- A `--target` is validated as a language tag before it touches the filesystem, so it cannot be
  used to write a translation or report outside its intended directory.
- The tool never hands the raw subtitle file to the backend: content is structured as
  `[ID] text` data lines, which reduces — but does not eliminate — injection risk.

There is still **no OS-level sandbox** (container/seccomp) around the agent process; the built-in
restrictions above are the first line of defence, and full isolation remains the responsibility of
how you invoke and permission that CLI.

## Scope

Reports about the deterministic core (parsing, reinsertion, validation, path handling) and
about the documented backends are in scope. The behaviour of third-party agent CLIs and model
providers themselves is out of scope — report those to their respective projects.
