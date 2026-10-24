"""英語版 Windows など、日本語以外の環境でも落ちないことのテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.config import PROJECT_ROOT
from src.tools._file_tools_common import read_text_with_fallback

PDF_SCRIPTS = PROJECT_ROOT / "skills" / "pdf-tools" / "scripts"


@pytest.fixture
def create_pdf(monkeypatch):
    monkeypatch.syspath_prepend(str(PDF_SCRIPTS))
    import create_pdf as module

    yield module
    sys.modules.pop("create_pdf", None)


def test_pdf_uses_latin_fonts_when_japanese_fonts_are_missing(create_pdf, monkeypatch) -> None:
    if not (create_pdf._WINDOWS_FONTS_DIR / "arial.ttf").exists():
        pytest.skip("Windows fonts are not available")
    monkeypatch.setattr(create_pdf, "_JP_GOTHIC_CANDIDATES", ["missing-gothic.ttc"])
    monkeypatch.setattr(create_pdf, "_JP_MINCHO_CANDIDATES", ["missing-mincho.ttc"])

    # 英語だけの文書は欧文フォントで作れる
    create_pdf.register_jp_fonts("<h1>Report</h1><p>Hello</p>")

    # 日本語を含む文書は、豆腐化けした PDF を返さないよう明示的にエラーにする
    with pytest.raises(RuntimeError, match="Japanese"):
        create_pdf.register_jp_fonts("<p>" + chr(0x65E5) + chr(0x672C) + "</p>")


def test_text_fallback_reads_cp932_and_normalizes_newlines(tmp_path: Path) -> None:
    sjis = tmp_path / "sjis.txt"
    sjis.write_bytes(bytes([0x93, 0xFA, 0x96, 0x7B]) + b"\r\nx")
    assert read_text_with_fallback(sjis) == chr(0x65E5) + chr(0x672C) + "\nx"


def test_text_fallback_uses_ansi_code_page(tmp_path: Path, monkeypatch) -> None:
    # 英語版 Windows（ANSI コードページ cp1252）の Latin-1 系ファイルを読める
    monkeypatch.setattr("locale.getencoding", lambda: "cp1252")
    latin = tmp_path / "latin.txt"
    latin.write_bytes(b"caf\xe9")
    assert read_text_with_fallback(latin) == "caf" + chr(0xE9)
