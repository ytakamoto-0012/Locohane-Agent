"""lock_plan_mode ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging

from ..plan_persist import persist_plan_state

from . import _state
from ._plan_render import _render_plan_payload

logger = logging.getLogger(__name__)


@tool
async def lock_plan_mode() -> str:
    """Edit Automatically から Plan Mode へ手動で戻す（承認状態を取り消す）。

    全ステップの完了を待たず、途中で自動実行を止めて書き込み系ツールを再びロックしたい
    場合に、ユーザーの承認を介さず自分の判断で呼んでよい。計画（ステップ一覧）自体は
    削除されない。再度書き込み系ツールを使うには、改めて approve_plan で承認を得ること。

    Returns:
        状態変更の結果を伝えるテキスト。
    """
    was_approved = bool(cl.user_session.get("plan_approved"))
    cl.user_session.set("plan_approved", False)
    if was_approved:
        await persist_plan_state()
    plan = cl.user_session.get("plan")
    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None and plan is not None:
        finished = all(s["status"] == "completed" for s in plan)
        message.content = _render_plan_payload(plan, finished=finished, approved=False)
        await message.update()
    logger.info("lock_plan_mode: called (previous state: %s)", "approved" if was_approved else "not approved")
    if not was_approved:
        return "既に Plan Mode です（変更なし）。"
    return "Plan Mode へ戻しました。書き込み系ツールは再びブロックされます。" "再開するには approve_plan で改めて承認を得てください。"


async def toggle_plan_mode_from_ui() -> None:
    """送信ボタン付近の Plan Mode / Edit Automatically バッジをユーザーが
    クリックした際に呼ばれる（app.py の action_callback("toggle_plan_mode")
    経由）。LLMツールではなく、ユーザーがUIから直接操作するための関数。

    計画が存在しない場合は何もしない（切り替える対象が無いため）。また
    config.ini の [plan].allow_badge_unlock が False の場合、Plan Mode →
    Edit Automatically 方向（ロック解除）のクリックは無視する（Edit
    Automatically → Plan Mode 方向のクリックは常に許可する）。
    """
    plan = cl.user_session.get("plan")
    if not plan:
        return
    currently_approved = bool(cl.user_session.get("plan_approved"))
    if not currently_approved and not _state._PLAN_BADGE_ALLOW_UNLOCK:
        logger.info("toggle_plan_mode_from_ui: ignored a click in the unlock direction because allow_badge_unlock=False")
        return
    approved = not currently_approved
    cl.user_session.set("plan_approved", approved)
    await persist_plan_state()
    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None:
        finished = all(s["status"] == "completed" for s in plan)
        message.content = _render_plan_payload(plan, finished=finished, approved=approved)
        await message.update()
    logger.info(
        "toggle_plan_mode_from_ui: the user clicked the badge (new state: %s)",
        "approved" if approved else "not approved",
    )
