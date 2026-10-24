"""show_help(help) ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import logging

from . import _state
from ..i18n import DEFAULT_LANGUAGE, current_lang

logger = logging.getLogger(__name__)


@tool("help")
def show_help() -> str:
    """このシステムの使い方に関するヘルプ本文を返す。

    ユーザーがヘルプや使い方、フィードバックの窓口について尋ねてきた場合に呼ぶ。
    本文は設定されたヘルプ用Markdownファイルに記述されており、このツールは
    その内容をそのまま読み込んで返すだけの薄いラッパー（憶測でヘルプ内容を
    生成しない）。

    Returns:
        ヘルプ本文（UTF-8 テキスト、Markdown形式）。init_tools() が未実行、
        または help_path のファイルが存在しない場合は、例外を送出せず
        「エラー: ...」形式の文字列を返す。
    """
    if _state._HELP_PATH is None:
        return "エラー: init_tools() が未実行です"
    if not _state._HELP_PATH.is_file():
        return f"エラー: ヘルプファイルが見つかりません: {_state._HELP_PATH}"
    logger.info("show_help")
    # 英語のセッションでは help.en.md があればそちらを返す（無ければ日本語の本文）。
    localized = _state._HELP_PATH.with_name(f"{_state._HELP_PATH.stem}.{current_lang()}{_state._HELP_PATH.suffix}")
    path = localized if current_lang() != DEFAULT_LANGUAGE and localized.is_file() else _state._HELP_PATH
    return path.read_text(encoding="utf-8")


# init_tools() の _resolve_agent_types() がこのリストを実行時に参照するだけなので、
# 定義順は init_tools()/dispatch_agent より後でもよい（analyze_image・メモリー系
# ツールを含めるため、それらの定義後に置く）。
