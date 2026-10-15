"""Read ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import json
import logging

from ._duplicate_guard import _check_file_tools_duplicate
from ._file_tools_common import looks_binary, read_text_with_fallback
from ._path_memory_helpers import _register_path_memory, suggest_from_path_memory
from ._safe_path import _resolve_file_tools_path

logger = logging.getLogger(__name__)


def read_file(path: Path, offset: int = 0, limit: int = 10) -> dict:
    """行番号付きでテキストファイルを読み込む。

    Args:
        path: 読み込む絶対パス。
        offset: 読み飛ばす先頭行数（0始まり）。
        limit: 読み込む最大行数。

    Returns:
        {"path", "total_lines", "start_line", "end_line", "content"}。

    Raises:
        ValueError: ファイルが存在しない・ディレクトリ・バイナリ・
            読み込み権限が無い・読み込みに失敗した場合。
    """
    if not path.exists():
        raise ValueError(f"ファイルが見つかりません: {path}")
    if path.is_dir():
        raise ValueError(f"指定パスはディレクトリです（ファイル専用）: {path}")
    if looks_binary(path):
        raise ValueError(f"バイナリファイルの可能性があるため読み込めません: {path}")

    try:
        text = read_text_with_fallback(path)
    except PermissionError as e:
        raise ValueError(f"ファイルへのアクセス権限がありません: {path}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"ファイル読み込みに失敗しました: {e}") from e

    lines = text.splitlines()
    total_lines = len(lines)
    offset = max(offset, 0)
    selected = lines[offset : offset + limit]

    return {
        "path": str(path),
        "total_lines": total_lines,
        "start_line": offset + 1 if selected else None,
        "end_line": offset + len(selected) if selected else None,
        "content": "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(selected)),
    }


@tool("Read")
def read_tool(file_path: str, offset: int = 0, limit: int = 10) -> str:
    """ローカルファイルシステム上の任意のテキストファイルを行番号付きで読み込む。

    skills ディレクトリ配下限定の read_skill_file とは異なり、パスの制限は
    行わない（ユーザーが指定した任意の絶対パスを読めることが目的。ただし
    `_tmp_<作成時刻>_<短縮ID>` の他セッション分だけは例外で読み取れない）。
    スキル本文・補助資料を読むなら read_skill_file、ユーザーが指定した
    ファイルを読むならこちらを使う。読み取り専用のため、計画の有無に
    関わらずいつでも呼んでよい。

    Args:
        file_path: 読み込む絶対パス（`@N` のパスメモリー参照も指定可）。
            相対パスを指定した場合は作業ディレクトリ基準で解決する。
        offset: 読み飛ばす先頭行数（0始まり、既定0）。
        limit: 読み込む最大行数（既定10）。大きいファイルの続きを読みたい
            場合は offset を前回の end_line に合わせて指定すること。

    Returns:
        `{"path", "total_lines", "start_line", "end_line", "content", "path_memory"}`
        を持つJSON文字列。`content` は "行番号\\t内容" を改行結合した文字列。
        ファイル不在・ディレクトリ指定・バイナリファイル・同一引数での
        再呼び出し（読み取り専用のため上限回数まで）は例外を送出せず
        「エラー: ...」形式の文字列を返す。
    """
    path, error = _resolve_file_tools_path(file_path)
    if error:
        return f"エラー: {error}"
    dup_error = _check_file_tools_duplicate("Read", f"Read\x00{path}\x00{offset}\x00{limit}")
    if dup_error:
        return dup_error
    try:
        result = read_file(path, offset=offset, limit=limit)
    except ValueError as e:
        hint = suggest_from_path_memory(str(path)) if not path.exists() else ""
        return f"エラー: {e}{hint}"
    path_memory = _register_path_memory([result["path"]])
    if path_memory:
        result["path_memory"] = path_memory
    logger.info("Read: %s", path)
    return json.dumps(result, ensure_ascii=False)
