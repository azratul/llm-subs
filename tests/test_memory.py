from __future__ import annotations

import json

import pytest

from translate_subs.ai.analysis import EpisodeCharacter, EpisodeContext
from translate_subs.memory.compact import compact_project_memory
from translate_subs.memory.merge import Conflict, merge_episode_context
from translate_subs.memory.models import CharacterMemory, SeriesMemory, StyleGuide
from translate_subs.memory.rules import build_memory_rules, rules_for_text, translation_rules
from translate_subs.memory.store import ProjectMemory


def _ctx(**kw) -> EpisodeContext:
    return EpisodeContext(**kw)


def test_store_round_trip(tmp_path):
    pm = ProjectMemory(tmp_path)
    pm.memory.characters.append(CharacterMemory(name="Akira", gender="male"))
    pm.glossary["Shadow Core"] = "Núcleo Sombrío"
    pm.save()

    assert (tmp_path / "memory.json").exists()
    assert (tmp_path / "glossary.json").exists()
    assert (tmp_path / "style_guide.json").exists()
    assert json.loads((tmp_path / "memory.json").read_text())["schema_version"] == 1
    assert json.loads((tmp_path / "glossary.json").read_text())["schema_version"] == 1
    assert json.loads((tmp_path / "style_guide.json").read_text())["schema_version"] == 1

    reloaded = ProjectMemory.load(tmp_path)
    assert reloaded.glossary["Shadow Core"] == "Núcleo Sombrío"
    assert reloaded.memory.find("akira").gender == "male"


def test_merge_applies_safe_updates():
    memory = SeriesMemory()
    glossary: dict[str, str] = {}
    ctx = _ctx(
        characters=[
            EpisodeCharacter(name="Akira", gender="male", relationships={"Yumi": "friend"})
        ],
        glossary={"Shadow Core": "Núcleo Sombrío"},
    )
    report = merge_episode_context(memory, glossary, ctx, policy="flag")
    assert not report.conflicts
    assert glossary == {"Shadow Core": "Núcleo Sombrío"}
    assert memory.find("Akira").gender == "male"
    assert memory.find("Akira").relationships == {"Yumi": "friend"}


def test_merge_fills_unknown_gender_without_conflict():
    memory = SeriesMemory(characters=[CharacterMemory(name="Akira", gender="unknown")])
    ctx = _ctx(characters=[EpisodeCharacter(name="akira", gender="female")])
    report = merge_episode_context(memory, {}, ctx, policy="flag")
    assert not report.conflicts
    assert memory.find("Akira").gender == "female"


def test_glossary_conflict_is_flagged_not_overwritten():
    glossary = {"Shadow Core": "Núcleo Sombrío"}
    ctx = _ctx(glossary={"Shadow Core": "Corazón de las Sombras"})
    report = merge_episode_context(SeriesMemory(), glossary, ctx, policy="flag")
    assert glossary["Shadow Core"] == "Núcleo Sombrío"  # unchanged
    assert len(report.conflicts) == 1
    assert report.conflicts[0].kind == "glossary"


def test_glossary_trivial_punctuation_diff_is_not_a_conflict():
    glossary = {"Episode Title": "El ardor del amor juvenil"}
    ctx = _ctx(glossary={"Episode Title": "El ardor del amor juvenil."})
    report = merge_episode_context(SeriesMemory(), glossary, ctx, policy="flag")
    assert not report.conflicts
    assert glossary["Episode Title"] == "El ardor del amor juvenil"  # first wins, unchanged


def test_gender_conflict_policies():
    def base():
        return SeriesMemory(characters=[CharacterMemory(name="Akira", gender="male")])

    ctx = _ctx(characters=[EpisodeCharacter(name="Akira", gender="female")])

    keep = base()
    r = merge_episode_context(keep, {}, ctx, policy="keep")
    assert keep.find("Akira").gender == "male" and not r.conflicts

    flag = base()
    r = merge_episode_context(flag, {}, ctx, policy="flag")
    assert flag.find("Akira").gender == "male" and len(r.conflicts) == 1

    over = base()
    r = merge_episode_context(over, {}, ctx, policy="overwrite")
    assert over.find("Akira").gender == "female"


def test_ask_policy_uses_resolver():
    memory = SeriesMemory(characters=[CharacterMemory(name="Akira", gender="male")])
    ctx = _ctx(characters=[EpisodeCharacter(name="Akira", gender="female")])

    seen: list[Conflict] = []

    def resolver(c: Conflict) -> bool:
        seen.append(c)
        return True  # overwrite

    report = merge_episode_context(memory, {}, ctx, policy="ask", resolver=resolver)
    assert memory.find("Akira").gender == "female"
    assert seen and not report.conflicts


def test_append_conflicts_accumulates(tmp_path):
    pm = ProjectMemory(tmp_path)
    pm.append_conflicts([{"kind": "glossary", "key": "A", "existing": "1", "suggested": "2"}])
    pm.append_conflicts(
        [{"kind": "gender", "key": "Akira", "existing": "male", "suggested": "female"}]
    )
    data = json.loads((tmp_path / "conflicts.json").read_text("utf-8"))
    assert data["schema_version"] == 1
    assert len(data["conflicts"]) == 2


def test_append_conflicts_dedups_identical_records(tmp_path):
    pm = ProjectMemory(tmp_path)
    rec = {"kind": "gender", "key": "Akira", "existing": "male", "suggested": "female"}
    pm.append_conflicts([rec])
    pm.append_conflicts([rec])  # same unresolved conflict recurs next episode
    data = json.loads((tmp_path / "conflicts.json").read_text("utf-8"))
    assert len(data["conflicts"]) == 1


def test_store_loads_legacy_glossary_and_conflicts(tmp_path):
    (tmp_path / "glossary.json").write_text('{"Tokyo": "Tokio"}', encoding="utf-8")
    (tmp_path / "conflicts.json").write_text(
        '[{"kind":"glossary","key":"Tokyo","existing":"Tokyo","suggested":"Tokio"}]',
        encoding="utf-8",
    )

    pm = ProjectMemory.load(tmp_path)
    assert pm.glossary == {"Tokyo": "Tokio"}
    assert pm.load_conflicts()[0]["key"] == "Tokyo"


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("glossary.json", '["not", "a", "mapping"]'),
        ("glossary.json", '{"Tokyo": 123}'),
        (
            "conflicts.json",
            '[{"kind":"unknown","key":"X","existing":"a","suggested":"b"}]',
        ),
    ],
)
def test_store_rejects_invalid_persisted_schemas(tmp_path, filename, payload):
    (tmp_path / filename).write_text(payload, encoding="utf-8")
    if filename == "glossary.json":
        with pytest.raises(ValueError, match=filename):
            ProjectMemory.load(tmp_path)
    else:
        pm = ProjectMemory.load(tmp_path)
        with pytest.raises(ValueError, match=filename):
            pm.load_conflicts()


def test_relationship_paraphrase_never_conflicts():
    memory = SeriesMemory()
    glossary: dict[str, str] = {}
    short = _ctx(characters=[EpisodeCharacter(name="Akira", relationships={"Yumi": "amiga"})])
    merge_episode_context(memory, glossary, short, policy="flag")
    # A reworded, more informative description must not be flagged; the richer one wins.
    long = _ctx(
        characters=[
            EpisodeCharacter(name="Akira", relationships={"Yumi": "amiga cercana de la infancia"})
        ]
    )
    report = merge_episode_context(memory, glossary, long, policy="flag")
    assert not report.conflicts
    assert memory.find("Akira").relationships == {"Yumi": "amiga cercana de la infancia"}


def test_translation_rules_series_precedence():
    pm = ProjectMemory(
        project_dir=".",
        memory=SeriesMemory(characters=[CharacterMemory(name="Akira", gender="male")]),
        glossary={"Shadow Core": "Núcleo Sombrío"},
        style_guide=StyleGuide(locale="es-latam"),
    )
    ctx = _ctx(
        glossary={"Shadow Core": "Corazón de las Sombras"},  # episode disagrees
        characters=[EpisodeCharacter(name="Yumi", gender="female")],
        translation_rules=["Keep it punchy."],
    )
    rules = translation_rules(pm, ctx)
    joined = "\n".join(rules)
    assert "Shadow Core -> Núcleo Sombrío" in joined  # series wins
    assert "Corazón de las Sombras" not in joined
    assert "Akira: male" in joined and "Yumi: female" in joined
    assert "Keep it punchy." in joined
    assert "Target locale/variant: es-latam." in joined


def test_style_guide_locale_optional_for_any_target():
    pm = ProjectMemory(project_dir=".", style_guide=StyleGuide())  # default: no locale
    joined = "\n".join(translation_rules(pm, None))
    assert "Target locale" not in joined  # language comes from --target, not the style guide


def test_build_memory_rules_prunes_identity_glossary():
    pm = ProjectMemory(
        project_dir=".",
        glossary={"Shadow Core": "Núcleo Sombrío", "Tokyo": "Tokyo"},
        style_guide=StyleGuide(locale="es-latam"),
    )
    mr = build_memory_rules(pm, None)
    assert mr.glossary == {"Shadow Core": "Núcleo Sombrío"}  # identity mapping dropped


def test_rules_for_text_keeps_only_referenced_memory():
    pm = ProjectMemory(
        project_dir=".",
        memory=SeriesMemory(
            characters=[
                CharacterMemory(name="Akira", gender="male", relationships={"Yumi": "friend"}),
                CharacterMemory(name="Hikaru", gender="female"),
            ]
        ),
        glossary={"Shadow Core": "Núcleo Sombrío", "Orange Road": "Camino Naranja"},
        style_guide=StyleGuide(locale="es-latam"),
    )
    mr = build_memory_rules(pm, None)

    # Block mentions only Akira + Shadow Core.
    joined = "\n".join(rules_for_text(mr, "Akira drew the Shadow Core.", ["Akira"]))
    assert "Shadow Core -> Núcleo Sombrío" in joined
    assert "Orange Road" not in joined  # absent glossary term excluded
    assert "Akira: male" in joined
    assert "Akira-Yumi: friend" in joined
    assert "Hikaru" not in joined  # absent character excluded
    assert "Target locale/variant: es-latam." in joined  # base always present

    # Block mentions nothing known: only base rules.
    bare = "\n".join(rules_for_text(mr, "Hello there.", []))
    assert "Shadow Core" not in bare
    assert "Grammatical gender" not in bare
    assert "Target locale/variant: es-latam." in bare


def test_rules_use_speech_style_and_episode_summary():
    from translate_subs.ai.analysis import EpisodeCharacter, EpisodeContext

    pm = ProjectMemory(
        project_dir=".",
        memory=SeriesMemory(
            characters=[CharacterMemory(name="Akira", gender="male", speech_style="blunt, casual")]
        ),
        style_guide=StyleGuide(locale="es-latam"),
    )
    ctx = EpisodeContext(
        episode_summary="Akira confronts his rival at the festival.",
        characters=[EpisodeCharacter(name="Yumi", gender="female", speech_style="polite, formal")],
    )
    mr = build_memory_rules(pm, ctx)

    # The summary rides in base (always sent), even for a block mentioning nobody known.
    bare = "\n".join(rules_for_text(mr, "Hello there.", []))
    assert "Episode context: Akira confronts his rival" in bare
    assert "Speech style" not in bare  # no referenced character -> no speech-style line

    # Speech style is relevance-filtered like gender: only the referenced character's appears.
    akira = "\n".join(rules_for_text(mr, "Akira spoke first.", ["Akira"]))
    assert "Speech style by character: Akira: blunt, casual." in akira
    assert "Yumi" not in akira


def test_compact_project_memory_prunes_and_merges():
    pm = ProjectMemory(
        project_dir=".",
        memory=SeriesMemory(
            characters=[
                CharacterMemory(name="Akira", gender="male"),
                CharacterMemory(name="akira", relationships={"Yumi": "friend"}),  # case dup
                CharacterMemory(name="Extra"),  # unknown gender, no info -> removed
            ]
        ),
        glossary={"Shadow Core": "Núcleo Sombrío", "Tokyo": "Tokyo", "shadow core": "X"},
    )
    report = compact_project_memory(pm)

    assert report.removed_identity_terms == 1
    assert report.removed_duplicate_terms == 1
    assert pm.glossary == {"Shadow Core": "Núcleo Sombrío"}

    assert report.merged_characters == 1
    assert report.removed_empty_characters == 1
    assert [c.name for c in pm.memory.characters] == ["Akira"]
    assert pm.memory.characters[0].relationships == {"Yumi": "friend"}  # merged from the dup


def test_resolve_conflicts_applies_and_drops(tmp_path, monkeypatch):
    from translate_subs import config
    from translate_subs.pipeline import resolve_conflicts

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    pdir = tmp_path / "Serie" / "es"  # per-target memory root (default target es-latam)
    pm = ProjectMemory(
        pdir,
        memory=SeriesMemory(characters=[CharacterMemory(name="Akira", gender="male")]),
        glossary={"Kasuga Residence": "residencia Kasuga"},
    )
    pm.save()
    pm.write_conflicts(
        [
            {
                "kind": "glossary",
                "key": "Kasuga Residence",
                "existing": "residencia Kasuga",
                "suggested": "residencia de la familia Kasuga",
            },
            {"kind": "gender", "key": "Akira", "existing": "male", "suggested": "female"},
            {"kind": "glossary", "key": "Tokyo", "existing": "Tokyo", "suggested": "Tokio"},
        ]
    )

    decisions = iter(["use", "keep", "skip"])
    result = resolve_conflicts("Serie", lambda c: next(decisions))

    assert (result.resolved, result.remaining) == (2, 1)
    reloaded = ProjectMemory.load(pdir)
    assert reloaded.glossary["Kasuga Residence"] == "residencia de la familia Kasuga"  # used
    assert reloaded.memory.find("Akira").gender == "male"  # kept stored, not overwritten
    leftover = reloaded.load_conflicts()
    assert len(leftover) == 1 and leftover[0]["key"] == "Tokyo"  # skipped stays in log
