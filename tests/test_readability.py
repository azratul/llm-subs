from __future__ import annotations

import json

import pysubs2
import pytest

from translate_subs import config, pipeline
from translate_subs.ai.provider import ProviderError
from translate_subs.readability.compactor import FlaggedLine, compact_lines, parse_compactions
from translate_subs.readability.metrics import ReadabilityLimits, exceeds, measure


def test_measure_basic():
    m = measure("First line\nSecond line", 0, 2000)
    assert m.n_lines == 2
    assert m.max_line_chars == len("Second line")
    assert m.chars_total == len("First line") + len("Second line")
    assert m.duration_ms == 2000
    assert m.cps == m.chars_total / 2


def test_exceeds_each_reason():
    limits = ReadabilityLimits()
    long_line = measure("x" * 50, 0, 5000)  # 50 chars in 5s -> only length
    reasons = exceeds(long_line, limits)
    assert any("line too long" in r for r in reasons)

    three = measure("a\nb\nc", 0, 5000)
    assert any("too many lines" in r for r in exceeds(three, limits))

    fast = measure("x" * 36, 0, 1000)  # 36 cps
    assert any("too fast" in r for r in exceeds(fast, limits))

    assert exceeds(measure("Hola.", 0, 2000), limits) == []


def test_char_budget():
    m = measure("whatever", 0, 2000)  # 2 seconds
    assert m.char_budget(ReadabilityLimits()) == 36  # 18 cps * 2s


def test_parse_compactions_validates_ids():
    assert parse_compactions(json.dumps({"0001": "Corto."}), {"0001"}) == {"0001": "Corto."}
    with pytest.raises(ProviderError):
        parse_compactions(json.dumps({"9999": "x"}), {"0001"})
    with pytest.raises(ProviderError):
        parse_compactions(json.dumps({}), {"0001"})
    with pytest.raises(ProviderError):
        parse_compactions(json.dumps({"0001": "  "}), {"0001"})
    with pytest.raises(ProviderError, match="non-string"):
        parse_compactions(json.dumps({"0001": ["not", "text"]}), {"0001"})
    with pytest.raises(ProviderError):
        parse_compactions("not json", {"0001"})


def test_compact_lines_uses_runner():
    flagged = [
        FlaggedLine(
            id="0001",
            event_index=0,
            text="x" * 60,
            metrics=measure("x" * 60, 0, 2000),
            reasons=["line too long"],
        )
    ]
    seen = {}

    def fake_runner(prompt: str) -> str:
        seen["prompt"] = prompt
        return json.dumps({"0001": "Corto."})

    out = compact_lines(flagged, limits=ReadabilityLimits(), runner=fake_runner)
    assert out == {"0001": "Corto."}
    assert "[0001]" in seen["prompt"]


def test_compact_lines_chunks_into_blocks():
    flagged = [
        FlaggedLine(
            id=f"{i:04d}",
            event_index=i,
            text="x" * 60,
            metrics=measure("x" * 60, 0, 2000),
            reasons=["line too long"],
        )
        for i in range(95)
    ]
    calls = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        import re

        ids = re.findall(r"\[(\d{4})\]", prompt)
        return json.dumps({i: "Corto." for i in ids})

    out = compact_lines(flagged, limits=ReadabilityLimits(), runner=runner, block_size=40)
    assert len(calls) == 3  # 40 + 40 + 15
    assert len(out) == 95  # every flagged line still compacted across the blocks


def test_tighten_applies_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    subs = pysubs2.SSAFile()
    subs.events.append(
        pysubs2.SSAEvent(start=0, end=2000, text="This is a needlessly long subtitle line here.")
    )
    subs.events.append(pysubs2.SSAEvent(start=2000, end=5000, text="Hola."))
    srt = tmp_path / "ep01.es.srt"
    subs.save(str(srt), format_="srt")

    def fake_runner(prompt: str) -> str:
        return json.dumps({"0001": "Línea corta."})

    result = pipeline.tighten_subtitle(srt, project="Serie", apply=True, runner=fake_runner)

    assert result.n_subs == 2
    assert result.n_flagged == 1
    assert result.n_compacted == 1
    assert result.n_applied == 1
    assert result.n_residual == 0
    assert result.report_path.exists()

    reloaded = pysubs2.load(str(srt))
    assert reloaded.events[0].plaintext == "Línea corta."
    assert reloaded.events[1].plaintext == "Hola."


def test_readability_limits_reject_non_positive():
    ReadabilityLimits()  # defaults are valid
    for bad in (
        {"max_chars_per_line": 0},
        {"max_lines": 0},
        {"max_chars_per_second": -1},
    ):
        with pytest.raises(ValueError, match="must be positive"):
            ReadabilityLimits(**bad)


def test_is_safe_improvement_accepts_and_rejects():
    from translate_subs.readability.metrics import is_safe_improvement

    limits = ReadabilityLimits()
    original = measure("x" * 50, 0, 5000)  # too long
    assert is_safe_improvement(original, measure("Corto.", 0, 5000), limits)  # now compliant
    assert is_safe_improvement(original, measure("x" * 45, 0, 5000), limits)  # shorter, same axis
    assert not is_safe_improvement(original, measure("x" * 60, 0, 5000), limits)  # longer
    # Splitting one over-long line into three introduces a new (line_count) violation.
    assert not is_safe_improvement(original, measure("a\nb\nc", 0, 5000), limits)


def test_tighten_report_colocates_with_episode_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    from translate_subs.workflows.support import episode_key, readability_path

    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="x" * 60))
    srt = tmp_path / "Ep 01.es.srt"
    subs.save(str(srt), format_="srt")

    result = pipeline.tighten_subtitle(srt, target="es-latam", project="Show", use_llm=False)
    # The report uses the full target dir and the hashed episode key, matching translate/review.
    assert result.report_path == readability_path("Show", "es-latam", episode_key(srt))


def test_tighten_report_includes_provenance_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="x" * 60))
    srt = tmp_path / "ep01.es.srt"
    subs.save(str(srt), format_="srt")

    result = pipeline.tighten_subtitle(srt, target="es-latam", project="Show", use_llm=False)
    text = result.report_path.read_text("utf-8")
    assert "Translated: ep01.es.srt" in text
    assert "Target: es-latam" in text
    assert "Content fingerprint:" in text


def test_tighten_rejects_compaction_that_does_not_improve(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=5000, text="x" * 50))  # too long
    srt = tmp_path / "ep01.es.srt"
    subs.save(str(srt), format_="srt")

    # The "compaction" is even longer — writing it would make the line worse.
    result = pipeline.tighten_subtitle(
        srt, project="Serie", apply=True, runner=lambda _: json.dumps({"0001": "y" * 60})
    )

    assert result.n_compacted == 1
    assert result.n_applied == 0  # not written
    assert pysubs2.load(str(srt)).events[0].plaintext == "x" * 50  # original kept intact
    assert "Not applied" in result.report_path.read_text("utf-8")


def test_tighten_apply_preserves_ass_leading_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    subs = pysubs2.SSAFile()
    subs.events.append(
        pysubs2.SSAEvent(
            start=0,
            end=1000,
            text=(
                r"{\an8\pos(640,100)\c&H00FF00&}"
                "This is a needlessly long subtitle line for one second."
            ),
        )
    )
    ass = tmp_path / "ep01.es.ass"
    subs.save(str(ass))

    result = pipeline.tighten_subtitle(
        ass,
        project="Serie",
        apply=True,
        runner=lambda _: json.dumps({"0001": "Línea corta."}),
    )

    assert result.n_applied == 1
    event = pysubs2.load(str(ass)).events[0]
    assert event.plaintext == "Línea corta."
    assert event.text.startswith(r"{\an8\pos(640,100)\c&H00FF00&}")
