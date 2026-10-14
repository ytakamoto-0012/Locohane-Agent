"""edit_excel.py の --code で実行するopenpyxlコードへ、名前空間として渡すヘルパー群。

openpyxl だけで書くと崩れやすい・手間がかかる処理（テーマ配色の表仕上げ、
結合セルを保ったままの行列挿入削除、グラフの配色とデータラベル、cm単位の
画像配置、グルーピング列の行範囲の解決）に限って用意する。それ以外は
LLM が openpyxl を直接使う前提で、ここに薄いラッパーを増やさない
（ops JSON 方式との比較検証が目的のため、ヘルパーが ops の再実装にならないようにする）。

実装は既存の _ops.py / _style.py の内部関数を流用する。
run_script からは直接実行されない。edit_excel.py から import して使う。
"""

from __future__ import annotations

import sys
from pathlib import Path

_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from excel_common import group_column_values  # noqa: E402
from office_theme import CHART_PALETTE, THEMES  # noqa: E402
from _ops import (  # noqa: E402
    _apply_col_widths,
    _col_width_from_rows,
    _format_table_range,
    _resize_preserving_merges,
    _write_rows,
    op_add_image,
    op_set_image_position,
)
from _style import ROLE_COLORS  # noqa: E402
from _style import apply_style as _apply_style_dict  # noqa: E402
from openpyxl.chart import PieChart
from openpyxl.chart.label import DataLabelList
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

__all__ = [
    "CHART_PALETTE",
    "ROLE_COLORS",
    "THEMES",
    "apply_style",
    "autofit_columns",
    "delete_cols",
    "delete_rows",
    "find_group",
    "format_table",
    "insert_cols",
    "insert_rows",
    "list_groups",
    "move_image",
    "place_image",
    "style_chart",
    "write_table",
]


def apply_style(ws, ref: str, style: dict) -> None:
    """セル範囲（"A1" / "A1:C3"）へ style 辞書（SKILL.md「style辞書」参照）を適用する。"""
    min_col, min_row, max_col, max_row = range_boundaries(ref if ":" in ref else f"{ref}:{ref}")
    for row in range(min_row, max_row + 1):
        for col in range(min_col, max_col + 1):
            _apply_style_dict(ws.cell(row=row, column=col), style)


def write_table(
    ws,
    top_left: str,
    rows: list[list],
    *,
    header: bool = True,
    theme: str | None = None,
    style: dict | None = None,
    row_styles: list[dict | None] | None = None,
    banded: bool = True,
    freeze_header: bool = True,
) -> str:
    """top_left から rows（見出し行を含む2次元リスト）を書き込み、header=True なら
    見出し配色・罫線・縞模様・見出し行固定・列幅調整まで仕上げる。

    row_styles は rows と同じ要素数の style 辞書（色なしは None）のリスト（指定時は縞模様を付けない）。
    戻り値は書き込んだ範囲（例 "A1:C4"）。
    """
    if not rows:
        raise ValueError("rows が空です")
    if row_styles is not None and len(row_styles) != len(rows):
        raise ValueError(f"row_stylesの要素数({len(row_styles)})はrowsの行数({len(rows)})と一致させてください")
    min_row, min_col = coordinate_to_tuple(top_left)
    op: dict = {"rows": rows}
    if style is not None:
        op["style"] = style
    if row_styles is not None:
        op["row_styles"] = row_styles
    if header:
        # row_styles の背景色を縞模様で上書きしないよう、row_styles 指定時は縞模様を付けない
        op["format_table"] = {"theme": theme, "banded": banded and row_styles is None, "freeze_header": freeze_header}
    else:
        op["format_table"] = False
    _write_rows(ws, min_row, min_col, op)
    max_col = min_col + max(len(r) for r in rows) - 1
    max_row = min_row + len(rows) - 1
    return f"{top_left.upper()}:{get_column_letter(max_col)}{max_row}"


def format_table(
    ws,
    ref: str,
    *,
    theme: str | None = None,
    header_fill: str | None = None,
    header_font_color: str | None = None,
    banded: bool = True,
    band_fill: str = "F2F2F2",
    border: str = "thin",
    freeze_header: bool = True,
    autofit: bool = True,
) -> None:
    """書き込み済みの表（ref の1行目=見出し）を後から仕上げる。本体行の文字色・太字は変えない。"""
    min_col, min_row, max_col, max_row = range_boundaries(ref)
    opts = {
        "theme": theme,
        "header_fill": header_fill,
        "header_font_color": header_font_color,
        "banded": banded,
        "band_fill": band_fill,
        "border": border,
        "freeze_header": freeze_header,
        "autofit": autofit,
    }
    _format_table_range(ws, min_row, max_row, min_col, max_col, opts)


def autofit_columns(ws, ref: str | None = None) -> None:
    """ref（省略時はシートの使用範囲）の値から列幅を調整する（全角=2文字換算、既存幅より縮めない）。"""
    min_col, min_row, max_col, max_row = range_boundaries(ref or ws.dimensions)
    values = [[ws.cell(row=r, column=c).value for c in range(min_col, max_col + 1)] for r in range(min_row, max_row + 1)]
    _apply_col_widths(ws, _col_width_from_rows(values, min_col))


def insert_rows(ws, idx: int, amount: int = 1) -> None:
    """idx 行目の前に amount 行挿入する（既存の結合セルも追従させる。ws.insert_rows は追従しない）。"""
    _resize_preserving_merges(ws, "row", idx, amount)


def delete_rows(ws, idx: int, amount: int = 1) -> None:
    """idx 行目から amount 行削除する（既存の結合セルも追従させる）。"""
    _resize_preserving_merges(ws, "row", idx, -amount)


def insert_cols(ws, idx: int, amount: int = 1) -> None:
    """idx 列目の前に amount 列挿入する（既存の結合セルも追従させる）。"""
    _resize_preserving_merges(ws, "col", idx, amount)


def delete_cols(ws, idx: int, amount: int = 1) -> None:
    """idx 列目から amount 列削除する（既存の結合セルも追従させる）。"""
    _resize_preserving_merges(ws, "col", idx, -amount)


def list_groups(ws, column: str) -> list[dict]:
    """column（"A" 等）の同じ値が連続する区間を
    [{"value", "start_row", "end_row", "row_count"}, ...] で返す
    （同じ値の連続・値が空のセル・結合セルの非先頭行は1つの区間にまとめる）。"""
    groups: list[dict] = []
    for group in group_column_values(ws, column):
        prev = groups[-1] if groups else None
        if prev is not None and prev["value"] == group["value"] and prev["end_row"] + 1 == group["start_row"]:
            prev["end_row"] = group["end_row"]
            prev["row_count"] += group["row_count"]
        else:
            groups.append(dict(group))
    return groups


def find_group(ws, column: str, value) -> tuple[int, int]:
    """column の値が value（型も含めて厳密一致）である最初の区間の (start_row, end_row) を返す。"""
    groups = list_groups(ws, column)
    for group in groups:
        if group["value"] == value:
            return group["start_row"], group["end_row"]
    found = [g["value"] for g in groups]
    raise ValueError(f"{column}列に値 {value!r} の区間が見つかりません（存在する値: {found[:30]!r}）")


def style_chart(chart, *, colors: bool = True, data_labels: bool = True):
    """グラフへ既定の見た目（色覚多様性に配慮した系列色・データラベル）を当てて返す。

    add_data/set_categories の後に呼ぶ（系列が無い時点で呼ぶと色が付かない）。
    円グラフは系列色を付けず（Excel既定の配色）、データラベルは%表示にする。
    """
    is_pie = isinstance(chart, PieChart)
    if data_labels:
        chart.dataLabels = DataLabelList(
            showVal=not is_pie,
            showPercent=is_pie,
            showCatName=False,
            showSerName=False,
            showLegendKey=False,
            showBubbleSize=False,
        )
    if colors and not is_pie:
        for i, series in enumerate(chart.series):
            series.graphicalProperties.solidFill = CHART_PALETTE[i % len(CHART_PALETTE)]
    return chart


def place_image(ws, image_path: str, left_cm: float, top_cm: float, width_cm: float | None = None, height_cm: float | None = None) -> None:
    """画像をシート左上原点からのcm座標へ配置する（幅・高さの片方だけ指定ならアスペクト比維持、両方省略で原寸）。"""
    op = {"sheet": ws.title, "image_path": image_path, "left_cm": left_cm, "top_cm": top_cm, "width_cm": width_cm, "height_cm": height_cm}
    op_add_image(ws.parent, op)


def move_image(
    ws,
    image_index: int,
    *,
    left_cm: float | None = None,
    top_cm: float | None = None,
    width_cm: float | None = None,
    height_cm: float | None = None,
) -> None:
    """シート内 image_index 番目（0始まり、追加順）の画像の位置・サイズのうち指定したものだけ変える。"""
    op = {
        "sheet": ws.title,
        "image_index": image_index,
        "left_cm": left_cm,
        "top_cm": top_cm,
        "width_cm": width_cm,
        "height_cm": height_cm,
    }
    op_set_image_position(ws.parent, op)

