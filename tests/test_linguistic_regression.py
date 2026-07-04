"""Linguistic regression pack: realistic multilingual fragments through the deterministic core.

Not a model benchmark — no LLM is called. Each fragment exercises the full prompt/reply/reinsert
contract (extract -> build_jobs -> build_translation_prompt -> echo reply -> parse -> apply) the
way a real episode does, so a prompt or serialization change that would visibly degrade
translations (dropped speaker framing, broken multiline token, memory rules leaking into the
wrong block, mangled non-Latin text) fails here instead of on a real translation run.
"""

from __future__ import annotations

import json

import pysubs2
import pytest

from translate_subs.ai.blocks import build_jobs
from translate_subs.ai.provider import build_translation_prompt, parse_translation_reply
from translate_subs.memory.models import CharacterMemory
from translate_subs.memory.rules import build_memory_rules, rules_for_text
from translate_subs.memory.store import ProjectMemory
from translate_subs.readability.metrics import ReadabilityLimits, exceeds, measure
from translate_subs.subs import document
from translate_subs.subs.extractor import extract_units
from translate_subs.subs.reinserter import apply_translations, flatten_overlaps, prune_to_units

_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,30,30,30,1
Style: Sign,Arial,36,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,8,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass(*event_lines: str) -> pysubs2.SSAFile:
    return pysubs2.SSAFile.from_string(_ASS_HEADER + "\n".join(event_lines))


def _echo_reply(job) -> str:
    """What a perfectly obedient model returns: every id echoed exactly as the prompt shows it —
    backslashes doubled, breaks as the literal \\n."""
    return json.dumps(
        {line.id: line.text.replace("\\", "\\\\").replace("\n", "\\n") for line in job.translate},
        ensure_ascii=False,
    )


def _round_trip(subs: pysubs2.SSAFile, *, target: str = "es-latam", rules_for=None):
    """Extract, block, prompt, echo, parse and reinsert; returns (units, jobs, prompts)."""
    units = extract_units(subs)
    jobs = build_jobs(units, target=target, rules_for=rules_for)
    prompts = [build_translation_prompt(job) for job in jobs]
    translations: dict[str, str] = {}
    for job in jobs:
        translations.update(parse_translation_reply(_echo_reply(job), job))
    apply_translations(subs, units, translations)
    return units, jobs, prompts


# --- multiline, overlaps and signs ----------------------------------------------------


def test_multiline_cue_stays_one_physical_prompt_line_and_round_trips():
    subs = _ass(
        r"Dialogue: 0,0:00:01.00,0:00:04.00,Default,Akane,0,0,0,,First line\NSecond line",
        "Dialogue: 0,0:00:05.00,0:00:07.00,Default,Kyosuke,0,0,0,,Single line.",
    )
    units, jobs, prompts = _round_trip(subs)

    translate_section = prompts[0].split("TRANSLATE:\n", 1)[1].split("\n\n", 1)[0]
    # The two-line cue must not split into an unlabeled physical line the model can't attribute.
    assert len(translate_section.splitlines()) == len(jobs[0].translate)
    assert "[0001] Akane: First line\\nSecond line" in translate_section

    assert subs.events[0].plaintext == "First line\nSecond line"


def test_sign_keeps_position_override_and_style_through_translation():
    subs = _ass(
        r"Dialogue: 0,0:00:01.00,0:00:04.00,Sign,,0,0,0,,{\an8\pos(640,60)}TRAIN STATION",
        "Dialogue: 0,0:00:01.50,0:00:04.00,Default,Akane,0,0,0,,Look at that sign!",
    )
    units, _, _ = _round_trip(subs)

    assert units[0].lead_tags == r"{\an8\pos(640,60)}"
    # The simultaneous sign keeps its whole-line override block and its top-aligned style.
    assert subs.events[0].text.startswith(r"{\an8\pos(640,60)}")
    assert subs.events[0].style == "Sign"


def test_overlapping_sign_and_dialogue_both_survive_srt_flattening():
    subs = _ass(
        r"Dialogue: 0,0:00:01.00,0:00:04.00,Sign,,0,0,0,,{\an8}BAKERY",
        "Dialogue: 0,0:00:02.00,0:00:05.00,Default,Akane,0,0,0,,Let's buy bread.",
    )
    units, _, _ = _round_trip(subs)
    prune_to_units(subs, units)
    flatten_overlaps(subs)

    stacked = [e.plaintext for e in subs.events if "BAKERY" in e.plaintext]
    assert stacked, "the sign text was dropped while flattening overlaps"
    assert any("Let's buy bread." in e.plaintext for e in subs.events)


# --- names, gender, register and glossary ---------------------------------------------


@pytest.fixture()
def series_memory(tmp_path):
    pm = ProjectMemory(tmp_path / "Kimagure")
    pm.glossary.update({"Power Sword": "Espada de Poder", "Tokyo": "Tokio", "same": "same"})
    pm.memory.characters.extend(
        [
            CharacterMemory(name="Akane", gender="female", speech_style="casual, teasing"),
            CharacterMemory(name="Kyosuke", gender="male"),
            CharacterMemory(name="Madoka", gender="female"),
        ]
    )
    return pm


def test_gender_and_speech_style_injected_only_for_referenced_characters(series_memory):
    mr = build_memory_rules(series_memory, None)
    rules = rules_for_text(mr, "[0001] Akane: Kyosuke, wait for me!", ["Akane"])

    joined = "\n".join(rules)
    assert "Akane: female" in joined
    assert "Kyosuke: male" in joined
    assert "Madoka" not in joined  # not in this block: must not bloat the prompt
    assert "casual, teasing" in joined


def test_glossary_term_injected_only_in_blocks_that_mention_it(series_memory):
    mr = build_memory_rules(series_memory, None)

    with_term = "\n".join(rules_for_text(mr, "He drew the Power Sword.", [None]))
    without_term = "\n".join(rules_for_text(mr, "Nothing relevant here.", [None]))
    assert "Power Sword -> Espada de Poder" in with_term
    assert "Espada de Poder" not in without_term
    # Identity mappings carry no instruction and must be dropped outright.
    assert "same -> same" not in with_term


def test_honorifics_survive_the_echo_round_trip(series_memory):
    subs = _ass(
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,Kyosuke,0,0,0,,Ayukawa-san, good morning.",
        "Dialogue: 0,0:00:03.50,0:00:05.00,Default,Madoka,0,0,0,,Morning, Kasuga-kun.",
    )
    mr = build_memory_rules(series_memory, None)

    def rules_for(lines):
        text = " ".join(line.text for line in lines)
        return rules_for_text(mr, text, [line.speaker for line in lines])

    _, jobs, prompts = _round_trip(subs, rules_for=rules_for)
    assert "Ayukawa-san" in prompts[0]
    assert subs.events[0].plaintext == "Ayukawa-san, good morning."


# --- scripts: Japanese, Spanish, Arabic, Korean, combining marks -----------------------

_FRAGMENTS = [
    ("japanese", "ちょっと待ってよ、恭介！\n本当にもう…"),
    ("japanese_long_vowel", "そうだねー、ラーメン食べたい。"),
    ("spanish", "¡¿Qué haces aquí?! Ándale, ven acá."),
    ("arabic_rtl", "انتظر لحظة من فضلك."),
    ("korean", "잠깐만 기다려 주세요."),
    ("combining_marks", "Naïve? C'est déjà vu."),
]


@pytest.mark.parametrize(("name", "text"), _FRAGMENTS)
def test_fragment_survives_prompt_reply_reinsert_and_save(tmp_path, name, text):
    escaped = text.replace("\n", r"\N")
    subs = _ass(f"Dialogue: 0,0:00:01.00,0:00:04.00,Default,Akane,0,0,0,,{escaped}")
    _round_trip(subs)
    assert subs.events[0].plaintext == text

    out = tmp_path / f"{name}.es.ass"
    document.save(subs, out, fmt="ass")
    reloaded = document.load(out)
    assert reloaded.events[0].plaintext == text


def test_cjk_line_width_is_measured_in_columns_not_codepoints():
    limits = ReadabilityLimits()
    kanji_line = "私は昨日友達と一緒に映画館へ行きました"  # 19 chars but 38 columns
    latin_line = "I went to the movies with my friends."  # 37 chars, 37 columns

    kanji = measure(kanji_line + "ですよ", 0, 10_000)  # 22 codepoints = 44 columns > 42
    latin = measure(latin_line, 0, 10_000)
    assert kanji.max_line_chars == 44  # measured in display columns, not codepoints
    assert any("line too long" in reason for reason in exceeds(kanji, limits))
    assert exceeds(latin, limits) == []
