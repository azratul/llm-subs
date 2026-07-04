from __future__ import annotations

import json

import pytest

from translate_subs.ai.analysis import (
    EpisodeContext,
    analyze_episode,
    build_analysis_prompt,
    build_transcript,
    parse_context,
)
from translate_subs.ai.blocks import build_jobs
from translate_subs.ai.claude_cli import extract_json
from translate_subs.ai.provider import (
    ClaudeTranslationProvider,
    CliTranslationProvider,
    ProviderError,
    build_translation_prompt,
    parse_translation_reply,
)
from translate_subs.domain.models import TranslatableUnit
from translate_subs.subs.extractor import extract_units

SAMPLE_CONTEXT = {
    "episode_summary": "Akira meets Yumi.",
    "characters": [
        {
            "name": "Akira",
            "gender": "male",
            "role": "protagonist",
            "speech_style": "informal",
            "relationships": {"Yumi": "close friend"},
        }
    ],
    "glossary": {"Shadow Core": "Núcleo Sombrío"},
    "translation_rules": ["Use neutral Latin American Spanish."],
}


def test_build_transcript_is_clean(sample_ass):
    units = extract_units(sample_ass)
    transcript = build_transcript(units)
    assert transcript.splitlines()[0] == "[0001] ?: I won't forgive you!"
    assert "\\pos" not in transcript
    assert "\n" not in transcript.splitlines()[1]  # cue line break flattened


def test_analysis_prompt_contains_transcript_and_schema(sample_ass):
    units = extract_units(sample_ass)
    prompt = build_analysis_prompt(units, target="es-latam")
    assert "es-latam" in prompt
    assert "episode_summary" in prompt
    assert "[0001]" in prompt


def test_extract_json_handles_fences_and_prose():
    fenced = 'Here you go:\n```json\n{"a": 1}\n```\nthanks'
    assert json.loads(extract_json(fenced)) == {"a": 1}
    bare = 'noise {"a": 2} trailing'
    assert json.loads(extract_json(bare)) == {"a": 2}


def test_parse_context_valid_and_invalid():
    ctx = parse_context("```json\n" + json.dumps(SAMPLE_CONTEXT) + "\n```")
    assert isinstance(ctx, EpisodeContext)
    assert ctx.characters[0].gender == "male"
    assert ctx.glossary["Shadow Core"] == "Núcleo Sombrío"

    with pytest.raises(ProviderError):
        parse_context("not json at all")


def test_analyze_episode_uses_injected_runner(sample_ass):
    units = extract_units(sample_ass)
    seen = {}

    def fake_runner(prompt: str) -> str:
        seen["prompt"] = prompt
        return json.dumps(SAMPLE_CONTEXT)

    ctx = analyze_episode(units, target="es-latam", runner=fake_runner)
    assert ctx.episode_summary == "Akira meets Yumi."
    assert "[0001]" in seen["prompt"]


def test_analyze_episode_requires_units():
    with pytest.raises(ProviderError):
        analyze_episode([], target="es", runner=lambda p: "{}")


def test_translation_prompt_lists_only_translate_ids(sample_ass):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=["r"], block_size=1, context=1)
    prompt = build_translation_prompt(jobs[1])
    assert "TRANSLATE:" in prompt
    assert "[0002]" in prompt
    assert "CONTEXT (before):" in prompt


def test_multiline_cue_stays_single_prompt_line_and_round_trips():
    # A cue with an internal line break must not split into an unlabeled second physical line in
    # the prompt, and the break must survive translation as a real newline for reinsertion.
    unit = TranslatableUnit(
        id="0001", event_index=0, start=0, end=1000, style="Default", text="First line\nSecond line"
    )
    jobs = build_jobs([unit], target="es", rules=[], block_size=1, context=0)
    prompt = build_translation_prompt(jobs[0])

    section = prompt.split("TRANSLATE:")[1].split("\n\n")[0]
    body = [ln for ln in section.splitlines() if ln.strip()]
    # Exactly one physical line carries the cue, and the break is shown as the literal token.
    assert body == ["[0001] ?: First line\\nSecond line"]

    # The model echoes the \n token; parsing turns it back into a real newline.
    parsed = parse_translation_reply(json.dumps({"0001": "Primera línea\\nSegunda línea"}), jobs[0])
    assert parsed == {"0001": "Primera línea\nSegunda línea"}


def test_literal_backslash_n_is_not_a_line_break():
    # `C:\new` carries the byte pair `\n` as visible text. Unescaped it is indistinguishable from
    # the encoded line-break token, so the round trip used to split it into two lines. (File input
    # is folded earlier — pysubs2's plaintext already treats a stored `\n` as a break — so this
    # guards the protocol itself: the reply direction and any text handed to it unfolded.)
    unit = TranslatableUnit(
        id="0001", event_index=0, start=0, end=1000, style="Default", text=r"Open C:\new folder"
    )
    jobs = build_jobs([unit], target="es", rules=[], block_size=1, context=0)
    prompt = build_translation_prompt(jobs[0])

    section = prompt.split("TRANSLATE:")[1].split("\n\n")[0]
    body = [ln for ln in section.splitlines() if ln.strip()]
    # The literal backslash is doubled in the prompt, so the token space stays unambiguous.
    assert body == [r"[0001] ?: Open C:\\new folder"]

    # An obedient model echoes the doubled backslash; decoding restores the literal `\n`.
    parsed = parse_translation_reply(json.dumps({"0001": r"Abre la carpeta C:\\new"}), jobs[0])
    assert parsed == {"0001": r"Abre la carpeta C:\new"}

    # An escaped backslash and a break token coexist: `\\` must be consumed before the `n`.
    parsed = parse_translation_reply(json.dumps({"0001": r"C:\\new\nsegunda"}), jobs[0])
    assert parsed == {"0001": "C:\\new\nsegunda"}

    # A model that emitted a JSON newline escape already sends a real newline; it stays one.
    parsed = parse_translation_reply(json.dumps({"0001": "línea uno\nlínea dos"}), jobs[0])
    assert parsed == {"0001": "línea uno\nlínea dos"}


def test_claude_provider_round_trip_with_fake_runner(sample_ass):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=[], block_size=2, context=0)

    def fake_runner(prompt: str) -> str:
        # only the TRANSLATE block ids matter; map each to a marker
        translate_ids = [
            line.split("]")[0][1:]
            for line in prompt.split("TRANSLATE:")[1].splitlines()
            if line.startswith("[")
        ]
        return json.dumps({i: f"ES:{i}" for i in translate_ids})

    result = ClaudeTranslationProvider(runner=fake_runner).translate(jobs)
    assert result == {"0001": "ES:0001", "0002": "ES:0002", "0003": "ES:0003"}


def test_cli_provider_retries_invalid_reply(sample_ass):
    units = extract_units(sample_ass)
    jobs = build_jobs(units[:1], target="es", rules=[], block_size=1, context=0)
    calls = 0

    def flaky_runner(prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not json"
        return json.dumps({"0001": "Hola"})

    result = ClaudeTranslationProvider(runner=flaky_runner, max_retries=1).translate(jobs)
    assert result == {"0001": "Hola"}
    assert calls == 2


def test_analysis_retries_invalid_reply(sample_ass):
    units = extract_units(sample_ass)
    replies = iter(["not json", json.dumps(SAMPLE_CONTEXT)])

    ctx = analyze_episode(units, target="es", runner=lambda _: next(replies), max_retries=1)
    assert ctx.episode_summary == "Akira meets Yumi."


def test_parse_translation_reply_rejects_id_mismatch(sample_ass):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=[], block_size=2, context=0)
    with pytest.raises(ProviderError):
        parse_translation_reply(json.dumps({"9999": "x"}), jobs[0])
    with pytest.raises(ProviderError):
        parse_translation_reply(json.dumps({line.id: "" for line in jobs[0].translate}), jobs[0])


def test_persistent_empty_keeps_source_and_flags_id(sample_ass):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=[], block_size=10, context=0)
    sources = {line.id: line.text for job in jobs for line in job.translate}
    empty_reply = json.dumps({line_id: "" for line_id in sources})

    provider = CliTranslationProvider(runner=lambda _: empty_reply, max_retries=1)
    result = provider.translate(jobs)

    assert result == sources  # every empty id fell back to its source text
    assert sorted(provider.untranslated_ids) == sorted(sources)


def test_empty_recovers_when_a_retry_succeeds(sample_ass):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=[], block_size=10, context=0)
    sources = {line.id: line.text for job in jobs for line in job.translate}
    good = {line_id: f"T-{line_id}" for line_id in sources}
    replies = iter([json.dumps({k: "" for k in sources}), json.dumps(good)])

    provider = CliTranslationProvider(runner=lambda _: next(replies), max_retries=1)
    result = provider.translate(jobs)

    assert result == good
    assert provider.untranslated_ids == []
