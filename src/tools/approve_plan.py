"""approve_plan ツールと Plan Mode バッジの手動切り替え。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging

from ..plan_persist import persist_plan_state

from . import _state
from ._ask_relay_helper import _ask_with_cross_session_relay
from ._plan_render import _render_plan, _render_plan_payload
from ._state import _resolve_ask_timeout
from ..i18n import t

logger = logging.getLogger(__name__)


@tool
async def approve_plan() -> str:
    """作成済みの実行計画についてユーザーの承認を得る。

    計画内容を提示し、承認/拒否を選ばせる。承認されると、以後
    run_script/run_script_background/execute_python_code/
    execute_python_code_background のハードブロックが解除され実行できるように
    なる。タイムアウト（未応答）は安全側に倒して未承認扱いにするが、ユーザーが
    明示的に却下した場合とは返り値のテキストで区別する（無応答は単に手が
    離せないだけの可能性が高く、計画自体を作り直す必要はないため）。

    config.ini の [plan].auto_approve が true の場合、承認/却下ボタンの表示・
    応答待ちを一切行わず、その場で自動的に承認済み扱いにする（無人自動化用途向け）。

    Returns:
        承認・明示的却下・タイムアウトのいずれかを伝えるテキスト。計画が未作成の
        場合は例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    plan = cl.user_session.get("plan")
    if not plan:
        return "エラー: 計画がありません。先に create_plan を呼んでください。"
    if _state._PLAN_AUTO_APPROVE:
        cl.user_session.set("plan_approved", True)
        cl.user_session.set("plan_denied_just_now", False)
        await persist_plan_state()
        message: cl.Message | None = cl.user_session.get("plan_message")
        if message is not None:
            message.content = _render_plan_payload(plan, approved=True)
            await message.update()
        await cl.Message(content=t("plan.auto_approved")).send()
        logger.info("approve_plan: auto_approve=true のため自動承認しました")
        return "config.ini の [plan].auto_approve が有効なため、ユーザーへの確認をスキップして自動承認しました。書き込み系ツール（run_script/execute_python_code）を実行できます。"
    content = _render_plan(plan, heading=t("plan.heading")) + "\n\n" + t("plan.approve_question")
    actions = [
        cl.Action(name="approve", payload={"value": "approve"}, label=t("plan.approve_button")),
        cl.Action(name="deny", payload={"value": "deny"}, label=t("plan.deny_button")),
    ]
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    timeout = _resolve_ask_timeout(_state._APPROVAL_TIMEOUT_SECONDS)

    def _factory():
        return cl.AskActionMessage(content=content, actions=actions, timeout=timeout).send()

    res = await _ask_with_cross_session_relay(thread_id, _factory, timeout)
    approved = res is not None and res["payload"].get("value") == "approve"
    cl.user_session.set("plan_approved", approved)
    # 前回却下時に立てたフラグが誤って残らないよう、承認・タイムアウト時は
    # 明示的にクリアする（このターンは却下ではないため）。
    cl.user_session.set("plan_denied_just_now", False)
    await persist_plan_state()
    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None:
        message.content = _render_plan_payload(plan, approved=approved)
        await message.update()
    if approved:
        logger.info("approve_plan: 承認されました")
        return "ユーザーが計画を承認しました。書き込み系ツール（run_script/execute_python_code）を実行できます。"
    if res is None:
        logger.info("approve_plan: 応答なし（タイムアウト）")
        return (
            f"ユーザーからの応答が{_state._APPROVAL_TIMEOUT_SECONDS}秒間ありませんでした"
            "（離席中の可能性があります）。計画自体はそのまま保持されているので、"
            "作り直す必要はありません。少し時間を置いてから改めて approve_plan を"
            "呼び直してください。"
        )
    logger.info("approve_plan: 明示的に却下されました")
    # app.py の on_tool_end がこのフラグを見て、ツール呼び出しを続けさせず
    # このターンの処理を強制的に打ち切る（LLMが「計画を微修正して続行しよう」
    # と自己判断してしまうのを、プロンプト指示だけに頼らずコード側で確実に防ぐため）。
    cl.user_session.set("plan_denied_just_now", True)
    return "ユーザーが計画を却下しました。これ以上ツールを呼ばず、" "却下された旨を最終回答として述べて処理を終了してください。"
