"""Resolve the source subtitle: direct sidecar, sidecar next to media, or
embedded track (with track selection)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from translate_subs.io.media_probe import MediaToolError, SubtitleTrack, probe_subtitle_tracks
from translate_subs.io.track_extractor import extract_track
from translate_subs.naming import ISO_639_1, is_lang_suffix

SUBTITLE_EXTS = {
    ".ass",
    ".ssa",
    ".srt",
    ".sub",
    ".vtt",
    ".smi",
    ".sami",
    ".mpl2",
    ".ttml",
}
MEDIA_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts"}

# Map ISO-639-1/2 codes and English names to a canonical base language, so a requested
# language matches a track/sidecar by *equality* of the normalized code rather than by a
# naive substring test (where e.g. "en" matched any label containing those letters).
_LANG_ALIASES = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "es": "es",
    "spa": "es",
    "esp": "es",
    # `lat`/`latam`/`latino` map to Spanish on purpose: in the anime/fan-release domain this tool
    # targets, `.lat.srt` sidecars and tracks labelled "LAT"/"Latino" mean Latin American Spanish,
    # a very common source. This is a deliberate override of the ISO-639-2 meaning of `lat` (Latin
    # the language), which does not occur as a video subtitle track in practice; matching a real
    # Latino sidecar is worth far more here than reserving `lat` for a language nobody subtitles.
    "lat": "es",
    "latam": "es",
    "latino": "es",
    "spanish": "es",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "de": "de",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "it": "it",
    "ita": "it",
    "italian": "it",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    # ISO 639-2/B and 639-2/T codes not covered above (what ffprobe commonly reports).
    "rus": "ru",
    "ara": "ar",
    "pol": "pl",
    "ces": "cs",
    "cze": "cs",
    "hun": "hu",
    "ukr": "uk",
    "tur": "tr",
    "nld": "nl",
    "dut": "nl",
    "swe": "sv",
    "nor": "no",
    "nob": "no",
    "nno": "no",
    "dan": "da",
    "fin": "fi",
    "ron": "ro",
    "rum": "ro",
    "heb": "he",
    "tha": "th",
    "ind": "id",
    "vie": "vi",
    "msa": "ms",
    "may": "ms",
    "bul": "bg",
    "hrv": "hr",
    "srp": "sr",
    "slk": "sk",
    "slo": "sk",
    "cat": "ca",
    "ell": "el",
    "gre": "el",
    "hin": "hi",
    "ben": "bn",
    "tam": "ta",
    "tel": "te",
    "urd": "ur",
    "fas": "fa",
    "per": "fa",
    "slv": "sl",
    "lit": "lt",
    "lav": "lv",
    "est": "et",
    "sqi": "sq",
    "alb": "sq",
    "mkd": "mk",
    "mac": "mk",
    "bel": "be",
    "aze": "az",
    "kaz": "kk",
    "kat": "ka",
    "geo": "ka",
    "hye": "hy",
    "arm": "hy",
    "isl": "is",
    "ice": "is",
    "glg": "gl",
    "eus": "eu",
    "baq": "eu",
    "gle": "ga",
    "wel": "cy",
    "cym": "cy",
    "mlt": "mt",
    "afr": "af",
    "swa": "sw",
    "amh": "am",
}

# Language suffixes recognised when matching sidecars (e.g. "movie.en.srt"): the spellings we
# normalize explicitly, plus every ISO 639-1 code (which normalize_lang passes through as-is),
# so a sidecar in any language is detected, not only the few with alias spellings.
_LANG_TOKENS = set(_LANG_ALIASES) | set(ISO_639_1)


def normalize_lang(value: str | None) -> str | None:
    """Canonical base language code, e.g. 'eng'/'English'/'en-US' -> 'en'. None stays None."""
    if not value:
        return None
    token = value.strip().lower().replace("_", "-").split("-")[0]
    return _LANG_ALIASES.get(token, token)


def _is_sdh(track: SubtitleTrack) -> bool:
    """Heuristic: a hearing-impaired/SDH track, deprioritized for plain translation."""
    title = (track.title or "").lower()
    return "sdh" in title or "hearing" in title or "[hi]" in title


class SourceError(Exception):
    pass


@dataclass
class ResolvedSource:
    subtitle_path: Path
    origin: Path  # file the output name derives from (media or sidecar)
    was_extracted: bool
    track: SubtitleTrack | None = None
    selected_lang: str | None = None  # normalized language of the chosen subtitle, if known
    lang_fallback: bool = False  # True when the chosen subtitle isn't the requested language


def _sidecar_lang(sidecar: Path) -> str | None:
    """Normalized language tag of a sidecar (e.g. 'movie.en.srt' -> 'en'), or None."""
    last = sidecar.stem.rpartition(".")[2].lower()
    return normalize_lang(last) if is_lang_suffix(last, _LANG_TOKENS) else None


def _is_lang_fallback(requested: str | None, selected: str | None) -> bool:
    """True when a specific language was requested but the chosen subtitle differs."""
    want = normalize_lang(requested)
    return want is not None and selected is not None and selected != want


def _find_sidecar(media: Path, lang: str | None = None) -> Path | None:
    """Find a sidecar next to `media`, preferring one in the requested language.

    Order: a sidecar whose language tag matches `lang`, then a bare `<stem>.<ext>` with no
    language tag, then any other recognised-language sidecar.
    """
    stem = media.stem
    want = normalize_lang(lang)
    bare: Path | None = None
    lang_matches: list[Path] = []
    others: list[Path] = []
    for candidate in sorted(media.parent.iterdir()):
        if candidate.suffix.lower() not in SUBTITLE_EXTS:
            continue
        cand_stem = candidate.stem
        if cand_stem == stem:
            bare = bare or candidate
            continue
        base, _, last = cand_stem.rpartition(".")
        if base == stem and is_lang_suffix(last, _LANG_TOKENS):
            if want is not None and normalize_lang(last) == want:
                lang_matches.append(candidate)
            else:
                others.append(candidate)
    if lang_matches:
        return lang_matches[0]
    if bare is not None:
        return bare
    return others[0] if others else None


def select_track(
    tracks: list[SubtitleTrack],
    *,
    lang: str | None,
    track_index: int | None,
    interactive: bool,
    prompt=input,
) -> SubtitleTrack:
    """Pick a text track by explicit flag, heuristic, or prompt."""
    text_tracks = [t for t in tracks if t.is_text]
    if not text_tracks:
        if tracks:
            raise SourceError(
                "Only image subtitle tracks (PGS/VobSub) present; need OCR (out of v1)."
            )
        raise SourceError("Container has no subtitle tracks.")

    if track_index is not None:
        for t in text_tracks:
            if t.rel_index == track_index:
                return t
        raise SourceError(f"No text track #{track_index}.")

    if len(text_tracks) == 1:
        return text_tracks[0]

    if interactive:
        for t in text_tracks:
            label = f"  [{t.rel_index}] {t.codec} lang={t.language or '?'}"
            if t.title:
                label += f" '{t.title}'"
            if t.default:
                label += " (default)"
            if t.forced:
                label += " (forced)"
            print(label)
        raw = prompt("Track to extract [index]: ").strip()
        try:
            chosen = int(raw)
        except ValueError as exc:
            raise SourceError(
                f"Not a track index: {raw!r}. Enter one of the numbers listed."
            ) from exc
        return select_track(text_tracks, lang=None, track_index=chosen, interactive=False)

    # Non-interactive heuristic: rank by exact language match, full track over forced,
    # plain over SDH/HI, then the default flag. Ties keep container order (stable max).
    want = normalize_lang(lang)

    def rank(t: SubtitleTrack) -> tuple[bool, bool, bool, bool]:
        lang_match = want is not None and normalize_lang(t.language) == want
        return (lang_match, not t.forced, not _is_sdh(t), t.default)

    return max(text_tracks, key=rank)


def resolve_source(
    input_path: str | Path,
    *,
    work_dir: str | Path,
    lang: str | None = "en",
    track_index: int | None = None,
    interactive: bool = True,
    strict_lang: bool = False,
) -> ResolvedSource:
    """Return the subtitle to translate, given a subtitle file or a media file.

    When a specific `lang` is requested but the only available sidecar/track is a different
    language, that is a *fallback*: flagged on the result, and refused if `strict_lang`.
    """
    path = Path(input_path)
    if not path.exists():
        raise SourceError(f"Path does not exist: {path}")
    if path.is_dir():
        raise SourceError(f"Expected a subtitle or media file, but received a directory: {path}")

    suffix = path.suffix.lower()

    if suffix in SUBTITLE_EXTS:
        # An explicitly passed subtitle file is taken at face value (no language guessing).
        return ResolvedSource(subtitle_path=path, origin=path, was_extracted=False)

    if suffix in MEDIA_EXTS:
        # An explicit --track flag means the user wants a specific embedded track; skip
        # sidecar discovery so it cannot silently override the user's choice.
        sidecar = _find_sidecar(path, lang) if track_index is None else None
        fallback_sidecar: tuple[Path, str | None] | None = None
        if sidecar is not None:
            selected = _sidecar_lang(sidecar)
            if _is_lang_fallback(lang, selected):
                # Wrong-language sidecar: keep as last resort and try the container first.
                fallback_sidecar = (sidecar, selected)
            else:
                # Exact-match or untagged sidecar: prefer it over embedded tracks.
                return ResolvedSource(
                    subtitle_path=sidecar,
                    origin=path,
                    was_extracted=False,
                    selected_lang=selected,
                    lang_fallback=False,
                )
        try:
            tracks = probe_subtitle_tracks(path)
            track = select_track(
                tracks, lang=lang, track_index=track_index, interactive=interactive
            )
            selected = normalize_lang(track.language)
            fallback = _is_lang_fallback(lang, selected)
            if strict_lang and fallback:
                raise SourceError(
                    f"No '{lang}' subtitle track in {path.name} "
                    f"(closest: #{track.rel_index}, lang={track.language or '?'}). "
                    "Pass a different --lang/--track or drop --strict-lang."
                )
            extracted = extract_track(path, track, work_dir)
            return ResolvedSource(
                subtitle_path=extracted,
                origin=path,
                was_extracted=True,
                track=track,
                selected_lang=selected,
                lang_fallback=fallback,
            )
        except (SourceError, MediaToolError):
            if fallback_sidecar is not None:
                sidecar, selected = fallback_sidecar
                if strict_lang:
                    raise SourceError(
                        f"No '{lang}' subtitle next to {path.name} (closest sidecar: "
                        f"{sidecar.name}) and no matching embedded track. "
                        "Pass a different --lang/--track or drop --strict-lang."
                    ) from None
                return ResolvedSource(
                    subtitle_path=sidecar,
                    origin=path,
                    was_extracted=False,
                    selected_lang=selected,
                    lang_fallback=True,
                )
            raise

    raise SourceError(f"Unsupported extension: {suffix}")
