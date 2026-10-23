"""取り込み対象ファイルをページ単位の画像（PNG）にする（[vector_db].image_input = true のとき）。

render 系スキル（skills/docx-render 等の _render.py）と同じく、Office ファイルは
OLE（COM）で Word / PowerPoint / Excel をヘッドレス起動して PDF へ書き出し、
PDF のページを pypdfium2 で画像化する。スキルのスクリプトは CLI として作られていて
import できないため、取り込みに必要な部分だけの薄い実装をここに持つ。
画像ファイル（HEIC/HEIF を含む）は Pillow で開き、PNG にそろえる。

Office ファイルの画像化には Windows と Microsoft Office（pywin32）が必要。
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import os
from pathlib import Path
import tempfile
import threading
from typing import Callable

from .extract import ExtractError

# ページごとに画像にする文書の形式。
PAGE_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".xlsm")
# そのまま1枚の画像として取り込む画像ファイルの形式（image_input = true のときだけ取り込み対象になる）。
IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif")
# image_input = true のときに画像で埋め込む形式。
IMAGE_EXTENSIONS = PAGE_EXTENSIONS + IMAGE_FILE_EXTENSIONS

# 複数ページを持てる画像形式（スキャンした文書の TIFF 等）。それ以外は先頭の1枚だけ使う。
_MULTI_PAGE_IMAGE_EXTENSIONS = (".tif", ".tiff")
# A4 の長辺（インチ）。画像ファイルは、A4 の文書を image_dpi で画像化したときの長辺まで縮小する。
_A4_LONG_SIDE_INCHES = 297 / 25.4

# PDFium は複数スレッドからの同時呼び出しに対応していないため、取り込みを並列にしても
# （[vector_db].ingest_workers）PDF の画像化は1つずつ行う。
_PDFIUM_LOCK = threading.Lock()

_PROG_ID = {
    ".docx": "Word.Application",
    ".pptx": "PowerPoint.Application",
    ".xlsx": "Excel.Application",
    ".xlsm": "Excel.Application",
}


# PDF のページ番号（1始まり）と総ページ数から、ファイル内の位置（locator）を作る。
Locate = Callable[[int, int], str]


@dataclass(frozen=True)
class PageImage:
    """1ページ分の画像と、そのページから取り出した文字（検索結果の抜粋に使う）。

    Attributes:
        locator: ファイル内の位置（例: "p.3"、"スライド2"、"シート: 集計"）。
        text: ページの文字（PDF のテキスト層。無ければ空文字）。
        png: ページの PNG 画像。
    """

    locator: str
    text: str
    png: bytes


def render_pages(path: Path, dpi: int) -> list[PageImage]:
    """path の全ページを dpi の解像度で画像にする。

    Raises:
        ExtractError: 画像化に対応していない拡張子、Office が使えない、
            またはファイルを開けなかった場合。
    """
    ext = path.suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise ExtractError(f"画像化に対応していない形式です: {ext}")
    try:
        if ext == ".pdf":
            return _render_pdf(path, dpi, _page_locator)
        if ext in IMAGE_FILE_EXTENSIONS:
            return _render_image_file(path, dpi)
        with tempfile.TemporaryDirectory(prefix="locohane_vdb_") as tmp:
            pages: list[PageImage] = []
            for pdf_path, locate in _export_office_to_pdf(path, Path(tmp)):
                pages.extend(_render_pdf(pdf_path, dpi, locate))
            return pages
    except ExtractError:
        raise
    except ImportError as e:
        raise ExtractError(f"Office ファイルの画像化には Windows と Microsoft Office（pywin32）が必要です: {e}") from e
    except Exception as e:  # noqa: BLE001 - 壊れたファイルは1件の失敗として扱い、取り込み全体は止めない
        raise ExtractError(f"画像化に失敗しました: {type(e).__name__}: {e}") from e


def _render_pdf(pdf_path: Path, dpi: int, locate: Locate) -> list[PageImage]:
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            total = len(pdf)
            pages = []
            for i in range(total):
                page = pdf[i]
                try:
                    text = page.get_textpage().get_text_bounded() or ""
                    buffer = io.BytesIO()
                    page.render(scale=dpi / 72).to_pil().save(buffer, format="PNG")
                finally:
                    page.close()
                pages.append(PageImage(locate(i + 1, total), _normalize_newlines(text), buffer.getvalue()))
            return pages
        finally:
            pdf.close()


def _render_image_file(path: Path, dpi: int) -> list[PageImage]:
    """画像ファイルを PNG にする。EXIF の回転を反映し、透過部分は白で埋め、長辺が大きすぎれば縮小する。

    複数ページの TIFF はページごとに分ける（位置は p.N）。1枚だけなら位置は「画像」。
    """
    import pillow_heif
    from PIL import Image, ImageOps, ImageSequence

    # HEIC/HEIF は Pillow 標準では開けないため、オープナーの登録が必要（何度呼んでもよい）。
    pillow_heif.register_heif_opener()
    max_long_side = round(_A4_LONG_SIDE_INCHES * dpi)
    with Image.open(path) as img:
        frames = list(ImageSequence.Iterator(img)) if path.suffix.lower() in _MULTI_PAGE_IMAGE_EXTENSIONS else [img]
        pngs = [_to_png(ImageOps.exif_transpose(frame), max_long_side) for frame in frames]
    if len(pngs) == 1:
        return [PageImage("画像", "", pngs[0])]
    return [PageImage(f"p.{i}", "", png) for i, png in enumerate(pngs, start=1)]


def _to_png(img, max_long_side: int) -> bytes:
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (255, 255, 255))
        img.paste(rgba, mask=rgba.getchannel("A"))
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > max_long_side:
        img = img.copy()
        img.thumbnail((max_long_side, max_long_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _page_locator(page: int, total: int) -> str:
    return f"p.{page}"


def _sheet_locator(sheet: str) -> Locate:
    """Excel のシート1枚分の PDF の位置。1ページに収まれば「シート: 名前」だけにする。"""
    return lambda page, total: f"シート: {sheet}" if total == 1 else f"シート: {sheet} p.{page}"


def _export_office_to_pdf(path: Path, out_dir: Path) -> list[tuple[Path, Locate]]:
    """Office ファイルを COM で PDF に書き出し、(PDF パス, ページ位置の付け方) のリストを返す。

    Excel は可視シートごとに、render 系スキルと同じく横1×縦1ページのフィット印刷にして
    1シート=1PDF で書き出す（印刷設定の変更は保存しない）。PowerPoint は非表示スライドを
    PDF に含めないため、ページ番号を表示スライドの番号へ対応させる。
    """
    import pythoncom
    import win32com.client as win32

    ext = path.suffix.lower()
    abs_path = os.path.abspath(path)
    pythoncom.CoInitialize()
    app = None
    doc = None
    try:
        app = win32.DispatchEx(_PROG_ID[ext])
        # PowerPoint は Application.Visible = False の設定自体が COM エラーになる（Open(WithWindow=False) で隠す）。
        if ext != ".pptx":
            app.Visible = False
        app.DisplayAlerts = 0

        if ext == ".docx":
            pdf_path = out_dir / "document.pdf"
            doc = app.Documents.Open(abs_path, ReadOnly=True, AddToRecentFiles=False)
            doc.SaveAs2(str(pdf_path), 17)  # wdFormatPDF
            return [(pdf_path, _page_locator)]

        if ext == ".pptx":
            pdf_path = out_dir / "presentation.pdf"
            doc = app.Presentations.Open(abs_path, ReadOnly=True, Untitled=False, WithWindow=False)
            visible = [s.SlideIndex for s in doc.Slides if not s.SlideShowTransition.Hidden]
            doc.SaveAs(str(pdf_path), 32)  # ppSaveAsPDF
            return [(pdf_path, lambda page, total: f"スライド{visible[page - 1]}" if page <= len(visible) else f"p.{page}")]

        doc = app.Workbooks.Open(abs_path, UpdateLinks=0, ReadOnly=True, AddToMru=False)
        entries: list[tuple[Path, Locate]] = []
        for idx, ws in enumerate(doc.Worksheets):
            if ws.Visible != -1:  # xlSheetVisible 以外（非表示・最非表示）は印刷対象外
                continue
            try:
                page_setup = ws.PageSetup
                page_setup.Zoom = False
                page_setup.FitToPagesWide = 1
                page_setup.FitToPagesTall = 1
            except Exception:  # noqa: BLE001 - 印刷設定を変えられないシートはそのままの設定で書き出す
                pass
            sheet_pdf = out_dir / f"sheet_{idx}.pdf"
            try:
                ws.ExportAsFixedFormat(0, str(sheet_pdf), Quality=0, IgnorePrintAreas=False, OpenAfterPublish=False)  # xlTypePDF
            except Exception:  # noqa: BLE001 - 空のシートは書き出せない（印刷するものが無い）ため飛ばす
                continue
            entries.append((sheet_pdf, _sheet_locator(ws.Name)))
        return entries
    finally:
        if doc is not None:
            try:
                if ext == ".pptx":
                    doc.Close()
                else:
                    doc.Close(SaveChanges=False)
            except Exception:  # noqa: BLE001
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()
