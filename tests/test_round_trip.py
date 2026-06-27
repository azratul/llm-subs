from __future__ import annotations

from pathlib import Path

import pysubs2
import pytest

from translate_subs.ai.blocks import build_jobs
from translate_subs.ai.job_protocol import TranslationJobOut
from translate_subs.ai.provider import FileHandoffProvider, IdentityProvider, ProviderError
from translate_subs.domain.models import TranslatableUnit
from translate_subs.naming import base_stem, output_path
from translate_subs.subs import document
from translate_subs.subs.extractor import extract_units
from translate_subs.subs.reinserter import apply_translations, flatten_overlaps, prune_to_units
from translate_subs.subs.validator import validate_output, validate_translations


def test_extractor_skips_non_translatable(sample_ass):
    units = extract_units(sample_ass)
    # tagged dialogue + multiline + sign = 3; comment, drawing and empty are skipped.
    assert len(units) == 3
    assert [u.id for u in units] == ["0001", "0002", "0003"]
    assert units[0].text == "I won't forgive you!"
    assert "\\pos" not in units[1].text
    assert units[1].text == "First line\nSecond line"
    assert units[2].style == "Sign"


def test_identity_round_trip_preserves_structure(sample_ass, tmp_path):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es-latam", rules=[], block_size=2, context=1)
    translations = IdentityProvider().translate(jobs)

    assert validate_translations(units, translations).ok

    apply_translations(sample_ass, units, translations)
    prune_to_units(sample_ass, units)
    out = tmp_path / "out.srt"
    document.save(sample_ass, out)

    result = validate_output(out, units)
    assert result.ok, result.errors

    reloaded = pysubs2.load(str(out))
    assert len(reloaded.events) == len(units)
    assert (reloaded.events[0].start, reloaded.events[0].end) == (1000, 3000)
    # Positioning never survives .srt; whole-line italic does (as <i>, reloaded as {\i1}).
    assert all("\\pos" not in e.text and "\\an" not in e.text for e in reloaded.events)


def test_blocks_have_context(sample_ass):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=["r"], block_size=1, context=1)
    assert len(jobs) == 3
    assert [line.id for line in jobs[1].context_before] == ["0001"]
    assert [line.id for line in jobs[1].translate] == ["0002"]
    assert [line.id for line in jobs[1].context_after] == ["0003"]


def test_file_handoff_writes_jobs_and_reads_back(sample_ass, tmp_path):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=[], block_size=2, context=0)
    provider = FileHandoffProvider(tmp_path)

    # No *.out.json yet: should fail asking for them to be filled.
    with pytest.raises(ProviderError):
        provider.translate(jobs)

    in_files = sorted(p.name for p in tmp_path.glob("*.in.json"))
    assert in_files == ["block_0001.in.json", "block_0002.in.json"]

    for job in jobs:
        out = TranslationJobOut(
            block_id=job.block_id,
            translations={line.id: f"ES:{line.text}" for line in job.translate},
        )
        (tmp_path / f"block_{job.block_id}.out.json").write_text(
            out.model_dump_json(), encoding="utf-8"
        )
    result = provider.translate(jobs)
    assert result["0001"] == "ES:I won't forgive you!"


def test_output_naming_strips_lang_suffix():
    # Default format is .ass.
    assert output_path("/m/Matrix (1999) 4K.mp4").name == "Matrix (1999) 4K.es.ass"
    assert output_path("/m/Matrix (1999) 4K.en.srt").name == "Matrix (1999) 4K.es.ass"
    assert base_stem(Path("/m/Show - 01.eng.ass")) == "Show - 01"
    # Any ISO 639-1 language suffix is recognized, not just a hardcoded few.
    assert base_stem(Path("/m/Show - 01.ru.srt")) == "Show - 01"
    assert base_stem(Path("/m/Show - 01.de.ass")) == "Show - 01"
    # A dot that is not a language suffix is kept.
    assert output_path("/m/Episode 3.5.mkv").name == "Episode 3.5.es.ass"
    assert base_stem(Path("/m/Episode 3.5.mkv")) == "Episode 3.5"
    # --format srt switches the extension.
    assert output_path("/m/Matrix (1999) 4K.mp4", fmt="srt").name == "Matrix (1999) 4K.es.srt"


def test_output_dir_override(tmp_path):
    out = output_path("/media/Show/Show - 01.mkv", out_dir=tmp_path)
    assert out == tmp_path / "Show - 01.es.ass"


def test_flatten_overlaps_merges_simultaneous_cues():
    subs = pysubs2.SSAFile()
    subs.styles["Note"] = pysubs2.SSAStyle(alignment=8)  # top
    subs.styles["Dialog"] = pysubs2.SSAStyle(alignment=2)  # bottom
    # A translator note and the dialogue share the exact same span.
    subs.events.append(
        pysubs2.SSAEvent(start=1000, end=4000, text="bento: box lunch", style="Note")
    )
    subs.events.append(
        pysubs2.SSAEvent(start=1000, end=4000, text="My sisters make me a bento.", style="Dialog")
    )
    # A later, non-overlapping line stays on its own.
    subs.events.append(pysubs2.SSAEvent(start=5000, end=7000, text="A b...bento?!", style="Dialog"))

    flatten_overlaps(subs)

    assert len(subs.events) == 2
    first = subs.events[0]
    assert (first.start, first.end) == (1000, 4000)
    # Top-aligned note is stacked above the dialogue.
    assert first.plaintext == "bento: box lunch\nMy sisters make me a bento."
    assert subs.events[1].plaintext == "A b...bento?!"


def test_flatten_overlaps_splits_partial_overlap():
    subs = pysubs2.SSAFile()
    subs.styles["Default"] = pysubs2.SSAStyle(alignment=2)
    # A long sign overlapping a shorter dialogue: timeline splits, no text is lost.
    subs.events.append(pysubs2.SSAEvent(start=0, end=6000, text="SIGN", style="Default"))
    subs.events.append(pysubs2.SSAEvent(start=2000, end=4000, text="Line.", style="Default"))

    flatten_overlaps(subs)

    spans = [(e.start, e.end, e.plaintext) for e in subs.events]
    assert spans == [
        (0, 2000, "SIGN"),
        (2000, 4000, "SIGN\nLine."),
        (4000, 6000, "SIGN"),
    ]


def test_extractor_captures_whole_line_leading_tags(sample_ass):
    units = extract_units(sample_ass)
    # Leading italic and \pos blocks are whole-line and captured; the bare sign has none.
    assert units[0].lead_tags == r"{\i1}"
    assert units[1].lead_tags == r"{\pos(640,690)}"
    assert units[2].lead_tags == ""


def test_extractor_drops_karaoke_lead():
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=1000, text=r"{\k20}la{\k30}la"))
    units = extract_units(subs)
    # Karaoke is per-syllable; restoring only the first \k would be wrong, so drop it.
    assert units[0].lead_tags == ""


def test_leading_tags_restored_in_ass(sample_ass, tmp_path):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=[], block_size=40, context=0)
    translations = IdentityProvider().translate(jobs)

    apply_translations(sample_ass, units, translations)
    prune_to_units(sample_ass, units)
    out = tmp_path / "out.ass"
    document.save(sample_ass, out, fmt="ass")

    reloaded = pysubs2.load(str(out))
    texts = [e.text for e in reloaded.events]
    # The positioned cue and the italic cue keep their leading block ahead of the text.
    assert any(t.startswith(r"{\pos(640,690)}") for t in texts)
    assert any(t.startswith(r"{\i1}") for t in texts)


def test_leading_tags_stripped_in_srt(sample_ass, tmp_path):
    units = extract_units(sample_ass)
    jobs = build_jobs(units, target="es", rules=[], block_size=40, context=0)
    apply_translations(sample_ass, units, IdentityProvider().translate(jobs))
    prune_to_units(sample_ass, units)
    out = tmp_path / "out.srt"
    document.save(sample_ass, out, fmt="srt")

    reloaded = pysubs2.load(str(out))
    # Positioning tags are stripped by the .srt writer; only basic styling survives.
    assert all("\\pos" not in e.text and "\\an" not in e.text for e in reloaded.events)


def test_validation_compares_duplicate_timestamps_by_position(sample_ass, tmp_path):
    units = extract_units(sample_ass)
    units[1].start = units[0].start
    units[1].end = units[0].end

    out = pysubs2.SSAFile()
    out.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="A"))
    out.events.append(pysubs2.SSAEvent(start=3100, end=5000, text="B"))
    out.events.append(pysubs2.SSAEvent(start=5200, end=7000, text="C"))
    path = tmp_path / "out.srt"
    out.save(str(path), format_="srt")

    result = validate_output(path, units)
    assert not result.ok
    assert any("timestamp mismatches by index" in error for error in result.errors)


def test_validation_tolerates_ass_centisecond_rounding(tmp_path):
    # A millisecond-precision source (e.g. an .srt sidecar) is rounded to the nearest
    # 10ms when written to .ass; that is inherent to the format, not a mismatch.
    units = [
        TranslatableUnit(
            id="0001", event_index=0, start=72139, end=75302, style="Default", text="A"
        ),
        TranslatableUnit(
            id="0002", event_index=1, start=78045, end=80207, style="Default", text="B"
        ),
    ]
    out = pysubs2.SSAFile()
    for unit in units:
        out.events.append(pysubs2.SSAEvent(start=unit.start, end=unit.end, text=unit.text))
    path = tmp_path / "out.ass"
    out.save(str(path), format_="ass")

    result = validate_output(path, units)
    assert result.ok, result.errors


def test_ass_output_preserves_drawing_events(sample_ass, tmp_path):
    # sample_ass has 6 events: 3 translatable + 1 comment + 1 drawing + 1 empty.
    # For ASS output the workflow skips prune_to_units, so all 6 survive.
    units = extract_units(sample_ass)
    apply_translations(sample_ass, units, {u.id: u.text for u in units})
    # No prune_to_units call — that's the point.
    out = tmp_path / "out.ass"
    document.save(sample_ass, out, fmt="ass")

    reloaded = pysubs2.load(str(out))
    assert len(reloaded.events) == 6  # all original events preserved

    result = validate_output(out, units, check_fidelity=True)
    assert result.ok, result.errors
    # Drawing events must not trigger the empty-translation warning.
    assert not result.warnings
