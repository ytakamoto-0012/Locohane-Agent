"""execute_python_code_background ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import asyncio
import chainlit as cl
import logging
import tempfile
import time
import uuid

from . import _script_job
from . import _state
from ._python_fs_guard import _exec_guard_roots, _python_fs_guard_preamble
from ._script_job import _BackgroundJob, _background_job_started_message, _finalize_script_job_result, _purge_stale_background_jobs, _run_background_job
from ._subprocess_env import _subprocess_env
from ._workdir import _resolve_exec_workdir, _tmp_dir_parents

logger = logging.getLogger(__name__)


@tool
async def execute_python_code_background(code: str) -> str:
    """LLMが生成したPythonコードをバックグラウンドで実行する。

    処理時間が長くなることが見込まれるコード向け。execute_python_code と
    異なり、このターン自体は（下記の安全上限に達しない限り）完了まで
    ブロックされる（Chainlit UI上は「実行中」表示が続く）。待っている間、
    人間向けにチャットへ直接進捗（経過秒数・標準出力/標準エラー末尾）が
    自動で通知されるため、自分から check_script_job を繰り返し呼んで
    ポーリングする必要は無い（進捗はコード側が直接チャットへ送るため、
    LLMの呼び出し回数・トークン消費は増えない）。1回の呼び出しで
    execute_python_code と同じ形式の最終結果がそのまま返る
    （run_script_background のジョブと共通のレジストリ・ツールで扱われる）。

    設定した安全上限（[scripts].background_inline_wait_max_seconds）を
    超えてもなお完了しない場合に限り、job_id を含む案内文を返してこのターンを
    終える（ジョブ自体は裏側で動き続ける）。この場合のみ、後続ターンで
    check_script_job（結果取得）・stop_script_job（明示的な中断指示があった
    場合のみ）を使う。

    引数チェック・作業ディレクトリ解決・実行可否チェックは
    execute_python_code と同じ。生成・更新されたファイルは完了時に自動検知して
    path_memory（`@N`）へ登録し、結果に含める。
    バックグラウンドジョブを強制終了するまでの上限は既定3600秒。

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
        code: 実行する Python コード全文。

    Returns:
        通常は execute_python_code と同じ形式の最終結果文字列。安全上限に
        達した場合のみ job_id を含む案内文字列。code が空・実行が
        無効化されている・一時ファイル作成や起動自体に
        失敗した場合は execute_python_code 同様「エラー: ...」形式の
        文字列を返す（この場合 job は作られない）。
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

    _purge_stale_background_jobs()

    logger.info("execute_python_code_background: cwd=%s", workdir)
    try:
        process = await asyncio.create_subprocess_exec(
            _state._SCRIPT_PYTHON,
            str(tmp_path),
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(),
        )
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        return f"エラー: コードを起動できませんでした: {e}"

    job_id = uuid.uuid4().hex[:12]
    job = _BackgroundJob(
        process=process,
        thread_id=cl.user_session.get("thread_id") or "",
        skill_name="",
        script_filename=tmp_path.name,
        started_at=time.monotonic(),
        stdout_chunks=[],
        stderr_chunks=[],
        status="running",
        returncode=None,
        error_message=None,
        tmp_path=tmp_path,
        workdir=workdir,
        before_snapshot=before_snapshot,
    )
    job.runner_task = asyncio.create_task(_run_background_job(job, job_id))
    _script_job._BACKGROUND_JOBS[job_id] = job

    wait_timeout = _state._SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS if _state._SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS > 0 else None
    try:
        await asyncio.wait_for(asyncio.shield(job.runner_task), timeout=wait_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "execute_python_code_background: 安全上限(%s秒)に達したため job_id を返してターンを終えます: job_id=%s",
            wait_timeout,
            job_id,
        )
        return _background_job_started_message(job_id)

    return _finalize_script_job_result(job, job_id)
