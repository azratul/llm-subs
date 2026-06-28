from __future__ import annotations

import types
from pathlib import Path

import pysubs2
import pytest
from typer.testing import CliRunner

from translate_subs import config, pipeline
from translate_subs.ai import cli_adapters
from translate_subs.ai.cli_adapters import AntigravityCli, CodexCli, OpencodeCli, make_runner
from translate_subs.ai.provider import CliTranslationProvider, IdentityProvider, ProviderError
from translate_subs.cli import app
from translate_subs.pipeline import PipelineError, make_ai_runner, make_provider
from translate_subs.subs.validator import validate_file


@pytest.fixture
def capture_run(monkeypatch):
    """Capture the argv/stdin a runner would execute, without spawning a process."""
    calls = {}

    def fake_which(name):
        return f"/usr/bin/{name}"

    def fake_run(cmd, input=None, capture_output=True, text=True, timeout=None, cwd=None):
        calls["cmd"] = cmd
        calls["input"] = input
        calls["cwd"] = cwd
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


def test_cli_reports_expected_error_without_traceback():
    result = CliRunner().invoke(
        app, ["translate", "/tmp/definitely-missing-subtitle.srt", "--non-interactive"]
    )
    assert result.exit_code == 1
    assert "Path does not exist" in result.output
    assert "Traceback" not in result.output


def test_validate_file(tmp_path):
    good = pysubs2.SSAFile()
    good.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hola."))
    p = tmp_path / "good.srt"
    good.save(str(p), format_="srt")
    assert validate_file(p).ok

    bad = pysubs2.SSAFile()
    bad.events.append(pysubs2.SSAEvent(start=3000, end=1000, text="Bad timing."))
    q = tmp_path / "bad.srt"
    bad.save(str(q), format_="srt")
    assert not validate_file(q).ok

    empty = tmp_path / "empty.srt"
    empty.write_text("", encoding="utf-8")
    assert not validate_file(empty).ok


def test_validate_file_allows_italics_warns_zero_duration(tmp_path):
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text=r"{\i1}En cursiva{\i0}"))
    subs.events.append(pysubs2.SSAEvent(start=2000, end=2000, text="Duración cero."))
    p = tmp_path / "it.srt"
    subs.save(str(p), format_="srt")

    result = validate_file(p)
    assert result.ok  # basic italics are fine, zero-duration is only a warning
    assert not any("markup" in e for e in result.errors)
    assert any("zero-duration" in w for w in result.warnings)


def test_validate_file_flags_nonbasic_markup_in_srt(tmp_path):
    # Leftover positioning markup in a flat .srt signals a reinsertion failure.
    p = tmp_path / "leftover.srt"
    p.write_text("1\n00:00:00,000 --> 00:00:02,000\n{\\an8}Mal\n", encoding="utf-8")
    assert not validate_file(p).ok


def test_validate_file_allows_markup_in_ass(tmp_path):
    # In .ass, positioning/colour override tags are legitimate (restored on purpose).
    subs = pysubs2.SSAFile()
    subs.styles["Default"] = pysubs2.SSAStyle()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text=r"{\pos(640,690)}Mal"))
    p = tmp_path / "pos.ass"
    subs.save(str(p))
    assert validate_file(p).ok


def test_translate_output_coerces_suffix_to_format(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hello."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    out = tmp_path / "custom_name"  # no extension
    result = pipeline.translate_subtitle(
        source, provider="identity", interactive=False, output=out, project="P"
    )
    assert result.output_path == tmp_path / "custom_name.ass"  # default format
    assert result.output_path.exists()

    result = pipeline.translate_subtitle(
        source, provider="identity", interactive=False, output=out, fmt="srt", project="P"
    )
    assert result.output_path == tmp_path / "custom_name.srt"
    assert result.output_path.exists()


def test_output_name_uses_target_lang_code(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hello."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    result = pipeline.translate_subtitle(
        source, provider="identity", target="fr-FR", interactive=False, project="P"
    )
    assert result.output_path.name == "ep.fr.ass"


def test_compact_memory_command(tmp_path, monkeypatch):
    from translate_subs.memory.models import CharacterMemory, SeriesMemory
    from translate_subs.memory.store import ProjectMemory

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    pm = ProjectMemory(
        project_dir=tmp_path / "S" / "es-latam",  # per-target memory root (default target)
        memory=SeriesMemory(characters=[CharacterMemory(name="Extra")]),  # empty -> removed
        glossary={"A": "A", "B": "C"},  # identity "A" dropped
    )
    pm.save()

    result = CliRunner().invoke(app, ["compact-memory", "S"])
    assert result.exit_code == 0

    reloaded = ProjectMemory.load(tmp_path / "S" / "es-latam")
    assert reloaded.glossary == {"B": "C"}
    assert reloaded.memory.characters == []


def test_compact_memory_missing_project_errors():
    result = CliRunner().invoke(app, ["compact-memory", "does-not-exist-xyz"])
    assert result.exit_code == 1
    assert "No memory at" in result.output


def test_translate_unsupported_format_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hello."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    with pytest.raises(PipelineError, match="Unsupported format"):
        pipeline.translate_subtitle(
            source, provider="identity", interactive=False, fmt="vtt", project="P"
        )


def test_default_rules_and_lang_code_are_target_driven():
    from translate_subs import config as cfg
    from translate_subs.naming import lang_code

    rules = cfg.default_rules("fr-FR")
    assert any("fr-FR" in r for r in rules)
    assert not any("Spanish" in r or "es-latam" in r for r in rules)
    assert lang_code("es-latam") == "es"
    assert lang_code("fr-FR") == "fr"
    assert lang_code("ja") == "ja"


def test_review_prompt_uses_source_lang_label():
    from translate_subs.review.models import ReviewLine
    from translate_subs.review.reviewer import build_review_prompt

    lines = [ReviewLine(id="0001", event_index=0, source="Hello", target="Bonjour")]
    prompt = build_review_prompt(lines, glossary={}, genders={}, target="fr-FR", source_lang="ja")
    assert "JA: Hello" in prompt
    assert "EN:" not in prompt
