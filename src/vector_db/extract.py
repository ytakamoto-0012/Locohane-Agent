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
            raise ExtractError(f"Unsupported format: {ext}")
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 - 壊れたファイルは1件の失敗として扱い、取り込み全体は止めない
        if ext in _OOXML_EXTENSIONS and _is_encrypted_office(path, data):
            raise ExtractError("Cannot read because the file is password-protected (encrypted)") from e
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
    """UTF-8（BOM 付き含む）→ OS の ANSI コードページ（日本語版 Windows なら cp932、英語版なら cp1252 等）
    → CP932 の順に読む（ANSI を先にする理由は src/tools/_file_tools_common.py の read_text_with_fallback）。"""
    for encoding in dict.fromkeys(("utf-8-sig", locale.getencoding(), "cp932")):
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


# Office Open XML の名前空間。
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_THREADED = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
# 新しい形式（図形のテキストボックス等）と、古い Office 向けの代替（VML）を両方持つ要素。
# 代替（mc:Fallback）は同じ文字の複製のため読まない。
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

# docx の本文以外で文字を読むパーツ（content type の末尾 → 本文に付ける見出し）。
_DOCX_EXTRA_PARTS = {
    "header+xml": "[Header]",
    "footer+xml": "[Footer]",
    "footnotes+xml": "[Footnote]",
    "endnotes+xml": "[Endnote]",
    "comments+xml": "[Comment]",
}


def _extract_docx(source) -> list[Segment]:
    """本文の段落・表に加えて、テキストボックス・ヘッダー/フッター・脚注/文末脚注・コメントの文字を取り出す。

    本文以外の文字は本文の後ろに見出し（[Header] 等）を付けて並べる。同じヘッダー/フッターを複数の
    セクションで使っていても1回だけにする。
    """
    import docx
    from docx.oxml import parse_xml
    from docx.text.paragraph import Paragraph

    def paragraphs_text(element) -> list[str]:
        _drop_fallbacks(element)
        return [t for p in element.iter(f"{{{_NS_W}}}p") if (t := Paragraph(p, None).text).strip()]

    document = docx.Document(source)
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(cell.text.strip() for cell in row.cells))
    seen: set[str] = set()

    def add(label: str, element) -> None:
        for text in paragraphs_text(element):
            line = f"{label} {text}"
            if line not in seen:
                seen.add(line)
                lines.append(line)

    body = document.element.body
    _drop_fallbacks(body)
    # テキストボックスの中のテキストボックスは外側でも読むため、同じ行は seen で1回にする。
    for textbox in body.iter(f"{{{_NS_W}}}txbxContent"):
        add("[Text box]", textbox)
    for part in document.part.package.iter_parts():
        label = next((v for k, v in _DOCX_EXTRA_PARTS.items() if part.content_type.endswith("wordprocessingml." + k)), None)
        if label is not None:
            add(label, parse_xml(part.blob))
    return [Segment("", "\n".join(lines))]


def _drop_fallbacks(root) -> None:
    """root の下の mc:Fallback（同じ内容の古い形式の複製）を取り除く（lxml・ElementTree のどちらの要素でも使える）。"""
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag == _MC_FALLBACK:
                parent.remove(child)


def _extract_xlsx(source) -> list[Segment]:
    """シートごとのセルの値に、図形（テキストボックス等）・グラフのタイトル・コメントの文字を加える。

    図形はアンカー（左上が乗っているセル）の行、コメントは付いているセルの行の後ろに、セル番地を付けて
    差し込む（例: "[Shape C5] 承認欄"）。近くのセルと同じチャンクに入り、検索結果でも位置が分かるようにするため。
    セルではなく位置で置いた図形はシートの末尾に付ける。
    """
    import openpyxl

    try:
        extras = _xlsx_sheet_extras(source)
    except Exception:  # noqa: BLE001 - 図形・コメントが読めなくても、セルの値は取り込む
        extras = {}
    if isinstance(source, io.BytesIO):
        source.seek(0)
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        segments = []
        for sheet in workbook.worksheets:
            notes = extras.get(sheet.title, {})
            rows = []
            last_row = 0
            for last_row, row in enumerate(sheet.iter_rows(min_row=1, values_only=True), start=1):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells).rstrip("\t"))
                rows.extend(notes.pop(last_row, []))
            # データの最後の行より下に置いた図形・コメントと、位置で置いた図形（行 0 として集めたもの）。
            for row_number in sorted(notes, key=lambda r: (r == 0, r)):
                rows.extend(notes[row_number])
            segments.append(Segment(f"Sheet: {sheet.title}", "\n".join(rows)))
        return segments
    finally:
        workbook.close()


def _xlsx_sheet_extras(source) -> dict[str, dict[int, list[str]]]:
    """シート名 → 行番号（1始まり。位置で置いた図形は 0）→ その行の後ろに差し込む図形・コメントの行。

    openpyxl の読み取り専用モードは図形・コメントを読まないため、xlsx（ZIP）の XML を直接読む。
    読めないパーツは飛ばす（セルの値の取り込みは続ける）。
    """
    import posixpath
    from xml.etree import ElementTree
    import zipfile

    with zipfile.ZipFile(source) as package:
        names = set(package.namelist())

        def read(name: str):
            if name not in names:
                return None
            try:
                return ElementTree.fromstring(package.read(name))
            except ElementTree.ParseError:
                return None

        def rels(part: str) -> list[tuple[str, str, str]]:
            """part の関係（種類の末尾（"drawing" 等）, 参照先のパーツ名, 関係の ID）。"""
            directory, base = posixpath.split(part)
            root = read(posixpath.join(directory, "_rels", base + ".rels"))
            if root is None:
                return []
            result = []
            for rel in root.iter(f"{{{_NS_PKG_REL}}}Relationship"):
                if rel.get("TargetMode") == "External":
                    continue
                target = rel.get("Target", "")
                name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(directory, target))
                result.append((rel.get("Type", "").rsplit("/", 1)[-1], name, rel.get("Id", "")))
            return result

        workbook = read("xl/workbook.xml")
        if workbook is None:
            return {}
        targets = {rel_id: name for _kind, name, rel_id in rels("xl/workbook.xml")}
        extras: dict[str, dict[int, list[str]]] = {}
        for sheet in workbook.iter(f"{{{_NS_S}}}sheet"):
            part = targets.get(sheet.get(f"{{{_NS_R}}}id", ""))
            if part is None:
                continue
            notes: dict[int, list[tuple[int, str]]] = {}
            sheet_rels = rels(part)
            threaded = [name for kind, name, _ in sheet_rels if kind == "threadedComment"]
            for kind, name, _ in sheet_rels:
                if kind == "drawing":
                    _xlsx_drawing_notes(read, rels, name, notes)
                elif kind == "comments" and not threaded:
                    # スレッド形式のコメントがあれば、古い形式のコメント（互換用の複製）は読まない。
                    # 古い形式の本文は <text><r><t>…</t></r></text> または <text><t>…</t></text>。
                    _xlsx_comment_notes(
                        read(name), f"{{{_NS_S}}}comment", lambda c: "".join(t.text or "" for t in c.iter(f"{{{_NS_S}}}t")), notes
                    )
                elif kind == "threadedComment":
                    _xlsx_comment_notes(
                        read(name), f"{{{_NS_THREADED}}}threadedComment", lambda c: c.findtext(f"{{{_NS_THREADED}}}text", ""), notes
                    )
            if notes:
                extras[sheet.get("name", "")] = {row: [line for _col, line in sorted(items)] for row, items in notes.items()}
        return extras


def _cell_name(row: int, col: int) -> str:
    """0始まりの行・列からセル番地（例: 0, 2 → "C1"）。"""
    letters = ""
    col += 1
    while col:
        col, rest = divmod(col - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return f"{letters}{row + 1}"


def _cell_position(ref: str) -> tuple[int, int]:
    """セル番地（例: "C5"）→ (行 1始まり, 列 0始まり)。読めなければ (0, 0)。"""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    digits = "".join(ch for ch in ref if ch.isdigit())
    col = 0
    for ch in letters:
        col = col * 26 + ord(ch) - ord("A") + 1
    return (int(digits), max(col - 1, 0)) if digits else (0, 0)


def _xlsx_drawing_notes(read, rels, drawing_part: str, notes: dict[int, list[tuple[int, str]]]) -> None:
    """図形（テキストボックス・オートシェイプ・グループ内の図形）の文字とグラフのタイトルを notes へ加える。"""
    root = read(drawing_part)
    if root is None:
        return
    _drop_fallbacks(root)
    charts = {rel_id: name for kind, name, rel_id in rels(drawing_part) if kind == "chart"}
    for anchor in root:
        start = anchor.find(f"{{{_NS_XDR}}}from")
        if start is not None:
            row = int(start.findtext(f"{{{_NS_XDR}}}row", "0"))
            col = int(start.findtext(f"{{{_NS_XDR}}}col", "0"))
            row_number, label = row + 1, f"[Shape {_cell_name(row, col)}]"
        else:
            row_number, col, label = 0, 0, "[Shape]"  # xdr:absoluteAnchor（セルではなく位置で置いた図形）
        texts = [_paragraphs_text(body) for body in anchor.iter(f"{{{_NS_XDR}}}txBody")]
        for chart_ref in anchor.iter(f"{{{_NS_C}}}chart"):
            chart = read(charts.get(chart_ref.get(f"{{{_NS_R}}}id", ""), ""))
            if chart is not None:
                _drop_fallbacks(chart)
                texts.extend(_paragraphs_text(title) for title in chart.iter(f"{{{_NS_C}}}title"))
        for text in texts:
            if text.strip():
                notes.setdefault(row_number, []).append((col, f"{label} {text}"))


def _paragraphs_text(element) -> str:
    """DrawingML の段落（a:p）ごとの文字を改行でつなぐ。"""
    lines = ["".join(t.text or "" for t in p.iter(f"{{{_NS_A}}}t")) for p in element.iter(f"{{{_NS_A}}}p")]
    return "\n".join(line for line in lines if line.strip())


def _xlsx_comment_notes(root, tag: str, text_of, notes: dict[int, list[tuple[int, str]]]) -> None:
    """コメント（tag の要素。本文は text_of(要素) で取り出す）の文字を、付いているセルの行の notes へ加える。"""
    if root is None:
        return
    for comment in root.iter(tag):
        ref = comment.get("ref", "")
        text = text_of(comment)
        if text.strip():
            row_number, col = _cell_position(ref)
            notes.setdefault(row_number, []).append((col, f"[Comment {ref}] {text.strip()}"))


def _extract_pptx(source) -> list[Segment]:
    from pptx import Presentation

    presentation = Presentation(source)
    segments = []
    for i, slide in enumerate(presentation.slides, start=1):
        lines = _pptx_shape_lines(slide.shapes)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                lines.append(f"[Notes] {notes}")
        segments.append(Segment(f"Slide {i}", "\n".join(lines)))
    return segments


def _pptx_shape_lines(shapes) -> list[str]:
    """図形の文字と表のセルを並べる。グループ化した図形は中の図形も（入れ子でも）読む。"""
    from pptx.shapes.group import GroupShape

    lines = []
    for shape in shapes:
        # shape_type は種類の分からない図形で例外になるため、型で判定する。
        if isinstance(shape, GroupShape):
            lines.extend(_pptx_shape_lines(shape.shapes))
            continue
        if shape.has_text_frame:
            lines.append(shape.text_frame.text)
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                lines.append("\t".join(cell.text.strip() for cell in row.cells))
    return lines
