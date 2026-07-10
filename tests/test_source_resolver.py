from __future__ import annotations

from pathlib import Path

import pytest

from translate_subs.io.media_probe import SubtitleTrack
from translate_subs.io.source_resolver import SourceError, resolve_source, select_track


def _track(
    rel: int,
    *,
    codec: str = "subrip",
    lang: str | None = None,
    default: bool = False,
    forced: bool = False,
) -> SubtitleTrack:
    return SubtitleTrack(
        rel_index=rel,
        stream_index=rel + 2,
        codec=codec,
        language=lang,
        title=None,
        default=default,
        forced=forced,
    )


def test_resolve_direct_subtitle(tmp_path):
    subtitle = tmp_path / "episode.en.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    result = resolve_source(subtitle, work_dir=tmp_path / "work")
    assert result.subtitle_path == subtitle
    assert result.origin == subtitle
    assert not result.was_extracted


def test_resolve_rejects_directory(tmp_path):
    with pytest.raises(SourceError, match="received a directory"):
        resolve_source(tmp_path, work_dir=tmp_path / "work")


def test_resolve_sidecar_before_embedded(tmp_path, monkeypatch):
    media = tmp_path / "episode.mkv"
    media.touch()
    sidecar = tmp_path / "episode.eng.ass"
    sidecar.write_text("[Events]\n", encoding="utf-8")

    monkeypatch.setattr(
        "translate_subs.io.source_resolver.probe_subtitle_tracks",
        lambda _: pytest.fail("ffprobe should not run when a sidecar exists"),
    )
    result = resolve_source(media, work_dir=tmp_path / "work", interactive=False)
    assert result.subtitle_path == sidecar
    assert result.origin == media


def test_noninteractive_track_selection_avoids_forced_language_match():
    tracks = [
        _track(0, lang="eng", forced=True),
        _track(1, lang="spa", default=True),
        _track(2, lang="eng"),
    ]
    assert select_track(tracks, lang="eng", track_index=None, interactive=False).rel_index == 2


def test_noninteractive_track_selection_uses_default_then_order():
    tracks = [_track(0, lang="jpn"), _track(1, lang="spa", default=True)]
    assert select_track(tracks, lang="eng", track_index=None, interactive=False).rel_index == 1


def test_explicit_track_and_image_rejection():
    tracks = [_track(0, lang="eng"), _track(1, lang="spa")]
    assert select_track(tracks, lang=None, track_index=1, interactive=False).rel_index == 1

    # The rejection must tell the user the way out (OCR / text sidecar), not just refuse.
    with pytest.raises(SourceError, match="Only image subtitle tracks.*OCR.*sidecar"):
        select_track(
            [_track(0, codec="hdmv_pgs_subtitle")],
            lang="eng",
            track_index=None,
            interactive=False,
        )


def test_interactive_track_selection_rejects_non_numeric_input():
    tracks = [_track(0, lang="eng"), _track(1, lang="spa")]
    with pytest.raises(SourceError, match="Not a track index"):
        select_track(tracks, lang=None, track_index=None, interactive=True, prompt=lambda _: "abc")


def test_embedded_track_is_extracted(tmp_path, monkeypatch):
    media = tmp_path / "episode.mkv"
    media.touch()
    track = _track(0, lang="eng")
    extracted = tmp_path / "work" / "episode.track0.srt"

    monkeypatch.setattr(
        "translate_subs.io.source_resolver.probe_subtitle_tracks", lambda _: [track]
    )

    def fake_extract(media_path: Path, selected: SubtitleTrack, work_dir: Path) -> Path:
        assert media_path == media
        assert selected == track
        extracted.parent.mkdir(parents=True)
        extracted.touch()
        return extracted

    monkeypatch.setattr("translate_subs.io.source_resolver.extract_track", fake_extract)
    result = resolve_source(media, work_dir=tmp_path / "work", interactive=False)
    assert result.subtitle_path == extracted
    assert result.was_extracted
    assert result.track == track


def test_explicit_track_overrides_sidecar(tmp_path, monkeypatch):
    # When --track is given the sidecar must be ignored, even if it matches the language,
    # because the user explicitly asked for a specific embedded track.
    media = tmp_path / "episode.mkv"
    media.touch()
    sidecar = tmp_path / "episode.en.ass"
    sidecar.write_text("[Events]\n", encoding="utf-8")

    track = _track(0, lang="eng")
    extracted = tmp_path / "work" / "episode.track0.srt"

    probe_called = []

    def fake_probe(path):
        probe_called.append(path)
        return [track]

    def fake_extract(media_path, selected, work_dir):
        extracted.parent.mkdir(parents=True)
        extracted.touch()
        return extracted

    monkeypatch.setattr("translate_subs.io.source_resolver.probe_subtitle_tracks", fake_probe)
    monkeypatch.setattr("translate_subs.io.source_resolver.extract_track", fake_extract)

    result = resolve_source(
        media, work_dir=tmp_path / "work", lang="en", track_index=0, interactive=False
    )

    assert probe_called, "ffprobe must be called when --track is specified"
    assert result.subtitle_path == extracted
    assert result.was_extracted
