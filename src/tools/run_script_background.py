"""run_script_background ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import asyncio
import chainlit as cl
import logging
import shutil
import time
import uuid

from . import _script_job
from . import _state
from ._script_job import _BackgroundJob, _background_job_started_message, _finalize_script_job_result, _purge_stale_background_jobs, _run_background_job
from ._subprocess_env import _run_script_guard_env

logger = logging.getLogger(__name__)


@tool
async def run_script_background(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> str:
    """スキルの scripts/ 配下のスクリプトをバックグラウンドで起動する。

    処理時間が長くなることが見込まれるスクリプト向け。run_script と異なり、
    このターン自体は（下記の安全上限に達しない限り）完了までブロックされる
    （Chainlit UI上は「実行中」表示が続く）。待っている間、人間向けに
    チャットへ直接進捗（経過秒数・標準出力/標準エラー末尾）が自動で通知
    されるため、自分から check_script_job を繰り返し呼んでポーリングする
    必要は無い（進捗はコード側が直接チャットへ送るため、LLMの呼び出し回数・
    トークン消費は増えない）。1回の呼び出しで run_script と同じ形式の
    最終結果がそのまま返る。

    設定した安全上限（[scripts].background_inline_wait_max_seconds）を
    超えてもなお完了しない場合に限り、job_id を含む案内文を返してこのターンを
    終える（ジョブ自体は裏側で動き続ける）。この場合のみ、後続ターンで
    check_script_job（結果取得）・stop_script_job（明示的な中断指示があった
    場合のみ）を使う。

    引数解決・作業ディレクトリ解決（例外の扱いも含む）、
    および書き込みサンドボックスガード（出力先は作業ディレクトリ/
    default_workdir配下限定、詳細は run_script のdocstring参照）は
    run_script と同じ。バックグラウンドジョブを強制終了するまでの上限は
    既定3600秒。

    Args:
        skill_name: スクリプトを持つスキルのフォルダ名。
        script_filename: 実行したいスクリプトのファイル名（例: "count.py"）。
        script_args: スクリプトへ渡す追加引数のリスト（省略可）。オプションと
            値は別要素にする（例: ["--file", "C:\\aaa.png"]）。

    Returns:
        通常は run_script と同じ形式の最終結果文字列。安全上限に達した
        場合のみ job_id を含む案内文字列。引数不正・スクリプトが見つからない・
        起動自体に失敗した場合は run_script 同様「エラー: ...」
        形式の文字列を返す（この場合 job は作られない）。
    """
    prepared = _script_job._prepare_script_execution(skill_name, script_filename, script_args)
    if isinstance(prepared, str):
        return prepared
    cmd, workdir = prepared
    env, guard_dir = _run_script_guard_env(workdir, skill_name, script_filename)

    _purge_stale_background_jobs()

    logger.info("run_script_background: %s %s cwd=%s", skill_name, script_filename, workdir)
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)
        return f"エラー: スクリプトを起動できませんでした: {e}"

    job_id = uuid.uuid4().hex[:12]
    job = _BackgroundJob(
        process=process,
        thread_id=cl.user_session.get("thread_id") or "",
        skill_name=skill_name,
        script_filename=script_filename,
        started_at=time.monotonic(),
        stdout_chunks=[],
        stderr_chunks=[],
        status="running",
        returncode=None,
        error_message=None,
        guard_dir=guard_dir,
    )
    job.runner_task = asyncio.create_task(_run_background_job(job, job_id))
    _script_job._BACKGROUND_JOBS[job_id] = job

    # asyncio.shield: このwait_forがタイムアウトして例外を送出しても、
    # job.runner_task 自体はキャンセルされず裏側で動き続ける
    # （安全上限超過時のフォールバック挙動の要。dispatch_agent と同じ設計）。
    # 0以下は無期限待ち。
    wait_timeout = _state._SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS if _state._SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS > 0 else None
    try:
        await asyncio.wait_for(asyncio.shield(job.runner_task), timeout=wait_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "run_script_background: 安全上限(%s秒)に達したため job_id を返してターンを終えます: job_id=%s",
            wait_timeout,
            job_id,
        )
        return _background_job_started_message(job_id)

    return _finalize_script_job_result(job, job_id)
