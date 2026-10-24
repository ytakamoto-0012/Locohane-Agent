"""show_help(help) ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import logging

from . import _state
from ..i18n import DEFAULT_LANGUAGE, current_lang

logger = logging.getLogger(__name__)


@tool("help")
def show_help() -> str:
    """Return the help text about how to use this system.

    Call it when the user asks for help, how to use it, or where to send feedback.
    The text is written in the configured help Markdown file, and this tool is a thin wrapper that
    just reads and returns it as is (do not make up help content). Relay it to the user in the
    user's language.

    Returns:
        The help text (UTF-8 text, Markdown). If init_tools() has not been run or the help_path
        file does not exist, it returns an "Error: ..." string instead of raising.
    """
    if _state._HELP_PATH is None:
        return "Error: init_tools() has not been run"
    if not _state._HELP_PATH.is_file():
        return f"Error: help file not found: {_state._HELP_PATH}"
    logger.info("show_help")
    # 英語のセッションでは help.en.md があればそちらを返す（無ければ日本語の本文）。
    localized = _state._HELP_PATH.with_name(f"{_state._HELP_PATH.stem}.{current_lang()}{_state._HELP_PATH.suffix}")
    path = localized if current_lang() != DEFAULT_LANGUAGE and localized.is_file() else _state._HELP_PATH
    return path.read_text(encoding="utf-8")


# init_tools() の _resolve_agent_types() がこのリストを実行時に参照するだけなので、
# 定義順は init_tools()/dispatch_agent より後でもよい（analyze_image・メモリー系
# ツールを含めるため、それらの定義後に置く）。
