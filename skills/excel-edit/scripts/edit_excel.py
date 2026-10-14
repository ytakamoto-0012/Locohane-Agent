"""openpyxl のPythonコードを実行して xlsx/xlsm を新規作成または編集する。

excel-edit スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script から
    python edit_excel.py <path> --code "<Pythonコード>" [--new] [--overwrite] [--output <別名保存先>]
または
    python edit_excel.py <path> --code-file <.pyファイルのパス> [--new] [--overwrite] [--output ...]
の形で呼ばれる。

コードは、読み込み済み（--new 時は0シートの空ブック）の openpyxl Workbook を
変数 `wb` として受け取り、それを書き換えるだけでよい。ブックの読み込み
（.xlsm は keep_vba=True）・新規作成時の上書きガード・保存直前のバックアップ・
保存・path_memory への登録はこのスクリプトが行う。コード内での wb.save() は
禁止（バックアップを経ない保存や別パスへの保存で .xlsm のマクロを失う事故を防ぐ）。
コードの名前空間には openpyxl の主要クラスと xlsx_helpers.py のヘルパーを
あらかじめ入れてある（_build_namespace 参照）。

コードの実行に失敗した場合は何も保存しない（途中までの変更が中途半端に
残ったファイルを作らないため）。

以前は ops（操作）のJSON配列を受け取る方式だった。その方式との比較検証用の実装。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import traceback
from pathlib import Path

# office_shared/excel_common.py から共有ヘルパーを import する（1-B 相互import方式）
_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from excel_common import backup_before_overwrite, register_output_path, setup_utf8_stdio  # noqa: E402

_CODE_FILENAME = "<code>"
# コードが print した内容を結果JSONへ載せる上限（会話履歴の肥大化を防ぐ）
_MAX_CODE_OUTPUT_CHARS = 4000


def _load_code(args: argparse.Namespace) -> str:
    if args.code is not None:
        return args.code
    code_path = Path(args.code_file)
    if not code_path.is_file():
        raise FileNotFoundError(f"コードファイルが見つかりません: {args.code_file}")
    try:
        return code_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return code_path.read_text(encoding="cp932")


def _build_namespace(wb) -> dict:
    """コードへ渡す名前空間。import を書かなくてもよく使うものはそのまま使えるようにする。"""
    import openpyxl
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference, ScatterChart
    from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule, FormulaRule, IconSetRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import column_index_from_string, get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.worksheet.table import Table, TableStyleInfo

    import xlsx_helpers

    ns = {
        "__name__": "__excel_edit_code__",
        "wb": wb,
        "openpyxl": openpyxl,
        "Font": Font,
        "PatternFill": PatternFill,
        "Border": Border,
        "Side": Side,
        "Alignment": Alignment,
        "get_column_letter": get_column_letter,
        "column_index_from_string": column_index_from_string,
        "BarChart": BarChart,
        "LineChart": LineChart,
        "PieChart": PieChart,
        "ScatterChart": ScatterChart,
        "Reference": Reference,
        "Table": Table,
        "TableStyleInfo": TableStyleInfo,
        "DataValidation": DataValidation,
        "CellIsRule": CellIsRule,
        "ColorScaleRule": ColorScaleRule,
        "DataBarRule": DataBarRule,
        "FormulaRule": FormulaRule,
        "IconSetRule": IconSetRule,
    }
    for name in xlsx_helpers.__all__:
        ns[name] = getattr(xlsx_helpers, name)
    return ns


def _format_code_error(exc: BaseException, code: str) -> str:
    """コード由来の例外を「何行目の何で失敗したか」が分かる形に整える。

    スクリプト内部（exec の呼び出し元）のフレームは利用者にとってノイズなので、
    コード本体（<code>）以降のフレームだけを残す。
    """
    lines = code.splitlines()
    if isinstance(exc, SyntaxError) and exc.filename == _CODE_FILENAME:
        src = lines[exc.lineno - 1].strip() if exc.lineno and 0 < exc.lineno <= len(lines) else ""
        return f"コードの構文エラーです（{exc.lineno}行目）: {exc.msg}\n  {src}"

    tb = traceback.extract_tb(exc.__traceback__)
    code_frames = [i for i, f in enumerate(tb) if f.filename == _CODE_FILENAME]
    head = f"コードの実行に失敗しました: {type(exc).__name__}: {exc}"
    if not code_frames:
        return head
    last_code = tb[code_frames[-1]]
    src = lines[last_code.lineno - 1].strip() if last_code.lineno and 0 < last_code.lineno <= len(lines) else ""
    detail = "".join(traceback.format_list(tb[code_frames[0] :]))
    return f"{head}\n失敗した箇所: コード{last_code.lineno}行目: {src}\n{detail.rstrip()}"


class _SaveForbiddenError(RuntimeError):
    pass


def _forbid_save(*_args, **_kwargs):
    raise _SaveForbiddenError(
        "コード内で wb.save() は呼べません（保存はedit_excel.pyが行います。別名で保存したい場合は --output を指定してください）"
    )


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="編集対象（--new時は作成先）のxlsx/xlsmパス")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--code", help="実行するopenpyxlのPythonコードをそのまま渡す")
    group.add_argument("--code-file", help="実行するopenpyxlのPythonコードを書いたファイルのパス")
    parser.add_argument("--new", action="store_true", help="既存ファイルを読まず新規ワークブックとして作成する")
    parser.add_argument("--overwrite", action="store_true", help="--new時に出力先が既に存在する場合の上書きを許可する")
    parser.add_argument("--output", default=None, help="保存先パス（省略時は path へ上書き保存）")
    args = parser.parse_args()

    # LLMが書いたコードを実行するため、execute_python_code と同じく
    # [scripts] code_execution_enabled=false の環境では実行しない。
    if os.environ.get("AGENT_CODE_EXECUTION_ENABLED") == "0":
        print(
            "LLMが生成したPythonコードの実行はconfig.iniで無効化されているため、" "excel-editは使えません（[scripts] code_execution_enabled=false）",
            file=sys.stderr,
        )
        return 1

    path = Path(args.path)
    ext = path.suffix.lower()
    if ext not in (".xlsx", ".xlsm"):
        print(f"対応拡張子は .xlsx/.xlsm のみです: {ext}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else path

    try:
        code = _load_code(args)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not code.strip():
        print("コードが空です", file=sys.stderr)
        return 1

    import openpyxl

    if args.new:
        if path.exists() and not args.overwrite:
            print(f"作成先が既に存在します（上書きするには --overwrite を指定）: {args.path}", file=sys.stderr)
            return 1
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
    else:
        if not path.is_file():
            print(f"編集対象のファイルが見つかりません（新規作成する場合は --new を指定）: {args.path}", file=sys.stderr)
            return 1
        try:
            wb = openpyxl.load_workbook(str(path), keep_vba=(ext == ".xlsm"))
        except Exception as e:  # noqa: BLE001 - 破損ファイル等、openpyxlが送出する多様な例外を丸めて報告する
            print(f"ファイルの読み込みに失敗しました: {e}\n{traceback.format_exc()}", file=sys.stderr)
            return 1

    ns = _build_namespace(wb)
    wb.save = _forbid_save
    captured = io.StringIO()
    try:
        compiled = compile(code, _CODE_FILENAME, "exec")
        with contextlib.redirect_stdout(captured):
            exec(compiled, ns)  # noqa: S102 - LLMが書いたopenpyxlコードの実行がこのスクリプトの役割
    except Exception as e:  # noqa: BLE001 - コード由来の任意の例外を行番号付きで報告する
        out = captured.getvalue()
        if out:
            print(f"[コードの出力]\n{out[:_MAX_CODE_OUTPUT_CHARS]}", file=sys.stderr)
        print(_format_code_error(e, code), file=sys.stderr)
        if isinstance(e, KeyError):
            print(f"（このブックのシート一覧: {wb.sheetnames}）", file=sys.stderr)
        print("（コードが失敗したため、ファイルは保存していません）", file=sys.stderr)
        return 1
    finally:
        del wb.save

    if ns.get("wb") is not wb:
        print(
            "コード内で変数 wb を別のブックに差し替えないでください（load_workbook/Workbook() で作り直すと"
            ".xlsmのマクロや--newの上書きガードが効かなくなります）。渡された wb をそのまま編集してください",
            file=sys.stderr,
        )
        return 1

    if not wb.sheetnames:
        print("最終的にシートが1つも存在しません（--new時は wb.create_sheet() でシートを作成してください）", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_before_overwrite(output_path)
    try:
        wb.save(str(output_path))
    except OSError as e:
        print(f"ファイルの保存に失敗しました: {e}", file=sys.stderr)
        return 1

    result = {
        "path": str(output_path),
        "backup_path": str(backup_path) if backup_path else None,
        "sheets": wb.sheetnames,
    }
    out = captured.getvalue()
    if out:
        result["code_output"] = out[:_MAX_CODE_OUTPUT_CHARS]
    path_memory = register_output_path(output_path, description="edit_excelが生成/更新したファイル")
    if path_memory:
        result["path_memory"] = path_memory
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
