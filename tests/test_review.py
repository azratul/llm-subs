from __future__ import annotations

import json

import pysubs2
import pytest

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


def test_check_line_length_measures_display_width():
    # CJK glyphs occupy two columns, so 22 of them exceed the 42-column budget even though
    # len() == 22 would not. review must agree with tighten, which measures display width.
    wide = "\u3042" * 22
    assert len(check_line_length([_line("0001", "s", wide)], max_chars=42)) == 1
    # A combining mark adds no column: "e" + U+0301 is one column wide. 40 such pairs that
    # len() counts as 80 chars measure as 40 columns -- under the limit, so no finding.
    combining = "e\u0301" * 40
    assert check_line_length([_line("0001", "s", combining)], max_chars=42) == []


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


def test_is_single_span_edit():
    from translate_subs.review.reviewer import is_single_span_edit

    # One short term swapped -> accepted (with or without surrounding context).
    assert is_single_span_edit("La Sword del héroe", "La Espada del héroe") is True
    assert is_single_span_edit("Hola Sword", "Hola Espada") is True
    assert is_single_span_edit("Alicia", "Alice") is True  # a name-only cue is a valid term swap
    # The term AND a surrounding word changed -> two separate spans -> rejected.
    assert is_single_span_edit("La Sword del héroe", "La Espada del guerrero") is False
    # A full reword is never a single-span term fix.
    assert is_single_span_edit("Buenos días amigo", "Qué tal compañero") is False
    # A single span longer than a term is a reword (the model expanded/rewrote) -> rejected.
    assert is_single_span_edit("Sword!", "Espada legendaria!") is False
    assert is_single_span_edit("Sword", "Espada legendaria") is False
    reworded = is_single_span_edit("Dice la Sword ahora", "Dice la palabra mucho más larga ahora")
    assert reworded is False
    # A large space-less CJK rewrite is bounded by the size cap even though it is one token.
    assert is_single_span_edit("剣が光る", "伝説の剣が空高くまばゆく光り輝いている") is False


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

    # The report's provenance must match the file as written, not the pre-apply state.
    import hashlib

    def _fp(events) -> str:
        joined = "\n".join(f"{e.start},{e.end},{e.plaintext}" for e in events)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    expected = _fp(reloaded.events)
    assert f"Translated fingerprint: {expected}" in report_text


def test_review_apply_rejects_multi_span_glossary_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.styles["White"] = pysubs2.SSAStyle()
    src.events.append(
        pysubs2.SSAEvent(start=1000, end=3000, text="The sword shines.", style="White")
    )
    src.events.append(
        pysubs2.SSAEvent(start=3100, end=5000, text="The sword glows.", style="White")
    )
    source_path = tmp_path / "ep01.en.ass"
    src.save(str(source_path))

    translated = pysubs2.SSAFile()
    translated.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="La Sword brilla."))
    translated.events.append(pysubs2.SSAEvent(start=3100, end=5000, text="La Sword brilla."))
    translated_path = tmp_path / "ep01.es.srt"
    translated.save(str(translated_path), format_="srt")

    project_dir = tmp_path / "projects" / "Serie" / "es-latam"
    project_dir.mkdir(parents=True)
    (project_dir / "glossary.json").write_text(json.dumps({"Sword": "Espada"}), encoding="utf-8")

    def fake_runner(prompt: str) -> str:
        return json.dumps(
            [
                {  # only the term changes -> surgical, applied
                    "scope": "line",
                    "id": "0001",
                    "kind": "glossary",
                    "message": "use glossary",
                    "current": "La Sword brilla.",
                    "suggested": "La Espada brilla.",
                    "auto_safe": True,
                },
                {  # term changes AND a surrounding word is reworded -> rejected, left for a human
                    "scope": "line",
                    "id": "0002",
                    "kind": "glossary",
                    "message": "use glossary",
                    "current": "La Sword brilla.",
                    "suggested": "La Espada reluce.",
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

    reloaded = pysubs2.load(str(translated_path))
    assert reloaded.events[0].plaintext == "La Espada brilla."  # single-span term fix applied
    assert reloaded.events[1].plaintext == "La Sword brilla."  # multi-span rewrite rejected
    assert result.n_applied == 1


def test_review_apply_does_not_trip_modified_output(tmp_path, monkeypatch):
    # Regression: `review --apply` legitimately edits the output; the run must re-bless its manifest
    # so a later `translate` does not mistake that edit for a hand-edit (ModifiedOutputError).
    from translate_subs.workflows.models import OutputExistsError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.styles["White"] = pysubs2.SSAStyle()
    src.events.append(
        pysubs2.SSAEvent(start=1000, end=3000, text="The Sword shines.", style="White")
    )
    source_path = tmp_path / "ep.en.ass"
    src.save(str(source_path))

    # Seed the glossary before translating so the manifest's memory_hash already reflects it;
    # otherwise the later re-translate would trip StaleOutputError (memory changed), not the
    # output-hash axis this test exercises.
    proj = tmp_path / "projects" / "Serie" / "es-latam"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "glossary.json").write_text(json.dumps({"Sword": "Espada"}), encoding="utf-8")

    tkw = dict(provider="identity", interactive=False, fmt="srt", project="Serie")
    result = pipeline.translate_subtitle(source_path, **tkw)  # writes output + manifest

    def fake_runner(prompt: str) -> str:
        return json.dumps(
            [
                {
                    "scope": "line",
                    "id": "0001",
                    "kind": "glossary",
                    "message": "g",
                    "current": "The Sword shines.",
                    "suggested": "The Espada shines.",
                    "auto_safe": True,
                }
            ]
        )

    r = pipeline.review_translation(
        source_path,
        result.output_path,
        project="Serie",
        interactive=False,
        apply=True,
        runner=fake_runner,
    )
    assert r.n_applied == 1  # the fix modified the output file

    # The output was changed by our own tool, not by hand: re-translating skips, never "modified".
    with pytest.raises(OutputExistsError):
        pipeline.translate_subtitle(source_path, **tkw)


def _gender_fix_review_fixture(tmp_path, monkeypatch):
    """A source/translation pair plus memory where one confirmed-gender safe fix is available."""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = pysubs2.SSAFile()
    src.styles["White"] = pysubs2.SSAStyle()
    e1 = pysubs2.SSAEvent(start=1000, end=3000, text="I'm tired of this.", style="White")
    e1.name = (
        "Yumi"  # ASS keeps the speaker name; SRT would drop it and the gender fix couldn't map
    )
    src.events.append(e1)
    source_path = tmp_path / "ep01.en.ass"
    src.save(str(source_path))

    translated = pysubs2.SSAFile()
    translated.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Estoy cansado de esto."))
    translated_path = tmp_path / "ep01.es.srt"
    translated.save(str(translated_path), format_="srt")

    project_dir = tmp_path / "projects" / "Serie" / "es-latam"
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
                }
            ]
        )

    return source_path, translated_path, fake_runner


def test_review_apply_confirm_declined_writes_nothing(tmp_path, monkeypatch):
    source_path, translated_path, fake_runner = _gender_fix_review_fixture(tmp_path, monkeypatch)

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=True,
        confirm=lambda changes: False,
        runner=fake_runner,
    )

    assert result.n_applied == 0
    assert result.applied_fixes == []
    assert result.planned_fixes  # the fix was found, just not written
    reloaded = pysubs2.load(str(translated_path))
    assert reloaded.events[0].plaintext == "Estoy cansado de esto."  # untouched


def test_review_apply_confirm_receives_planned_and_writes(tmp_path, monkeypatch):
    source_path, translated_path, fake_runner = _gender_fix_review_fixture(tmp_path, monkeypatch)
    seen: list[tuple[str, str, str]] = []

    def confirm(changes):
        seen.extend(changes)
        return True

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        apply=True,
        confirm=confirm,
        runner=fake_runner,
    )

    assert seen == [("0001", "Estoy cansado de esto.", "Estoy cansada de esto.")]
    assert result.n_applied == 1
    reloaded = pysubs2.load(str(translated_path))
    assert reloaded.events[0].plaintext == "Estoy cansada de esto."


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
    # No LLM ran, so the report must not claim a provider/model.
    assert "Provider: (none)" in result.report_path.read_text("utf-8")


def test_review_no_llm_reports_no_provider(tmp_path, monkeypatch):
    # --no-llm runs only deterministic checks; the manifest must say no backend was used and
    # must not name the (untouched) provider/model.
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hello."))
    source_path = tmp_path / "ep.en.srt"
    src.save(str(source_path), format_="srt")
    tr = pysubs2.SSAFile()
    tr.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hola."))
    translated_path = tmp_path / "ep.es.srt"
    tr.save(str(translated_path), format_="srt")

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        use_llm=False,
        provider="claude",
    )
    text = result.report_path.read_text("utf-8")
    assert "Provider: (none)" in text
    assert "Model: (none)" in text


def test_review_records_effective_model_default(tmp_path, monkeypatch):
    # When no --model is given, the manifest records the resolved default, not "(default)".
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hello."))
    source_path = tmp_path / "ep.en.srt"
    src.save(str(source_path), format_="srt")
    tr = pysubs2.SSAFile()
    tr.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hola."))
    translated_path = tmp_path / "ep.es.srt"
    tr.save(str(translated_path), format_="srt")

    result = pipeline.review_translation(
        source_path,
        translated_path,
        project="Serie",
        interactive=False,
        provider="claude",
        runner=lambda _: "[]",
    )
    assert "Model: claude-opus-4-8" in result.report_path.read_text("utf-8")


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
