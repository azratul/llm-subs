from __future__ import annotations

import pytest

from translate_subs.subs import document
from translate_subs.subs.validator import validate_file

# Realistic multi-line fixtures: statistical detection needs a representative sample, and real
# subtitle files always have many lines. A one-line micro-fixture is genuinely ambiguous (too few
# bytes to tell CP1252 from Shift-JIS), so tests assert on the round-tripped *content*, not on the
# exact codec name — cp932 is a valid decode of Shift-JIS, cp1250/cp1252 agree on the Latin range.
_ES_LINES = [
    "La canción de la función empezó.",
    "¿Dónde está el niño pequeño?",
    "Él vivió más allá del jardín, solo.",
]
_JA_LINES = [
    "こんにちは、元気ですか？",
    "今日はいい天気ですね。",
    "明日も晴れるといいな。",
    "一緒に公園へ行きましょう。",
]


def _srt(lines):
    blocks = []
    for i, text in enumerate(lines, start=1):
        blocks.append(f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\n{text}\n")
    return "\n".join(blocks)


def _write(tmp_path, name, lines, encoding):
    path = tmp_path / name
    path.write_bytes(_srt(lines).encode(encoding))
    return path


def _plaintexts(subs):
    return [e.plaintext for e in subs.events]


def test_detect_encoding_boms():
    assert document.detect_encoding(b"\xef\xbb\xbfhi") == "utf-8-sig"
    assert document.detect_encoding(b"\xff\xfeh\x00") == "utf-16"
    assert document.detect_encoding(b"\xfe\xff\x00h") == "utf-16"
    # The UTF-32 marks embed the UTF-16 marks, so the 4-byte check must win.
    assert document.detect_encoding(b"\xff\xfe\x00\x00h\x00\x00\x00") == "utf-32"
    assert document.detect_encoding(b"\x00\x00\xfe\xff\x00\x00\x00h") == "utf-32"


def test_detect_encoding_prefers_utf8():
    assert document.detect_encoding("Café ñ para todos".encode()) == "utf-8"


def test_lang_hint_disambiguates_cp1252_from_cp1250(tmp_path):
    # €/£ occupy positions where CP1250 and CP1252 differ (0x80/0xA3 read as €/Ł in CP1250), and
    # byte statistics alone can pick the wrong sibling codepage. --lang en is a stronger prior:
    # the hinted codec decodes strictly, so it wins and the symbols survive.
    lines = [
        "It costs €20… maybe £25 tomorrow.",
        "That is not a fair price, is it?",
        "We will see about that soon enough.",
    ]
    path = _write(tmp_path, "s.srt", lines, "cp1252")
    assert _plaintexts(document.load(path, lang_hint="en")) == lines


def test_lang_hint_ja_selects_shift_jis(tmp_path):
    path = _write(tmp_path, "s.srt", _JA_LINES, "shift-jis")
    assert _plaintexts(document.load(path, lang_hint="ja")) == _JA_LINES


def test_wrong_lang_hint_falls_through_when_undecodable():
    # A hint whose codec cannot decode the bytes is discarded, not trusted: cp1250 rejects 0x81,
    # so detection falls through to statistics instead of failing.
    raw = "こんにちは、元気ですか？今日はいい天気ですね。".encode("shift-jis")
    assert b"\x81" in raw  # the hinted codec (cp1250 for pl) can't decode this byte
    detected = document.detect_encoding(raw, lang_hint="pl")
    assert raw.decode(detected)  # whatever won, it decodes — the bad hint didn't break detection


def test_load_cp1252_sidecar(tmp_path):
    path = _write(tmp_path, "s.srt", _ES_LINES, "cp1252")
    assert _plaintexts(document.load(path)) == _ES_LINES


def test_load_shift_jis_sidecar(tmp_path):
    path = _write(tmp_path, "s.srt", _JA_LINES, "shift-jis")
    assert _plaintexts(document.load(path)) == _JA_LINES


def test_load_utf16_sidecar(tmp_path):
    path = _write(tmp_path, "s.srt", _ES_LINES, "utf-16")
    assert _plaintexts(document.load(path)) == _ES_LINES


def test_load_utf8_sig_sidecar(tmp_path):
    path = _write(tmp_path, "s.srt", _ES_LINES, "utf-8-sig")
    assert _plaintexts(document.load(path)) == _ES_LINES


def test_explicit_encoding_overrides_detection(tmp_path):
    # cp1252 and latin-1 agree on the accented Latin range used here, so forcing latin-1 decodes
    # the same content — proving the override is honoured.
    path = _write(tmp_path, "s.srt", _ES_LINES, "cp1252")
    assert _plaintexts(document.load(path, encoding="latin-1")) == _ES_LINES


def test_unknown_encoding_raises_clean_value_error(tmp_path):
    # A bad --encoding must be a short, actionable error, not a LookupError traceback from pysubs2.
    path = _write(tmp_path, "s.srt", _ES_LINES, "utf-8")
    with pytest.raises(ValueError, match="unknown encoding 'definitely-not-a-codec'"):
        document.load(path, encoding="definitely-not-a-codec")


def test_load_rejects_absurdly_large_input(tmp_path):
    # A mis-pointed video/archive with a subtitle extension must fail fast with a clear message,
    # not grind through charset detection on gigabytes. Sparse file: big st_size, no disk cost.
    import os

    path = tmp_path / "huge.ass"
    path.touch()
    os.truncate(path, 64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="too large for a subtitle file"):
        document.load(path)


def test_validate_file_reads_cp1252(tmp_path):
    path = _write(tmp_path, "s.srt", _ES_LINES, "cp1252")
    result = validate_file(path)
    assert result.ok, result.errors
