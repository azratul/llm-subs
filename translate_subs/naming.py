"""Output naming convention: <base>.<lang>.<format> (format defaults to .ass)."""

from __future__ import annotations

from pathlib import Path

# The full ISO 639-1 two-letter set, so a sidecar/output suffix in *any* language (e.g.
# `episode.ru.srt`) is recognized — the tool translates any language to any language.
ISO_639_1 = frozenset(
    "aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv "
    "cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr "
    "ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw "
    "ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv "
    "ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr "
    "ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi "
    "yo za zh zu".split()
)

# Recognized as a trailing language suffix in a filename: the ISO codes plus common spellings
# people actually use in subtitle names (full words and 3-letter codes).
_LANG_SUFFIX_SPELLINGS = frozenset(
    {
        "eng",
        "english",
        "spa",
        "esp",
        "lat",
        "latam",
        "spanish",
        "jpn",
        "jp",
        "japanese",
        "fra",
        "fre",
        "french",
        "por",
        "portuguese",
        "ger",
        "deu",
        "german",
        "ita",
        "italian",
        "chi",
        "zho",
        "chinese",
        "kor",
        "korean",
        "rus",
        "russian",
        "ara",
        "arabic",
    }
)
_LANG_TOKENS = ISO_639_1 | _LANG_SUFFIX_SPELLINGS

SUPPORTED_FORMATS = ("ass", "srt")
DEFAULT_FORMAT = "ass"


def lang_code(target: str) -> str:
    """Short filename code for a target like 'es-latam' -> 'es', 'fr-FR' -> 'fr'."""
    code = target.strip().lower().replace("_", "-").split("-", 1)[0]
    return code or "out"


def target_dirname(target: str) -> str:
    """Filesystem-safe, case-normalized directory name for a *full* target.

    Unlike `lang_code` (which collapses both 'es-latam' and 'es-ES' to 'es'), this keeps the
    region so different variants get separate memory subtrees and can't contaminate each other.
    Used only for the on-disk memory layout; the output *filename* still uses `lang_code`.
    """
    name = target.strip().lower().replace("_", "-")
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch == "-").strip("-")
    return cleaned or "out"


def base_stem(origin: Path) -> str:
    """Original name without extension and without a trailing language suffix."""
    stem = origin.stem
    base, _, last = stem.rpartition(".")
    if base and last.lower() in _LANG_TOKENS:
        return base
    return stem


def output_path(
    origin: str | Path,
    *,
    fmt: str = DEFAULT_FORMAT,
    out_dir: str | Path | None = None,
    lang: str = "es",
) -> Path:
    """Output path `<base>.<lang>.<fmt>`.

    Defaults next to the original; `out_dir` redirects it (test sandbox). `fmt` is
    the output container ('ass' keeps positioning/styles, 'srt' is flat).
    """
    origin = Path(origin)
    name = f"{base_stem(origin)}.{lang}.{fmt}"
    directory = Path(out_dir) if out_dir is not None else origin.parent
    return directory / name
