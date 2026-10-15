"""execute_python_code ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import asyncio
import chainlit as cl
import logging
import subprocess
import tempfile

from . import _state
from ._duplicate_guard import _track_failure_streak
from ._python_fs_guard import _exec_guard_roots, _python_fs_guard_preamble, _register_exec_output_files
from ._subprocess_env import _subprocess_env
from ._workdir import _resolve_exec_workdir, _tmp_dir_parents

logger = logging.getLogger(__name__)


@tool
async def execute_python_code(code: str) -> str:
    """LLMが生成したPythonコードをその場で実行し、標準出力/標準エラーを返す。

    run_script が skills/*/scripts/ 配下の既存ファイルしか実行できないのに対し、
    このツールはコード文字列を一時ファイルへ書き出してその場で実行する。任意コード
    実行はリスクが高いため、サーバー設定で無効化されている場合は実行せずエラーを
    返す。

    **cwd（作業ディレクトリ）は run_script とは異なる**。run_script の cwd は
    ユーザーが設定した作業ディレクトリだが、このツールの cwd は常に
    default_workdir 配下のこのセッション専用の一時フォルダ
    （`_tmp_<thread_id>`）になる（ユーザーの作業ディレクトリが何であっても
    変わらない）。このコードが相対パスで書き出すファイル（中間生成物）は
    そちらへ溜まり、LLMがユーザーの作業ディレクトリを直接汚さないように
    している。ユーザーの作業ディレクトリを狙う必要はなく、単純に相対パス
    （例: `open("ops.json", "w", ...)`）で書けばよい。生成・更新された
    ファイルは実行後に自動検知して path_memory（`@N`）へ登録し、戻り値に
    含める（後続の run_script 等へは `@N` かこのツールが返す絶対パスを
    そのまま渡せる。run_script 側もこの一時フォルダを読み取れるため、
    cwd が違うことは問題にならない）。タイムアウトや Python 実行ファイルは
    run_script と共通の設定を流用する。

    **重要: パスメモリ(@N)の活用**
    Globや他のツールで取得したファイルパス（@0, @1, @2…）を、このツールの
    code引数で使う場合、code内で `path_memory.resolve()` を呼び出して
    実パスへ展開する必要があります。環境変数 `AGENT_SRC_DIR` で `src/`
    ディレクトリが利用可能なので、以下のようにインポート・展開できます:

      import os, sys
      sys.path.insert(0, os.environ.get("AGENT_SRC_DIR", ""))
      import path_memory
      thread_id = os.environ.get("AGENT_THREAD_ID", "_no_session")
      pm_dir = os.environ.get("AGENT_PATH_MEMORY_DIR", "")
      if pm_dir:
          resolved = path_memory.resolve(thread_id, "@0", Path(pm_dir))
          print(open(resolved).read()[:500])

    ファイル一覧をcode内にリテラルリストとして書き写す必要は絶対にない。

    **重要: ファイル数上限**
    code引数内へファイル名をリテラルとしてリスト化する場合、**30件を超えると**
    トークン爆発（会話履歴の肥大化）を引き起こす。ファイルが30件を超す場合は
    code内にリスト化せず、globやpathlibでディレクトリ探索を行うか、
    run_script で既存スクリプトを呼び出す方式を優先すること。

    **重要: 委譲の原則**
    ファイル調査・比較・集計等の処理は、可能な限り既存のスキルや
    run_script で実装されたスクリプトへ委譲すること。execute_python_codeは
    簡易なスクリプト実行やプロトタイピングに限定し、複雑なデータ処理や
    大規模なファイル操作は避ける。

    **重要: 書き込みは作業ディレクトリ配下限定（サンドボックス）**
    このコードは実行前ガードにより、書き込み・削除・改名の類が作業
    ディレクトリ・自セッション専用の一時フォルダ（`_tmp_<thread_id>`。
    このコードのcwdそのもの）以外では自動的にブロックされる
    （PermissionErrorで失敗する。Locohaneのプロジェクトフォルダ
    〔src/・app.py・config.ini・skills/ 等〕、それ以外の任意のドライブ・
    フォルダに加え、default_workdir直下の`_tmp_<thread_id>`以外の場所
    （サーバー側の共有フォルダのため他セッションから見えてしまう）も
    含めて、書き込みは一切できない）。プロジェクト自体の設定やソース
    コードを変更する必要がある場合はこのツールを使わず、ユーザーへ
    直接の編集を依頼すること。
    読み取りはこのガードの対象外で従来通り制限されない。

    Args:
        code: 実行する Python コード全文。path_memory の @N トークン
            （例: @0, @1）を含める場合、code内で `path_memory.resolve()`
            を呼び出して実パスへ展開する必要がある（上記「パスメモリの活用」
            参照）。

    Returns:
        「[終了コード] N」に続けて、標準出力・標準エラー、生成/更新
        ファイルの path_memory 参照（あれば）を、それぞれ見出し付きで
        連結した文字列。code が空の場合、実行が無効化されている場合、
        タイムアウトした場合、起動自体に失敗した場合はいずれも例外を送出せず「エラー: ...」形式で返す。
    """
    if not code.strip():
        return "エラー: code が空です。"
    if not _state._CODE_EXEC_ENABLED:
        return "エラー: LLMが生成したPythonコードの実行はconfig.iniで無効化されています" "（[scripts] code_execution_enabled=false）。"
    workdir = _resolve_exec_workdir()

    try:
        before_snapshot = {p: p.stat().st_mtime for p in workdir.iterdir() if p.is_file()}
    except OSError:
        before_snapshot = {}

    try:
        _guard_allowed_roots, _guard_display_roots = _exec_guard_roots()
        _fs_guard = _python_fs_guard_preamble(
            _guard_allowed_roots, tmp_dir_roots=_tmp_dir_parents(workdir.parent), display_roots=_guard_display_roots
        )
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", dir=str(workdir), delete=False, encoding="utf-8")
        tmp.write(_fs_guard + code)
        tmp.close()
        tmp_path = Path(tmp.name)
    except OSError as e:
        return f"エラー: 一時ファイルを作成できませんでした: {e}"

    logger.info("execute_python_code: cwd=%s", workdir)
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

    path_memory_note = _register_exec_output_files(workdir, before_snapshot, cl.user_session.get("thread_id") or "_no_session")
    if path_memory_note:
        parts.append(path_memory_note)

    warning = _track_failure_streak("execute_python_code_failure_streak", proc.returncode != 0, "execute_python_code")
    if warning:
        parts.append(warning)
    return "\n".join(parts)
