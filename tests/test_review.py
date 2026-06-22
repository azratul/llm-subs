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
