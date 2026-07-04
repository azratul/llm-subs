"""Subtitle loading and saving (any input format -> .ass or .srt)."""

from __future__ import annotations

import codecs
from pathlib import Path

import pysubs2
from charset_normalizer import from_bytes

# Upper bound on an input subtitle file. The whole file is read into memory (encoding detection
# needs the bytes, pysubs2 parses in memory) and charset-normalizer's cost grows with input size;
# a mis-pointed video/archive would grind or exhaust memory with no useful message. Real subtitle
# files top out in the tens of MiB even with embedded fonts, so 64 MiB is comfortably generous.
_MAX_INPUT_BYTES = 64 * 1024 * 1024

# BOM signatures, longest first: the UTF-32 marks start with the UTF-16 marks, so the 4-byte
# checks must precede the 2-byte ones. Each maps to the codec that *strips* the BOM — utf-8-sig,
# and the length-generic utf-16/utf-32 which also auto-detect endianness — so the marker never
# leaks into the parsed text.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

# The conventional legacy codepage per source language (primary subtag). Statistical detection
# cannot always separate near-identical single-byte codepages (CP1250 vs CP1252 differ in a handful
# of positions such as €/Ł), but the user already told us the source language via --lang — that is
# a far stronger prior than byte statistics. The hint is only *trusted* when the bytes decode
# strictly in the hinted codec; otherwise detection falls through to charset-normalizer.
_LANG_LEGACY_ENCODINGS: dict[str, str] = {
    # Western Europe / Americas -> CP1252
    **dict.fromkeys(
        (
            "en",
            "es",
            "fr",
            "de",
            "pt",
            "it",
            "nl",
            "da",
            "sv",
            "no",
            "nb",
            "nn",
            "fi",
            "is",
            "ca",
            "gl",
            "eu",
            "id",
            "ms",
            "sw",
            "tl",
        ),
        "cp1252",
    ),
    # Central Europe -> CP1250
    **dict.fromkeys(("pl", "cs", "sk", "hu", "hr", "sl", "ro", "sq", "bs"), "cp1250"),
    # Cyrillic -> CP1251
    **dict.fromkeys(("ru", "uk", "be", "bg", "mk", "sr", "kk"), "cp1251"),
    "el": "cp1253",
    "tr": "cp1254",
    "he": "cp1255",
    "ar": "cp1256",
    "et": "cp1257",
    "lt": "cp1257",
    "lv": "cp1257",
    "vi": "cp1258",
    "th": "cp874",
    "ja": "cp932",  # Shift-JIS superset
    "ko": "cp949",
    "zh": "gbk",  # simplified; a Big5 (traditional) file falls through to statistics
}


def _lang_hint_encoding(lang_hint: str | None) -> str | None:
    if not lang_hint:
        return None
    primary = lang_hint.split("-")[0].strip().lower()
    return _LANG_LEGACY_ENCODINGS.get(primary)


def detect_encoding(raw: bytes, *, lang_hint: str | None = None) -> str:
    """Best-effort text encoding for subtitle bytes.

    A BOM is honoured first (UTF-8/16/32), then strict UTF-8 — both unambiguous and cheap. For the
    legacy remainder, the source language (`--lang`) is the strongest signal available: if the
    hinted language's conventional codepage decodes the bytes strictly, use it. Only then defer to
    charset-normalizer's statistics — near-identical codepages (CP1250 vs CP1252, which differ in a
    few positions like €/Ł) are genuinely beyond byte statistics, and a strict decode ladder cannot
    tell CP1252 from Shift-JIS at all (byte runs valid in both would silently corrupt). Latin-1 is
    the final catch-all since it decodes every byte value.
    """
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            return enc
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    hinted = _lang_hint_encoding(lang_hint)
    if hinted is not None:
        try:
            raw.decode(hinted)
            return hinted
        except UnicodeDecodeError:
            pass
    best = from_bytes(raw).best()
    if best is not None:
        return best.encoding
    return "latin-1"


def load(
    path: str | Path, *, encoding: str | None = None, lang_hint: str | None = None
) -> pysubs2.SSAFile:
    """Load a subtitle file, auto-detecting the text encoding when not given.

    pysubs2 defaults to UTF-8; with `encoding=None` the bytes are sniffed first (`detect_encoding`,
    biased by `lang_hint` — the CLI's `--lang`) so CP1252/CP1250/Shift-JIS/UTF-16 sidecars load
    correctly. An explicit `encoding` is validated up front so an unknown codec surfaces as a
    short, actionable `ValueError` instead of a `LookupError` traceback from deep inside pysubs2.
    """
    size = Path(path).stat().st_size
    if size > _MAX_INPUT_BYTES:
        raise ValueError(
            f"'{path}' is {size / (1024 * 1024):.0f} MiB — too large for a subtitle file "
            f"(limit {_MAX_INPUT_BYTES // (1024 * 1024)} MiB). Check that the path really "
            f"points at a subtitle and not a video/archive with a subtitle extension."
        )
    if encoding is None:
        encoding = detect_encoding(Path(path).read_bytes(), lang_hint=lang_hint)
    else:
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise ValueError(f"unknown encoding '{encoding}'") from exc
    return pysubs2.load(str(path), encoding=encoding)


def save(subs: pysubs2.SSAFile, path: str | Path, *, fmt: str | None = None) -> None:
    """Save `subs`; the format is `fmt` or, if None, inferred from the suffix."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(path), format_=fmt)
