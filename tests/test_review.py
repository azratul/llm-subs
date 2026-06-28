from __future__ import annotations

import json

import pysubs2

from translate_subs import config, pipeline
from translate_subs.review.checks import (
    check_glossary,
    check_line_length,
    check_name_consistency,
    check_target_not_empty,
)
from translate_subs.review.models import Finding, ReviewLine, ReviewReport
from translate_subs.review.report import render_markdown
from translate_subs.review.reviewer import apply_safe_policy, parse_findings, review_lines


def _line(id, source, target, speaker=None, idx=0) -> ReviewLine:
    return ReviewLine(id=id, event_index=idx, speaker=speaker, source=source, target=target)


def test_review_lines_chunks_into_blocks():
    # More lines than one block: the runner must be called per block, not once for the lot.
    lines = [_line(f"{i:04d}", f"src {i}", f"dst {i}") for i in range(95)]
    calls = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return "[]"

    review_lines(
        lines,
        glossary={},
        genders={},
        target="es-latam",
        runner=runner,
        block_size=40,
    )
    assert len(calls) == 3  # 40 + 40 + 15
    # Each block only carries its own lines.
    assert "[0000]" in calls[0] and "[0040]" not in calls[0]
    assert "[0094]" in calls[2]


def test_check_glossary_flags_missing_rendering():
    lines = [_line("0001", "The Shadow Core glows.", "El núcleo brilla.")]
    findings = check_glossary(lines, {"Shadow Core": "Núcleo Sombrío"})
    assert len(findings) == 1 and findings[0].kind == "glossary"

    ok = [_line("0001", "The Shadow Core glows.", "El Núcleo Sombrío brilla.")]
    assert check_glossary(ok, {"Shadow Core": "Núcleo Sombrío"}) == []


def test_check_name_and_empty_and_length():
    assert check_name_consistency([_line("0001", "Akira runs.", "Akira corre.")], ["Akira"]) == []
    assert len(check_name_consistency([_line("0001", "Akira runs.", "Él corre.")], ["Akira"])) == 1
    assert len(check_target_not_empty([_line("0001", "Hello", "  ")])) == 1
    assert check_target_not_empty([_line("0001", "Hello", "Hola")]) == []
    long = "x" * 50
    assert len(check_line_length([_line("0001", "s", long)], max_chars=42)) == 1


def test_parse_findings_handles_array_and_fences():
    raw = (
        "```json\n"
        + json.dumps(
            [
                {
                    "scope": "line",
                    "id": "0001",
                    "kind": "gender",
                    "message": "m",
                    "current": "cansado",
                    "suggested": "cansada",
                    "auto_safe": True,
                }
            ]
        )
        + "\n```"
    )
    findings = parse_findings(raw)
    assert len(findings) == 1 and findings[0].auto and findings[0].has_fix


def test_parse_findings_treats_string_false_as_not_auto():
    # bool("false") is True in Python; a model returning the JSON string "false" must not flip
    # a finding to auto-safe. Only a real boolean true or the string "true" counts as auto.
    raw = json.dumps(
        [
            {
                "scope": "line",
                "id": "0001",
                "kind": "glossary",
                "suggested": "x",
                "auto_safe": "false",
            },
            {
                "scope": "line",
                "id": "0002",
                "kind": "glossary",
                "suggested": "y",
                "auto_safe": "true",
            },
            {
                "scope": "line",
                "id": "0003",
                "kind": "glossary",
                "suggested": "z",
                "auto_safe": True,
            },
        ]
    )
    findings = parse_findings(raw)
    assert [f.auto for f in findings] == [False, True, True]


def test_safe_policy_gender_requires_confirmed_speaker():
    lines = [
        _line("0001", "a", "cansado", speaker="Yumi"),
        _line("0002", "b", "cansado", speaker="Rina"),
    ]
    findings = [
        Finding(
            id="0001", kind="gender", current="cansado", suggested="cansada", message="", auto=True
        ),
        Finding(
            id="0002", kind="gender", current="cansado", suggested="cansada", message="", auto=True
        ),
        Finding(id="0001", kind="literal", current="x", suggested="y", message="", auto=True),
        Finding(id="0001", kind="proper_name", current="x", suggested="", message="", auto=True),
    ]
    apply_safe_policy(findings, lines, {"Yumi": "female"})
    assert findings[0].auto is True  # confirmed speaker
    assert findings[1].auto is False  # Rina not confirmed
    assert findings[2].auto is False  # literal is never safe
    assert findings[3].auto is False  # an empty replacement is never safe


def test_safe_policy_glossary_requires_rendering_in_suggestion():
    lines = [_line("0001", "a", "x"), _line("0002", "b", "y")]
    findings = [
        # Correct glossary fix: the suggestion actually carries the expected rendering.
        Finding(
            id="0001",
            kind="glossary",
            current="usa la katana",
            suggested="usa la Espada Sagrada",
            message="",
            auto=True,
        ),
        # Mislabeled as glossary but the suggestion has no glossary term — must be demoted.
        Finding(
            id="0002",
            kind="glossary",
            current="hola",
            suggested="qué tal",
            message="",
            auto=True,
        ),
    ]
    apply_safe_policy(findings, lines, {}, {"Sword": "Espada Sagrada"})
    assert findings[0].auto is True
    assert findings[1].auto is False


def test_safe_policy_proper_name_requires_known_name():
    lines = [_line("0001", "a", "x"), _line("0002", "b", "y")]
    findings = [
        # Inserts a known character name -> safe.
        Finding(
            id="0001",
            kind="proper_name",
            current="Alicia",
            suggested="Alice",
            message="",
            auto=True,
        ),
        # No known name in the suggestion -> demoted, even though the model called it safe.
        Finding(
            id="0002", kind="proper_name", current="x", suggested="whatever", message="", auto=True
        ),
    ]
    apply_safe_policy(findings, lines, {}, {}, ["Alice", "Bob"])
    assert findings[0].auto is True
    assert findings[1].auto is False
    # With no known names at all, a proper_name fix can't be verified, so it is never auto.
    again = [
        Finding(
            id="0001",
            kind="proper_name",
            current="Alicia",
            suggested="Alice",
            message="",
            auto=True,
        )
    ]
    apply_safe_policy(again, lines, {}, {}, [])
    assert again[0].auto is False


def test_render_markdown_splits_warnings_and_fixes():
    report = ReviewReport(
        episode="ep01",
        findings=[
            Finding(scope="global", kind="gender", message="Yumi gender shifts."),
            Finding(
                id="0001",
                kind="gender",
                message="wrong gender",
                current="cansado",
                suggested="cansada",
                auto=True,
            ),
        ],
    )
    md = render_markdown(report)
    assert "# Review ep01" in md
    assert "Global (gender): Yumi gender shifts." in md
    assert "### Line 0001 (gender) [auto]" in md
    assert "Suggested: cansada" in md


def test_pair_lines_reports_structural_mismatches():
    from translate_subs.domain.models import TranslatableUnit

    source = pysubs2.SSAFile()
    source.styles["Top"] = pysubs2.SSAStyle(alignment=8)
    source.events.extend(
        [
            pysubs2.SSAEvent(start=1000, end=2000, text="One", style="Top"),
            pysubs2.SSAEvent(start=2000, end=3000, text="Two", style="Top"),
        ]
    )
    units = [
        TranslatableUnit(
            id="0001",
            event_index=0,
            start=1000,
            end=2000,
            style="Top",
            text="One",
        ),
        TranslatableUnit(
            id="0001",
            event_index=1,
            start=2000,
            end=3000,
            style="Top",
            text="Two",
        ),
    ]

    target = pysubs2.SSAFile()
    target.events.extend(
        [
            pysubs2.SSAEvent(start=2000, end=3000, text="Uno", style="Default"),
            pysubs2.SSAEvent(start=1000, end=2000, text="Dos", style="Default"),
            pysubs2.SSAEvent(start=4000, end=5000, text="Extra", style="Default"),
        ]
    )

    _, findings = pipeline._pair_lines(
        units,
        target,
        source_subs=source,
        compare_styles=True,
    )
    kinds = {finding.kind for finding in findings}
    assert {
        "extra_event",
        "duplicate_id",
        "timing_mismatch",
        "style_mismatch",
        "out_of_order",
    } <= kinds


def test_review_translation_applies_only_safe_fixes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.styles["White"] = pysubs2.SSAStyle()
    e1 = pysubs2.SSAEvent(start=1000, end=3000, text="I'm tired of this.", style="White")
    e1.name = "Yumi"
    e2 = pysubs2.SSAEvent(start=3100, end=5000, text="Let's go.", style="White")
    e2.name = "Akira"
    src.events.extend([e1, e2])
    source_path = tmp_path / "ep01.en.ass"
    src.save(str(source_path))

    translated = pysubs2.SSAFile()
    translated.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Estoy cansado de esto."))
    translated.events.append(pysubs2.SSAEvent(start=3100, end=5000, text="Vámonos."))
    translated_path = tmp_path / "ep01.es.srt"
    translated.save(str(translated_path), format_="srt")

    project_dir = tmp_path / "projects" / "Serie" / "es-latam"  # per-target memory root
    project_dir.mkdir(parents=True)
    (project_dir / "memory.json").write_text(
        json.dumps({"characters": [{"name": "Yumi", "gender": "female"}]}), encoding="utf-8"
    )

    def fake_runner(prompt: str) -> str:
        return json.dumps(
            [
                {
                    "scope": "line",
                    "id": "0001",
                    "kind": "gender",
                    "message": "wrong gender",
                    "current": "Estoy cansado de esto.",
                    "suggested": "Estoy cansada de esto.",
                    "auto_safe": True,
                },
                {
                    "scope": "line",
                    "id": "0002",
                    "kind": "literal",
                    "message": "too flat",
                    "current": "Vámonos.",
                    "suggested": "¡Vámonos ya!",
                    "auto_safe": True,
                },
            ]
        )

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=True,
        runner=fake_runner,
    )

    assert result.n_lines == 2
    assert result.n_applied == 1  # only the confirmed-gender fix
    assert result.report_path.exists()

    report_text = result.report_path.read_text("utf-8")
    assert "Source: ep01.en.ass" in report_text
    assert "Translated: ep01.es.srt" in report_text
    assert "Target: es-latam" in report_text
    assert "Source fingerprint:" in report_text
    assert "Translated fingerprint:" in report_text
    assert "Provider:" in report_text

    reloaded = pysubs2.load(str(translated_path))
    assert reloaded.events[0].plaintext == "Estoy cansada de esto."
    assert reloaded.events[1].plaintext == "Vámonos."  # literal fix not applied


def test_review_apply_skips_conflicting_same_line_fixes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="The Sword shines."))
    source_path = tmp_path / "ep01.en.srt"
    src.save(str(source_path), format_="srt")

    translated = pysubs2.SSAFile()
    translated.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="La katana brilla."))
    translated_path = tmp_path / "ep01.es.srt"
    translated.save(str(translated_path), format_="srt")

    from translate_subs.memory.store import ProjectMemory
    from translate_subs.workflows.support import memory_root

    pm = ProjectMemory(memory_root("Serie", "es-latam"))
    pm.glossary["Sword"] = "Espada"
    pm.save()

    # Two safe glossary fixes target the SAME line with different (both valid) suggestions.
    # Each whole-line replacement would clobber the other, so neither must be applied.
    def fake_runner(prompt: str) -> str:
        return json.dumps(
            [
                {
                    "scope": "line",
                    "id": "0001",
                    "kind": "glossary",
                    "message": "use the glossary",
                    "current": "La katana brilla.",
                    "suggested": "La Espada brilla.",
                    "auto_safe": True,
                },
                {
                    "scope": "line",
                    "id": "0001",
                    "kind": "glossary",
                    "message": "use the glossary",
                    "current": "La katana brilla.",
                    "suggested": "La Espada resplandece.",
                    "auto_safe": True,
                },
            ]
        )

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=True,
        runner=fake_runner,
    )

    assert result.n_applied == 0  # conflicting fixes on one line -> left for a human
    assert pysubs2.load(str(translated_path)).events[0].plaintext == "La katana brilla."


def test_review_apply_preserves_ass_leading_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    # "Alice" must be a known character for the proper_name fix to qualify as safe.
    from translate_subs.memory.models import CharacterMemory, SeriesMemory
    from translate_subs.memory.store import ProjectMemory
    from translate_subs.workflows.support import memory_root

    ProjectMemory(
        memory_root("Serie", "es-latam"),
        memory=SeriesMemory(characters=[CharacterMemory(name="Alice")]),
    ).save()

    source = pysubs2.SSAFile()
    source.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Alice"))
    source_path = tmp_path / "ep.en.ass"
    source.save(str(source_path))

    translated = pysubs2.SSAFile()
    translated.events.append(
        pysubs2.SSAEvent(
            start=1000,
            end=3000,
            text=r"{\an8\pos(640,100)\c&H00FF00&}Alicia",
        )
    )
    translated_path = tmp_path / "ep.es.ass"
    translated.save(str(translated_path))

    reply = json.dumps(
        [
            {
                "scope": "line",
                "id": "0001",
                "kind": "proper_name",
                "message": "Keep the proper name.",
                "current": "Alicia",
                "suggested": "Alice",
                "auto_safe": True,
            }
        ]
    )
    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=True,
        runner=lambda _: reply,
    )

    assert result.n_applied == 1
    event = pysubs2.load(str(translated_path)).events[0]
    assert event.plaintext == "Alice"
    assert event.text.startswith(r"{\an8\pos(640,100)\c&H00FF00&}")


def test_review_apply_rejects_empty_safe_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    source = pysubs2.SSAFile()
    source.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Alice"))
    source_path = tmp_path / "ep.en.ass"
    source.save(str(source_path))

    translated = pysubs2.SSAFile()
    translated.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Alicia"))
    translated_path = tmp_path / "ep.es.ass"
    translated.save(str(translated_path))

    reply = json.dumps(
        [
            {
                "scope": "line",
                "id": "0001",
                "kind": "proper_name",
                "message": "Bad empty suggestion.",
                "current": "Alicia",
                "suggested": "",
                "auto_safe": True,
            }
        ]
    )
    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=True,
        runner=lambda _: reply,
    )

    assert result.n_applied == 0
    assert pysubs2.load(str(translated_path)).events[0].plaintext == "Alicia"


# ---------------------------------------------------------------------------
# pair_lines regression tests: ASS comment/drawing interleaving and SRT sequential
# ---------------------------------------------------------------------------


def _make_unit(id, event_index, start, end, text, style="D"):
    from translate_subs.domain.models import TranslatableUnit

    return TranslatableUnit(
        id=id, event_index=event_index, start=start, end=end, style=style, text=text
    )


def test_pair_lines_ass_comment_with_text_not_extra_event():
    # A Comment event with visible text preserved verbatim in an ASS translation must not
    # be reported as extra_event; only Dialogue events without a source unit qualify.
    from translate_subs.review.structure import pair_lines

    units = [
        _make_unit("0001", 0, 1000, 2000, "Hello"),
        _make_unit("0002", 2, 3000, 4000, "Goodbye"),
    ]
    target = pysubs2.SSAFile()
    target.events.append(pysubs2.SSAEvent(start=1000, end=2000, text="Hola"))  # idx 0
    comment = pysubs2.SSAEvent(start=2100, end=2500, text="staff note")  # idx 1 – Comment
    comment.is_comment = True
    target.events.append(comment)
    target.events.append(pysubs2.SSAEvent(start=3000, end=4000, text="Adiós"))  # idx 2

    lines, findings = pair_lines(units, target)

    assert len(lines) == 2
    assert lines[0].target == "Hola"
    assert lines[1].target == "Adiós"
    assert not any(f.kind == "extra_event" for f in findings)


def test_pair_lines_ass_interleaved_drawing_correct_pairing():
    # A drawing (empty plaintext) interleaved between two translatable events must not
    # confuse event_index-based pairing or produce spurious findings.
    from translate_subs.review.structure import pair_lines

    units = [
        _make_unit("0001", 0, 1000, 2000, "Line 1"),
        _make_unit("0002", 2, 3000, 4000, "Line 2"),  # note: index 2, not 1
    ]
    target = pysubs2.SSAFile()
    target.events.append(pysubs2.SSAEvent(start=1000, end=2000, text="Línea 1"))  # idx 0
    target.events.append(  # idx 1 — drawing, empty plaintext
        pysubs2.SSAEvent(start=2100, end=2500, text=r"{\p1}m 0 0 l 10 0 10 10{\p0}")
    )
    target.events.append(pysubs2.SSAEvent(start=3000, end=4000, text="Línea 2"))  # idx 2

    lines, findings = pair_lines(units, target)

    assert len(lines) == 2
    assert lines[0].target == "Línea 1"
    assert lines[1].target == "Línea 2"
    assert not any(f.kind in ("extra_event", "missing_id") for f in findings)


def test_pair_lines_srt_sequential_with_shifted_event_index():
    # After prune_to_units the SRT has events at positions 0,1 but the source units carry
    # event_index 0 and 2 (a drawing was at index 1). Sequential mode must pair by position
    # instead of event_index so both lines are matched correctly.
    from translate_subs.review.structure import pair_lines

    units = [
        _make_unit("0001", 0, 1000, 2000, "Line 1"),
        _make_unit("0002", 2, 3000, 4000, "Line 2"),  # event_index 2 ≠ SRT position 1
    ]
    target = pysubs2.SSAFile()
    target.events.append(pysubs2.SSAEvent(start=1000, end=2000, text="Línea 1"))  # pos 0
    target.events.append(pysubs2.SSAEvent(start=3000, end=4000, text="Línea 2"))  # pos 1

    lines_seq, findings_seq = pair_lines(units, target, sequential=True)
    lines_idx, findings_idx = pair_lines(units, target, sequential=False)

    # Sequential mode: both units paired correctly.
    assert len(lines_seq) == 2
    assert lines_seq[0].target == "Línea 1"
    assert lines_seq[1].target == "Línea 2"
    assert not any(f.kind == "missing_id" for f in findings_seq)

    # Index mode on the same SRT: unit with event_index=2 falls off the end (only 2 events).
    assert any(f.kind == "missing_id" for f in findings_idx)


def test_review_srt_resegmented_skips_llm(tmp_path, monkeypatch):
    # When an SRT target has more events than source units (flatten_overlaps re-segmented it),
    # the LLM must not be called — pairs would be mismatched — and a structural finding must
    # explain the situation.
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=1000, end=4000, text="First."))
    src.events.append(pysubs2.SSAEvent(start=2000, end=5000, text="Second."))
    source_path = tmp_path / "ep.en.srt"
    src.save(str(source_path), format_="srt")

    # Simulate what flatten_overlaps produces: 2 source lines → 3 SRT segments.
    resegmented = pysubs2.SSAFile()
    resegmented.events.append(pysubs2.SSAEvent(start=1000, end=2000, text="First."))
    resegmented.events.append(pysubs2.SSAEvent(start=2000, end=4000, text="First.\nSecond."))
    resegmented.events.append(pysubs2.SSAEvent(start=4000, end=5000, text="Second."))
    translated_path = tmp_path / "ep.es.srt"
    resegmented.save(str(translated_path), format_="srt")

    llm_called = []

    def fake_runner(prompt: str) -> str:
        llm_called.append(prompt)
        return "[]"

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=False,
        runner=fake_runner,
    )

    assert not llm_called, "LLM must not be called for a re-segmented SRT"
    kinds = {f.kind for f in result.report.findings}
    assert "srt_resegmented" in kinds


def test_review_srt_same_count_resegmented_skips_llm(tmp_path, monkeypatch):
    # flatten_overlaps can produce the same number of cues as the source but with shifted
    # timestamps (e.g. 3 overlapping sources → 3 different segments). The count check alone
    # does not catch this; the timing-mismatch guard must trigger instead.
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=1000, end=4000, text="A."))
    src.events.append(pysubs2.SSAEvent(start=2000, end=5000, text="B."))
    src.events.append(pysubs2.SSAEvent(start=3000, end=6000, text="C."))
    source_path = tmp_path / "ep.en.srt"
    src.save(str(source_path), format_="srt")

    # 3 source lines → 3 resegmented cues with different boundaries (same count, wrong times).
    resegmented = pysubs2.SSAFile()
    resegmented.events.append(pysubs2.SSAEvent(start=1000, end=2000, text="A."))
    resegmented.events.append(pysubs2.SSAEvent(start=2000, end=3000, text="A.\nB."))
    resegmented.events.append(pysubs2.SSAEvent(start=3000, end=6000, text="A.\nB.\nC."))
    translated_path = tmp_path / "ep.es.srt"
    resegmented.save(str(translated_path), format_="srt")

    llm_called = []

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=False,
        runner=lambda p: llm_called.append(p) or "[]",
    )

    assert not llm_called, "LLM must not be called when timestamps are mismatched"
    kinds = {f.kind for f in result.report.findings}
    assert "srt_resegmented" in kinds
