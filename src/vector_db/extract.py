"""取り込み対象ファイルからテキストを取り出す。

skills/ 配下の読み取りスクリプト（read_pdf.py 等）は CLI として main() 内に実装されて
いて import できないため、取り込みに必要な本文だけを取り出す薄い実装をここに持つ。
ページ・シート・スライド単位に分けて返し、検索結果の出典（locator）に使う。
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import locale
from pathlib import Path
import threading

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".md", ".txt", ".csv")

# PDFium（pypdfium2）は同じプロセスの複数スレッドからの同時呼び出しに対応していないため、PDF の本文の抽出と
# ページの画像化（render.py）はこのロックで1つずつ行う（子プロセス（[vector_db].ingest_processes）同士は並列に動く）。
PDFIUM_LOCK = threading.Lock()


@dataclass(frozen=True)
class Segment:
    """ファイル内の1区切り（ページ・シート・スライド等）の本文。

    Attributes:
        locator: ファイル内の位置（例: "p.3"、"シート: 集計"、"スライド2"）。区切りが無い形式は空文字。
        text: 本文。
    """

    locator: str
    text: str


class ExtractError(Exception):
    """対応していない形式、またはファイルを読めなかった。"""


def extract_segments(path: Path, data: bytes | None = None) -> list[Segment]:
    """拡張子に応じてテキストを取り出す。本文が空の区切りは含めない。

    data にファイルの中身を渡すと、ファイルを読み直さずにそれを使う（取り込みで SHA-256 の計算と読み取りを1回にするため）。

    Raises:
        ExtractError: 対応していない拡張子、またはファイルを読めなかった場合。
    """
    ext = path.suffix.lower()
    try:
        source = str(path) if data is None else io.BytesIO(data)
        if ext == ".pdf":
            segments = _extract_pdf(str(path) if data is None else data)
        elif ext == ".docx":
            segments = _extract_docx(source)
        elif ext in (".xlsx", ".xlsm"):
            segments = _extract_xlsx(source)
        elif ext == ".pptx":
            segments = _extract_pptx(source)
        elif ext in (".md", ".txt", ".csv"):
            segments = [Segment("", _decode_text(path.read_bytes() if data is None else data))]
        else:
            raise ExtractError(f"対応していない形式です: {ext}")
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 - 壊れたファイルは1件の失敗として扱い、取り込み全体は止めない
        if ext in _OOXML_EXTENSIONS and _is_encrypted_office(path, data):
            raise ExtractError("パスワード付き（暗号化された）ファイルのため読めません") from e
        raise ExtractError(f"{type(e).__name__}: {e}") from e
    return [s for s in segments if s.text.strip()]


# Office Open XML（ZIP）の形式。パスワードを付けると ZIP ではなく OLE の複合ファイル（暗号化されたパッケージ）になる。
_OOXML_EXTENSIONS = (".docx", ".xlsx", ".xlsm", ".pptx")
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _is_encrypted_office(path: Path, data: bytes | None) -> bool:
    """docx・xlsx・pptx がパスワード付き（中身が OLE の複合ファイル）か。"""
    if data is not None:
        return data[:8] == _OLE_SIGNATURE
    try:
        with path.open("rb") as f:
            return f.read(8) == _OLE_SIGNATURE
    except OSError:
        return False


def _decode_text(data: bytes) -> str:
    """UTF-8（BOM 付き含む）→ CP932 → OS の ANSI コードページ（英語版 Windows なら cp1252 等）の順に読む。"""
    for encoding in dict.fromkeys(("utf-8-sig", "cp932", locale.getencoding())):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(source: str | bytes) -> list[Segment]:
    """PDF のページごとの文字（PDFium は C の実装で、pypdf より大幅に速い）。

    空のパスワードで開ける暗号化 PDF（閲覧の制限だけのもの）はそのまま読める。
    """
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(source)
        try:
            segments = []
            for i in range(len(pdf)):
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_bounded() or ""
                    finally:
                        textpage.close()
                finally:
                    page.close()
                segments.append(Segment(f"p.{i + 1}", normalize_newlines(text)))
            return segments
        finally:
            pdf.close()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _extract_docx(source) -> list[Segment]:
    import docx

    document = docx.Document(source)
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(cell.text.strip() for cell in row.cells))
    return [Segment("", "\n".join(lines))]


def _extract_xlsx(source) -> list[Segment]:
    import openpyxl

    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        segments = []
        for sheet in workbook.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells).rstrip("\t"))
            segments.append(Segment(f"シート: {sheet.title}", "\n".join(rows)))
        return segments
    finally:
        workbook.close()


def _extract_pptx(source) -> list[Segment]:
    from pptx import Presentation

    presentation = Presentation(source)
    segments = []
    for i, slide in enumerate(presentation.slides, start=1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append("\t".join(cell.text.strip() for cell in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                lines.append(f"[ノート] {notes}")
        segments.append(Segment(f"スライド{i}", "\n".join(lines)))
    return segments
