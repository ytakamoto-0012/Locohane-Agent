"""取り込み対象ファイルからテキストを取り出す。

skills/ 配下の読み取りスクリプト（read_pdf.py 等）は CLI として main() 内に実装されて
いて import できないため、取り込みに必要な本文だけを取り出す薄い実装をここに持つ。
ページ・シート・スライド単位に分けて返し、検索結果の出典（locator）に使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".md", ".txt", ".csv")


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


def extract_segments(path: Path) -> list[Segment]:
    """拡張子に応じてテキストを取り出す。本文が空の区切りは含めない。

    Raises:
        ExtractError: 対応していない拡張子、またはファイルを読めなかった場合。
    """
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            segments = _extract_pdf(path)
        elif ext == ".docx":
            segments = _extract_docx(path)
        elif ext in (".xlsx", ".xlsm"):
            segments = _extract_xlsx(path)
        elif ext == ".pptx":
            segments = _extract_pptx(path)
        elif ext in (".md", ".txt", ".csv"):
            segments = [Segment("", _read_text(path))]
        else:
            raise ExtractError(f"対応していない形式です: {ext}")
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 - 壊れたファイルは1件の失敗として扱い、取り込み全体は止めない
        raise ExtractError(f"{type(e).__name__}: {e}") from e
    return [s for s in segments if s.text.strip()]


def _read_text(path: Path) -> str:
    """UTF-8（BOM 付き含む）で読み、失敗したら CP932 で読む。"""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(path: Path) -> list[Segment]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        reader.decrypt("")
    segments = []
    for i, page in enumerate(reader.pages, start=1):
        segments.append(Segment(f"p.{i}", page.extract_text() or ""))
    return segments


def _extract_docx(path: Path) -> list[Segment]:
    import docx

    document = docx.Document(str(path))
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(cell.text.strip() for cell in row.cells))
    return [Segment("", "\n".join(lines))]


def _extract_xlsx(path: Path) -> list[Segment]:
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
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


def _extract_pptx(path: Path) -> list[Segment]:
    from pptx import Presentation

    presentation = Presentation(str(path))
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
