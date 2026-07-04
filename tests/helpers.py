"""Shared plain-function helpers for the test suite (fixtures live in conftest.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pysubs2

from translate_subs.pipeline import TranslateResult


def one_line_srt(path):
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hi."))
    subs.save(str(path), format_="srt")


def fake_translate_result(tmp_path, untranslated):
    out = tmp_path / "ep.es.ass"
    out.write_text("", encoding="utf-8")
    source = SimpleNamespace(
        was_extracted=False,
        track=None,
        subtitle_path=out,
        origin=out,
        lang_fallback=False,
        selected_lang="en",
    )
    validation = SimpleNamespace(ok=True, warnings=[], errors=[])
    return TranslateResult(
        source=source,
        output_path=out,
        n_units=1,
        n_jobs=1,
        output_validation=validation,
        context_used=False,
        memory_used=False,
        untranslated_ids=list(untranslated),
    )
