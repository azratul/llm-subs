"""Output manifest provenance: staleness on every recorded axis, hand-edit protection."""

from __future__ import annotations

import pysubs2
import pytest

from translate_subs import config, pipeline


def _srt_with(path, text, *, start=0, end=2000):
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=start, end=end, text=text))
    subs.save(str(path), format_="srt")


def test_translate_writes_manifest_and_reports_output_exists_when_unchanged(tmp_path, monkeypatch):
    from translate_subs.workflows.models import OutputExistsError
    from translate_subs.workflows.output_manifest import OutputManifest

    projects = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")

    pipeline.translate_subtitle(source, **kw)
    manifests = list(projects.rglob("*.manifest.json"))
    assert len(manifests) == 1
    saved = OutputManifest.model_validate_json(manifests[0].read_text("utf-8"))
    assert saved.provider == "identity" and saved.target == "es-latam" and saved.source_hash

    # Re-running with the same source/settings is up to date -> skip, not stale.
    with pytest.raises(OutputExistsError):
        pipeline.translate_subtitle(source, **kw)


def test_changed_source_reports_stale_and_force_refreshes(tmp_path, monkeypatch):
    from translate_subs.workflows.models import OutputExistsError, StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)

    _srt_with(source, "A completely different line.")
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **kw)

    # --force ignores staleness, rewrites, and refreshes the manifest to the new source.
    pipeline.translate_subtitle(source, force=True, **kw)
    with pytest.raises(OutputExistsError):
        pipeline.translate_subtitle(source, **kw)


def _ass_with_drawing(path, dialogue, drawing):
    subs = pysubs2.SSAFile()
    subs.styles["White"] = pysubs2.SSAStyle()
    subs.events.append(pysubs2.SSAEvent(start=1000, end=3000, text=dialogue, style="White"))
    subs.events.append(pysubs2.SSAEvent(start=3100, end=5000, text=drawing, style="White"))
    subs.save(str(path))


def test_changed_preserved_ass_event_reports_stale(tmp_path, monkeypatch):
    # A non-translatable event (a drawing) that .ass copies through verbatim must flag the output
    # stale when edited, even though it never becomes a translatable unit.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.ass"
    _ass_with_drawing(source, "Hello.", r"{\p1}m 0 0 l 10 0 10 10 0 10{\p0}")
    kw = dict(provider="identity", interactive=False, fmt="ass", project="P")
    pipeline.translate_subtitle(source, **kw)

    # Same dialogue, different drawing geometry: the translation is unchanged but the .ass output no
    # longer matches the source.
    _ass_with_drawing(source, "Hello.", r"{\p1}m 0 0 l 20 0 20 20 0 20{\p0}")
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **kw)


def test_changed_ass_style_definition_reports_stale(tmp_path, monkeypatch):
    # A restyle (font size) changes how every line using that style renders, even though no line's
    # text/timing changes. The .ass output must be flagged stale.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.ass"
    kw = dict(provider="identity", interactive=False, fmt="ass", project="P")

    subs = pysubs2.SSAFile()
    subs.styles["White"] = pysubs2.SSAStyle(fontsize=40)
    subs.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hello.", style="White"))
    subs.save(str(source))
    pipeline.translate_subtitle(source, **kw)

    subs.styles["White"].fontsize = 72
    subs.save(str(source))
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **kw)


def test_changed_script_info_reports_stale(tmp_path, monkeypatch):
    # PlayResX/PlayResY rescale every coordinate in the script, so a resolution change alters the
    # render without touching any line. The .ass output must be flagged stale.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.ass"
    kw = dict(provider="identity", interactive=False, fmt="ass", project="P")

    subs = pysubs2.SSAFile()
    subs.styles["White"] = pysubs2.SSAStyle()
    subs.info["PlayResX"] = "640"
    subs.info["PlayResY"] = "480"
    subs.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hello.", style="White"))
    subs.save(str(source))
    pipeline.translate_subtitle(source, **kw)

    subs.info["PlayResX"] = "1280"
    subs.info["PlayResY"] = "720"
    subs.save(str(source))
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **kw)


def test_changed_embedded_graphic_reports_stale(tmp_path, monkeypatch):
    # An embedded [Graphics] attachment is preserved in the .ass output; changing its content must
    # flag the output stale, exactly like fonts and drawings.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.ass"
    kw = dict(provider="identity", interactive=False, fmt="ass", project="P")

    subs = pysubs2.SSAFile()
    subs.styles["White"] = pysubs2.SSAStyle()
    subs.graphics_opaque["logo.bmp"] = ["QUJDREVG"]
    subs.events.append(pysubs2.SSAEvent(start=1000, end=3000, text="Hello.", style="White"))
    subs.save(str(source))
    pipeline.translate_subtitle(source, **kw)

    subs.graphics_opaque["logo.bmp"] = ["R0hJSktM"]
    subs.save(str(source))
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **kw)


def test_changed_ass_event_margin_reports_stale(tmp_path, monkeypatch):
    # An event-level layout change (vertical margin) repositions the line; the .ass output no longer
    # matches the source and must be flagged stale.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.ass"
    kw = dict(provider="identity", interactive=False, fmt="ass", project="P")

    subs = pysubs2.SSAFile()
    subs.styles["White"] = pysubs2.SSAStyle()
    subs.events.append(
        pysubs2.SSAEvent(start=1000, end=3000, text="Hello.", style="White", marginv=10)
    )
    subs.save(str(source))
    pipeline.translate_subtitle(source, **kw)

    subs.events[0].marginv = 200
    subs.save(str(source))
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **kw)


def test_changed_preserved_event_does_not_affect_srt(tmp_path, monkeypatch):
    # .srt prunes non-translatable events, so a drawing change must NOT flag an .srt output stale
    # (the drawing never reaches the .srt): the digest stays units-only and the run is skipped.
    from translate_subs.workflows.models import OutputExistsError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.ass"
    _ass_with_drawing(source, "Hello.", r"{\p1}m 0 0 l 10 0 10 10 0 10{\p0}")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)

    _ass_with_drawing(source, "Hello.", r"{\p1}m 0 0 l 20 0 20 20 0 20{\p0}")
    with pytest.raises(OutputExistsError):
        pipeline.translate_subtitle(source, **kw)


def test_ass_and_srt_outputs_get_independent_manifests(tmp_path, monkeypatch):
    # Regression: a single per-episode manifest was shared by every artifact, so force-refreshing
    # one format silently marked the other up to date. Each output must track its own provenance.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    ass_kw = dict(provider="identity", interactive=False, fmt="ass", project="P")
    srt_kw = dict(provider="identity", interactive=False, fmt="srt", project="P")

    pipeline.translate_subtitle(source, **ass_kw)
    pipeline.translate_subtitle(source, **srt_kw)
    manifests = list((tmp_path / "projects").rglob("*.manifest.json"))
    assert len(manifests) == 2  # one per artifact, not one shared

    # Edit the source and force-refresh only the .ass: the .srt manifest must stay on the old
    # source and still report stale, instead of being masked by the .ass refresh.
    _srt_with(source, "A completely different line.")
    pipeline.translate_subtitle(source, force=True, **ass_kw)
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **srt_kw)


def test_same_basename_different_dirs_get_independent_manifests(tmp_path, monkeypatch):
    # The manifest is keyed on the resolved output path, not the basename, so the same filename
    # written to two directories doesn't collapse onto one shared manifest.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    a = tmp_path / "A" / "ep.es-latam.srt"
    b = tmp_path / "B" / "ep.es-latam.srt"
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")

    pipeline.translate_subtitle(source, output=a, **kw)
    pipeline.translate_subtitle(source, output=b, **kw)
    assert len(list((tmp_path / "projects").rglob("*.manifest.json"))) == 2

    _srt_with(source, "A completely different line.")
    pipeline.translate_subtitle(source, output=a, force=True, **kw)  # refresh only A
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, output=b, **kw)  # B still stale, not masked by A


def test_manifest_records_version_and_output_hash(tmp_path, monkeypatch):
    from translate_subs.workflows.output_manifest import OutputManifest

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    pipeline.translate_subtitle(
        source, provider="identity", interactive=False, fmt="srt", project="P"
    )
    manifest = next((tmp_path / "projects").rglob("*.manifest.json"))
    saved = OutputManifest.model_validate_json(manifest.read_text("utf-8"))
    assert saved.output_hash  # the produced file's content hash was recorded
    assert hasattr(saved, "tool_version")  # version recorded (may be "" from an uninstalled tree)


def test_hand_edited_output_is_protected(tmp_path, monkeypatch):
    from translate_subs.workflows.models import ModifiedOutputError, OutputExistsError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    result = pipeline.translate_subtitle(source, **kw)

    with pytest.raises(OutputExistsError):  # unchanged output -> plain skip
        pipeline.translate_subtitle(source, **kw)

    # Hand-edit the output; a re-run must refuse to clobber it.
    edited = result.output_path.read_text("utf-8") + "\n\n99\n00:00:09,000 --> 00:00:10,000\nnote\n"
    result.output_path.write_text(edited, "utf-8")
    with pytest.raises(ModifiedOutputError):
        pipeline.translate_subtitle(source, **kw)

    # --force overwrites and re-records the hash, so a following run skips again.
    pipeline.translate_subtitle(source, force=True, **kw)
    with pytest.raises(OutputExistsError):
        pipeline.translate_subtitle(source, **kw)


def test_corrupt_manifest_is_surfaced_not_skipped(tmp_path, monkeypatch):
    # A manifest that exists but is unreadable must not be silently treated as "up to date" (skip);
    # we can't verify freshness, so it is surfaced as stale rather than hidden.
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)

    manifest = next((tmp_path / "projects").rglob("*.manifest.json"))
    manifest.write_text("{ this is not valid json", "utf-8")
    with pytest.raises(StaleOutputError, match="unreadable"):
        pipeline.translate_subtitle(source, **kw)


def test_batch_records_modified_status(tmp_path):
    from translate_subs.workflows.models import ModifiedOutputError
    from translate_subs.workflows.translation import batch_translate

    def discover(*_a, **_k):
        return [tmp_path / "a.mkv"]

    def modified(_path, **_k):
        raise ModifiedOutputError("edited by hand")

    res = batch_translate(tmp_path, discover_inputs_fn=discover, translate_fn=modified)
    assert res.n_modified == 1 and res.n_failed == 0 and res.n_translated == 0


def test_project_status_ignores_legacy_and_corrupt_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)

    ep_dir = next((tmp_path / "projects").rglob("*.manifest.json")).parent
    # A legacy manifest (fixed name, no recorded output) and a corrupt one must not appear as
    # phantom outputs in the status view.
    (ep_dir / "output.manifest.json").write_text(
        '{"source_hash":"x","target":"es-latam","provider":"identity","model":""}', "utf-8"
    )
    (ep_dir / "garbage.manifest.json").write_text("{not valid json", "utf-8")

    status = pipeline.project_status("P", "es-latam")
    outputs = status.episodes[0].outputs
    assert len(outputs) == 1 and outputs[0].endswith("ep.es-latam.srt")


def test_changed_model_reports_stale(tmp_path, monkeypatch):
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)  # model unset -> recorded as ""

    with pytest.raises(StaleOutputError, match="provider/model"):
        pipeline.translate_subtitle(source, model="some-model", **kw)


def test_changed_timing_reports_stale(tmp_path, monkeypatch):
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.", start=0, end=2000)
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)

    # Same text, different timing: the translation is unchanged but the output is now desynced.
    _srt_with(source, "Hello.", start=5000, end=7000)
    with pytest.raises(StaleOutputError, match="source"):
        pipeline.translate_subtitle(source, **kw)


def test_changed_reasoning_reports_stale(tmp_path, monkeypatch):
    from translate_subs.workflows.models import StaleOutputError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)  # reasoning unset -> recorded as ""

    with pytest.raises(StaleOutputError, match="reasoning"):
        pipeline.translate_subtitle(source, reasoning="high", **kw)


def test_changed_memory_reports_stale(tmp_path, monkeypatch):
    from translate_subs.memory.store import ProjectMemory
    from translate_subs.workflows.models import StaleOutputError
    from translate_subs.workflows.support import memory_root

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello Yumi.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)

    # Editing the series glossary changes the prompt but not the source: the output is now stale.
    pm = ProjectMemory.load(memory_root("P", "es-latam"))
    pm.glossary["Yumi"] = "Yumi-chan"
    pm.save()
    with pytest.raises(StaleOutputError, match="memory"):
        pipeline.translate_subtitle(source, **kw)


def test_legacy_manifest_without_memory_hash_not_flagged(tmp_path, monkeypatch):
    from translate_subs.workflows.models import OutputExistsError
    from translate_subs.workflows.output_manifest import OutputManifest, write_manifest

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    kw = dict(provider="identity", interactive=False, fmt="srt", project="P")
    pipeline.translate_subtitle(source, **kw)

    # Rewrite the manifest as a pre-memory_hash release would have (field empty): a later run
    # computes a real digest, but the stored empty value must not spuriously flag the output.
    manifests = list((tmp_path / "projects").rglob("*.manifest.json"))
    stored = OutputManifest.model_validate_json(manifests[0].read_text("utf-8"))
    write_manifest(manifests[0], stored.model_copy(update={"memory_hash": ""}))
    with pytest.raises(OutputExistsError):
        pipeline.translate_subtitle(source, **kw)


def test_is_stale_tolerates_legacy_empty_memory_hash():
    from translate_subs.workflows.output_manifest import OutputManifest, is_stale

    base = dict(source_hash="s", target="es-latam", provider="identity", model="")
    current = OutputManifest(**base, memory_hash="new")
    # A stored manifest that never recorded a memory hash is not a change...
    assert not is_stale(OutputManifest(**base, memory_hash=""), current)
    # ...but once one was recorded, a different value flags the output stale.
    assert is_stale(OutputManifest(**base, memory_hash="old"), current)


def test_legacy_output_without_manifest_reports_output_exists(tmp_path, monkeypatch):
    from translate_subs.workflows.models import OutputExistsError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _srt_with(source, "Hello.")
    # Output from an older version: present but with no manifest beside it.
    _srt_with(tmp_path / "ep.es-latam.srt", "Hola.")

    with pytest.raises(OutputExistsError):  # absent manifest -> treated as up to date, not stale
        pipeline.translate_subtitle(
            source, provider="identity", interactive=False, fmt="srt", project="P"
        )
