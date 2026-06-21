"""Round-trip of a real, tag-heavy .ass file loaded from disk.

The other suites build subtitles in memory; this one parses an actual .ass with the kinds of
tags real fansubs use — leading override blocks (`\\an8`, `\\pos`, inline colour), a mid-text
animation, karaoke, a vector drawing, a comment and an empty line — and checks that the visible
text is extracted cleanly, whole-line positioning is restored in `.ass`, and overlapping cues
merge in `.srt`.
"""

from __future__ import annotations

from translate_subs.ai.blocks import build_jobs
from translate_subs.ai.provider import IdentityProvider
from translate_subs.subs import document
from translate_subs.subs.extractor import extract_units
from translate_subs.subs.reinserter import apply_translations, flatten_overlaps, prune_to_units
from translate_subs.subs.validator import validate_file, validate_output

COMPLEX_ASS = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: Top,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,8,10,10,10,1
Style: Karaoke,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\i1}Stop right there!{\i0}
Dialogue: 0,0:00:03.00,0:00:05.00,Top,,0,0,0,,{\an8}Sign: DANGER
Dialogue: 0,0:00:03.00,0:00:05.00,Default,,0,0,0,,{\pos(640,700)\c&H00FF00&}Down here\Nsecond line
Dialogue: 0,0:00:05.00,0:00:07.00,Default,,0,0,0,,Plain{\fad(200,200)}tail
Dialogue: 0,0:00:07.00,0:00:08.00,Karaoke,,0,0,0,,{\k20}la{\k30}la
Comment: 0,0:00:08.00,0:00:09.00,Default,,0,0,0,,staff note
Dialogue: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,{\p1}m 0 0 l 10 0 10 10 0 10{\p0}
Dialogue: 0,0:00:10.00,0:00:11.00,Default,,0,0,0,,
"""


def _load_complex(tmp_path):
    path = tmp_path / "ep.ass"
    path.write_text(COMPLEX_ASS, encoding="utf-8")
    return document.load(path)


def _identity_jobs(units):
    jobs = build_jobs(units, target="es", rules=[], block_size=40, context=0)
    return IdentityProvider().translate(jobs)


def test_extracts_visible_text_and_captures_leading_tags(tmp_path):
    units = extract_units(_load_complex(tmp_path))
    # Comment, vector drawing and empty line are skipped; five visible lines remain.
    assert [u.text for u in units] == [
        "Stop right there!",
        "Sign: DANGER",
        "Down here\nsecond line",
        "Plaintail",  # mid-text \fad is inline and dropped, the words are kept
        "lala",  # karaoke syllable tags gone, visible text kept
    ]
    assert units[0].lead_tags == r"{\i1}"
    assert units[1].lead_tags == r"{\an8}"
    assert r"\pos(640,700)" in units[2].lead_tags
    assert units[3].lead_tags == ""  # text starts with a glyph, no whole-line block
    assert units[4].lead_tags == ""  # karaoke lead is never restored


def test_round_trip_to_ass_preserves_positioning(tmp_path):
    subs = _load_complex(tmp_path)
    units = extract_units(subs)
    apply_translations(subs, units, _identity_jobs(units))
    prune_to_units(subs, units)
    out = tmp_path / "out.ass"
    document.save(subs, out, fmt="ass")
    assert validate_output(out, units).ok

    texts = [e.text for e in document.load(out).events]
    assert any(r"\an8" in t for t in texts)
    assert any(r"\pos(640,700)" in t for t in texts)
    # Tags tied to the original wording must not be carried over onto the translation.
    assert all(r"\fad" not in t and r"\k" not in t for t in texts)


def test_ass_fidelity_check_flags_lost_style_and_tags(tmp_path):
    import pysubs2

    subs = _load_complex(tmp_path)
    units = extract_units(subs)
    apply_translations(subs, units, _identity_jobs(units))
    prune_to_units(subs, units)
    out = tmp_path / "out.ass"
    document.save(subs, out, fmt="ass")

    # A faithful round-trip passes even the strict fidelity check.
    assert validate_output(out, units, check_fidelity=True).ok

    # Tamper: drop every leading override block and rename all styles.
    tampered = pysubs2.load(str(out))
    for event in tampered.events:
        event.text = event.plaintext  # removes the {\an8\pos(..)} lead block
        event.style = "Renamed"
    bad = tmp_path / "bad.ass"
    tampered.save(str(bad))

    # Review mode (fidelity off) only checks structure, so the tampered file still "passes".
    assert validate_output(bad, units).ok
    strict = validate_output(bad, units, check_fidelity=True)
    assert not strict.ok
    assert any("style" in e for e in strict.errors)
    assert any("leading override" in e for e in strict.errors)


def test_overlapping_note_merges_in_srt(tmp_path):
    subs = _load_complex(tmp_path)
    units = extract_units(subs)
    apply_translations(subs, units, _identity_jobs(units))
    prune_to_units(subs, units)
    flatten_overlaps(subs)
    out = tmp_path / "out.srt"
    document.save(subs, out, fmt="srt")
    assert validate_file(out).ok

    reloaded = document.load(out)
    merged = [e for e in reloaded.events if (e.start, e.end) == (3000, 5000)]
    assert len(merged) == 1  # the \an8 note and the dialogue shared 3-5s -> one stacked cue
    assert "Sign: DANGER" in merged[0].plaintext
    assert "Down here" in merged[0].plaintext
    assert all(r"\an" not in e.text and r"\pos" not in e.text for e in reloaded.events)
