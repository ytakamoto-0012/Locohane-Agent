"""ask_user_choice ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging

from . import _state
from ._ask_echo import _echo_ask_answer
from ._ask_echo import _is_text_ask_cancelled
from ._ask_relay_helper import _ask_with_cross_session_relay
from ._state import _resolve_ask_timeout

logger = logging.getLogger(__name__)


_ASK_CHOICE_CANCEL_VALUE = "__cancel__"
_ASK_CHOICE_OTHER_VALUE = "__other__"
_ASK_CHOICE_CANCEL_MESSAGE = "エラー: ユーザーが選択をキャンセルしました。"
_ASK_CHOICE_CANCEL_LABEL = "❌ キャンセル"


def _multi_select_answer(res: dict) -> str:
    """MultiChoiceForm の送信結果を、ツールの戻り値（兼チャット表示）の文字列にする。"""
    selected = list(res.get("values") or [])
    other = (res.get("other") or "").strip()
    if other:
        selected.append(other)
    return "、".join(selected) if selected else "(選択なし)"


@tool
async def ask_user_choice(question: str, choices: list[str], multi_select: bool = False) -> str:
    """会話を続けるために必要な選択を、ユーザーに選択肢形式で質問する。

    複数の進め方・方針からユーザーに1つ（または複数）選んでもらいたい場合に使う。
    自由記述の回答が必要な場合は AskUserQuestion を使うこと。

    表示される選択肢には常に「✏️ その他（自由入力）」「❌ キャンセル」が
    自動的に追加される。「その他」が選ばれた場合は続けて自由記述の入力欄を
    表示しその回答を返す。「キャンセル」が選ばれた場合は choices に無い
    指示をユーザーがしたいときの離脱手段として機能する。

    Args:
        question: ユーザーに表示する質問文。
        choices: 選択肢の文字列リスト（1件以上）。
        multi_select: True の場合、チェックボックス形式で複数選択できるように
            表示し、選択された選択肢をまとめて返す（未選択のまま送信された
            場合は "(選択なし)" を返す）。False（既定）の場合は従来通り、
            選択肢ボタンをクリックした時点で即座にその1件を選んで返す
            （択一で確定させたい場合はこちら）。

    Returns:
        multi_select=False（既定）は選んだ選択肢の文字列、True は選択した
        選択肢を「、」区切りで連結した文字列（未選択なら "(選択なし)"）。
        いずれも「その他」経由の場合は自由記述の回答テキストになる。
        ユーザーがキャンセルした場合は "エラー: ユーザーが選択をキャンセルしま
        した。" を返す。choices が空の場合や、設定されたタイムアウト秒数以内に
        応答が無い場合も、例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    if not choices:
        return "エラー: choices が空です。1件以上の選択肢を指定してください。"
    logger.info("ask_user_choice: %s choices=%s multi_select=%s", question, choices, multi_select)
    timeout = _resolve_ask_timeout(_state._ASK_USER_CHOICE_TIMEOUT_SECONDS)
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    if multi_select:

        async def _factory_multi():
            # cl.CustomElement はセッション文脈に依存するため、中継時に別セッションで
            # 作り直せるよう毎回ここで新規生成する（_ask_with_cross_session_relay の
            # factory 引数のdocstring参照）。
            element = cl.CustomElement(name="MultiChoiceForm", props={"question": question, "choices": choices})
            ask_message = cl.AskElementMessage(content=question, element=element, timeout=timeout)
            res = await ask_message.send()
            if res is not None:
                submitted = res.get("submitted", True)
                answer = _multi_select_answer(res) if submitted else _ASK_CHOICE_CANCEL_LABEL
                await _echo_ask_answer(ask_message, question, answer)
            return res

        res = await _ask_with_cross_session_relay(thread_id, _factory_multi, timeout)
        if res is None:
            return "エラー: ユーザーからの応答がありませんでした（タイムアウト）。"
        if not res.get("submitted", True):
            return _ASK_CHOICE_CANCEL_MESSAGE
        return _multi_select_answer(res)

    async def _factory_single():
        actions = [cl.Action(name=f"choice_{i}", payload={"value": c}, label=c) for i, c in enumerate(choices)]
        actions.append(cl.Action(name="other", payload={"value": _ASK_CHOICE_OTHER_VALUE}, label="✏️ その他（自由入力）"))
        actions.append(cl.Action(name="cancel", payload={"value": _ASK_CHOICE_CANCEL_VALUE}, label=_ASK_CHOICE_CANCEL_LABEL))
        ask_message = cl.AskActionMessage(content=question, actions=actions, timeout=timeout)
        res = await ask_message.send()
        if res is not None:
            # 「その他」の場合も押したボタンを表示し、続く自由記述の回答は
            # AskTextBar が user_message として表示する。
            await _echo_ask_answer(ask_message, question, res.get("label") or res["payload"].get("value", ""))
        return res

    res = await _ask_with_cross_session_relay(thread_id, _factory_single, timeout)
    if res is None:
        return "エラー: ユーザーからの応答がありませんでした（タイムアウト）。"
    value = res["payload"].get("value") or res.get("label", "")
    if value == _ASK_CHOICE_CANCEL_VALUE:
        return _ASK_CHOICE_CANCEL_MESSAGE
    if value == _ASK_CHOICE_OTHER_VALUE:

        def _factory_other():
            return cl.AskUserMessage(content=question, timeout=timeout).send()

        other_res = await _ask_with_cross_session_relay(thread_id, _factory_other, timeout)
        if other_res is None:
            return "エラー: ユーザーからの応答がありませんでした（タイムアウト）。"
        if _is_text_ask_cancelled(other_res):
            return _ASK_CHOICE_CANCEL_MESSAGE
        return other_res.get("output", "")
    return value
