"""Agent-CLI adapters: argv construction, containment flags, runner/provider wiring."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from translate_subs.ai import cli_adapters
from translate_subs.ai.cli_adapters import AntigravityCli, CodexCli, OpencodeCli, make_runner
from translate_subs.ai.provider import CliTranslationProvider, IdentityProvider, ProviderError
from translate_subs.pipeline import PipelineError, make_ai_runner, make_provider


@pytest.fixture
def capture_run(monkeypatch):
    """Capture the argv/stdin a runner would execute, without spawning a process."""
    calls = {}

    def fake_which(name):
        return f"/usr/bin/{name}"

    def fake_run(cmd, input=None, capture_output=True, text=True, timeout=None, cwd=None, env=None):
        calls["cmd"] = cmd
        calls["input"] = input
        calls["cwd"] = cwd
        calls["env"] = env
        if "-o" in cmd:  # codex writes its final message to this file
            out = Path(cmd[cmd.index("-o") + 1])
            # Only write when -o points at an actual output path (codex), not a flag value.
            if out.is_absolute() and out.parent.exists():
                out.write_text("FROM_FILE", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="FROM_STDOUT", stderr="")

    monkeypatch.setattr(cli_adapters.shutil, "which", fake_which)
    monkeypatch.setattr(cli_adapters.subprocess, "run", fake_run)
    return calls


def test_codex_uses_stdin_and_output_file(capture_run):
    assert CodexCli(model="o3")("PROMPT") == "FROM_FILE"
    cmd = capture_run["cmd"]
    assert cmd[1:3] == ["exec", "--skip-git-repo-check"]
    # Hardening: model-generated commands run in a read-only sandbox.
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "-m" in cmd and "o3" in cmd
    assert cmd[-1] == "-"  # stdin
    assert capture_run["input"] == "PROMPT"


def test_antigravity_headless_via_stdin(capture_run):
    assert AntigravityCli(model="Gemini 3.5 Flash (Low)")("PROMPT") == "FROM_STDOUT"
    cmd = capture_run["cmd"]
    # --print runs one prompt non-interactively; --sandbox restricts the terminal.
    assert "--print" in cmd and "--sandbox" in cmd
    # Hardening: never auto-approve tool permissions.
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--model") + 1] == "Gemini 3.5 Flash (Low)"
    # The prompt arrives on stdin, not as an argument.
    assert capture_run["input"] == "PROMPT"


def test_opencode_passes_message_as_arg(capture_run):
    assert OpencodeCli()("PROMPT") == "FROM_STDOUT"
    cmd = capture_run["cmd"]
    assert cmd[1] == "run"
    # Hardening: no external plugins, and never auto-approve permissions.
    assert "--pure" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[-1] == "PROMPT"
    assert capture_run["input"] is None


def test_opencode_denies_all_tools_via_inline_config(capture_run):
    # Hardening: `--pure` alone leaves built-in tools (read/bash/webfetch) allowed; we inject an
    # inline config that denies every tool so untrusted subtitle text can't read/exfiltrate files.
    import json

    OpencodeCli()("PROMPT")
    env = capture_run["env"]
    assert env is not None and "OPENCODE_CONFIG_CONTENT" in env
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"permission": {"*": "deny"}}


def test_cli_adapters_run_from_throwaway_cwd(capture_run):
    # Hardening: each agent runs in an empty temp dir, not the user's real working directory.
    for runner in (CodexCli(), AntigravityCli(), OpencodeCli()):
        runner("PROMPT")
        cwd = capture_run["cwd"]
        assert cwd is not None and Path(cwd).name.startswith("llm-subs-cwd-")


def test_make_runner_and_unknown():
    assert isinstance(make_runner("codex"), CodexCli)
    assert isinstance(make_runner("antigravity"), AntigravityCli)
    with pytest.raises(ProviderError):
        make_runner("nope")


def test_make_runner_applies_timeout_override():
    # Default timeout when not overridden, custom timeout threaded through to the runner.
    assert make_runner("codex").timeout == 600
    assert make_runner("codex", timeout=30).timeout == 30
    # Also flows through the provider factory's runner.
    from translate_subs.workflows.support import make_provider as _mp

    provider = _mp("claude", Path("/tmp"), timeout=45)
    assert provider.runner.timeout == 45


def test_make_provider_wires_cli_providers(tmp_path):
    assert isinstance(make_provider("identity", tmp_path), IdentityProvider)
    for name in ("claude", "codex", "antigravity", "opencode"):
        assert isinstance(make_provider(name, tmp_path), CliTranslationProvider)
    with pytest.raises(PipelineError):
        make_provider("bogus", tmp_path)


def test_make_ai_runner_rejects_non_generative_provider():
    assert isinstance(make_ai_runner("codex"), CodexCli)
    with pytest.raises(PipelineError, match="cannot perform this operation"):
        make_ai_runner("identity")
