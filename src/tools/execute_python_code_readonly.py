"""execute_python_code_readonly ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import asyncio
import logging
import subprocess
import tempfile

from . import _state
from ._python_fs_guard import _python_fs_guard_preamble
from ._subprocess_env import _subprocess_env
from ._workdir import _resolve_exec_workdir, _tmp_dir_parents

logger = logging.getLogger(__name__)


@tool
async def execute_python_code_readonly(code: str) -> str:
    """LLMが生成したPythonコードをその場で実行し、標準出力/標準エラーを返す
    （読み取り専用・書き込み不可版）。

    execute_python_code と同じ方式（コード文字列を一時ファイルへ書き出し
    その場で実行）だが、書き込み・削除・改名を場所を問わず一切許可しない。
    規則から「正しい値」を計算する・既存データを読み込んで検算するといった、
    ファイルを一切書き換えない計算・検証専用に使う。成果物ファイルの生成・
    編集にはこのツールではなく execute_python_code、または対応するスキルの
    専用スクリプトを使うこと（このツールでファイルを書こうとしても
    PermissionError になり失敗する）。

    Args:
        code: 実行する Python コード全文。path_memory の @N トークンを使う
           場合は execute_python_code と同じ方法（path_memory.resolve()）
            で実パスへ展開する。

    Returns:
        「[終了コード] N」に続けて、標準出力・標準エラーを連結した文字列。
        code が空の場合、実行が無効化されている場合、タイムアウトした場合、
        起動自体に失敗した場合はいずれも例外を送出せず「エラー: ...」形式で
        返す。コードが書き込み・削除・改名を試みた場合は PermissionError と
        なり、その内容が標準エラーに含まれる。
    """
    if not code.strip():
        return "エラー: code が空です。"
    if not _state._CODE_EXEC_ENABLED:
        return "エラー: LLMが生成したPythonコードの実行はconfig.iniで無効化されています" "（[scripts] code_execution_enabled=false）。"
    workdir = _resolve_exec_workdir()

    try:
        _fs_guard = _python_fs_guard_preamble([], tmp_dir_roots=_tmp_dir_parents(workdir.parent))
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", dir=str(workdir), delete=False, encoding="utf-8")
        tmp.write(_fs_guard + code)
        tmp.close()
        tmp_path = Path(tmp.name)
    except OSError as e:
        return f"エラー: 一時ファイルを作成できませんでした: {e}"

    logger.info("execute_python_code_readonly: cwd=%s", workdir)
    try:
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [_state._SCRIPT_PYTHON, str(tmp_path)],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=_state._SCRIPT_TIMEOUT,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            return f"エラー: コードが {_state._SCRIPT_TIMEOUT} 秒でタイムアウトしました。"
        except OSError as e:
            return f"エラー: コードを実行できませんでした: {e}"
    finally:
        tmp_path.unlink(missing_ok=True)

    parts = [f"[終了コード] {proc.returncode}"]
    if proc.stdout:
        parts.append(f"[標準出力]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        parts.append(f"[標準エラー]\n{proc.stderr.rstrip()}")
    return "\n".join(parts)
