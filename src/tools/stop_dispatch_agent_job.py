"""stop_dispatch_agent_job ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import asyncio
import logging

from . import _dispatch_agent_job
from ._dispatch_agent_job import _resolve_dispatch_agent_job
from ._script_job import _JOB_OUTPUT_TAIL_CHARS
from .write_scratch_note import _scratch_notes_path_for_run

logger = logging.getLogger(__name__)


@tool
async def stop_dispatch_agent_job(job_id: str) -> str:
    """dispatch_agent で起動したジョブを強制終了する。

    ユーザーから明示的に中断・キャンセル・停止を指示された場合にのみ使う
    こと。処理に時間がかかっていること自体（check_dispatch_agent_job が
    実行中を返し続けること）は、自分の判断で打ち切ってよい理由にはならない。
    他セッションが起動した job_id は操作できない。

    Args:
        job_id: dispatch_agent の戻り値に含まれるID。

    Returns:
        強制終了結果を表す文字列。job_id が不明・他セッションのものである、
        または既に終了済みの場合は「エラー: ...」形式の文字列。
    """
    resolved = _resolve_dispatch_agent_job(job_id)
    if isinstance(resolved, str):
        return resolved
    job = resolved

    if job.status != "running":
        return f"エラー: job_id '{job_id}' は既に終了しています（status={job.status}）。" "check_dispatch_agent_job で結果を取得してください。"

    job.status = "killed"
    logger.warning(
        "dispatch_agent: force-stopping on a stop instruction (run_id=%s, job_id=%s, iter=%d/%d)",
        job.run_id,
        job_id,
        job.current_iteration,
        job.max_iterations,
    )
    if job.runner_task is not None:
        job.runner_task.cancel()
        try:
            await job.runner_task
        except asyncio.CancelledError:
            pass

    note_path = _scratch_notes_path_for_run(job.run_id)
    tail = ""
    if note_path.is_file():
        tail = note_path.read_text(encoding="utf-8", errors="replace")[-_JOB_OUTPUT_TAIL_CHARS:].rstrip()
    _dispatch_agent_job._DISPATCH_AGENT_JOBS.pop(job_id, None)
    if tail:
        return f"強制終了しました。サブエージェントの実行を中断しました。\n[強制終了時点の進捗メモ（末尾）]\n{tail}"
    return "強制終了しました。サブエージェントの実行を中断しました。"
