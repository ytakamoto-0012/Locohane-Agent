"""write_scratch_note ツール。"""

from __future__ import annotations

import re
from langchain_core.tools import tool
from pathlib import Path

from ._state import _SUBAGENT_RUN_ID
from ._workdir import _resolve_exec_workdir

_UNSAFE_RUN_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_run_id(raw: str) -> str:
    """run_id をファイル名の一部として安全な文字列に正規化する。

    dispatch_agent は run_id として LLM/フレームワークが生成する
    tool_call_id をそのまま使う（孤立tool_call検出時にtc["id"]から同じ
    run_idを再計算し、退避先ファイルの絶対パスを案内文に埋め込むため）。
    実測値（llama.cpp経由）は英数字のみだが、生成元のサーバー実装に
    フォーマット保証は無く、将来パス区切り文字等を含む値が来ても
    `_scratch_notes_path_for_run` がファイル名として安全に使えるように
    英数字・アンダースコア・ハイフン以外を "_" に置換する。
    """
    cleaned = _UNSAFE_RUN_ID_CHARS.sub("_", raw)
    return cleaned or "unknown"


def _scratch_notes_path_for_run(run_id: str) -> Path:
    """write_scratch_note が書き込むスクラッチファイルの絶対パスを、
    指定した run_id から直接決める（_SUBAGENT_RUN_ID を読まない版）。

    dispatch_agent のジョブを起動したツール呼び出しとは別の
    非同期コンテキスト（check_dispatch_agent_job / stop_dispatch_agent_job）
    から、同じジョブのスクラッチノートを参照するために使う。呼び出し元は
    事前にジョブの thread_id が現在のセッションと一致することを確認して
    いる前提（_resolve_exec_workdir() は cl.user_session から thread_id を
    読むため、一致していなければ別セッションのディレクトリを指してしまう）。
    """
    workdir = _resolve_exec_workdir()
    return workdir / f"_scratch_notes_{run_id}.md"


def _scratch_notes_path() -> Path:
    """write_scratch_note が書き込むスクラッチファイルの絶対パスを決める。

    _resolve_exec_workdir() と同じ `_tmp_<thread_id>` 配下に、現在の
    dispatch_agent 実行（_SUBAGENT_RUN_ID）ごとの専用ファイルを1つ割り当てる。
    サブエージェント外（run_id が無い状態）で呼ばれた場合は "_main" を使う。
    ファイル名はこの関数が決め打ちするため、呼び出し側が任意パスを
    指定することはできない。
    """
    return _scratch_notes_path_for_run(_SUBAGENT_RUN_ID.get() or "_main")


@tool
def write_scratch_note(content: str) -> str:
    """調査中に分かった内容を、その場でスクラッチファイルへ追記する。

    大量のファイルを読み進めながら1つの成果物（要約・抽出データ等）に
    まとめていくような調査タスクで使う。ある程度読み進めるたびにこの
    ツールで分かったことを書き残しておくと、万一このサブエージェント自身が
    トークン上限に達して打ち切られても、ここまでの内容は消えずに残る
    （打ち切り時、委譲元にはこのファイルのパスが案内され、そこから
    続きを判断できる）。

    書き込み先のファイル名はこのツール自身が決めるため任意パスへは書き込めず、ユーザーの
    作業ディレクトリや出力先には一切触れない（execute_python_code の中間生成物と
    同じスクラッチ領域を使う）。

    Args:
        content: 追記する内容（Markdown・JSON文字列など自由形式）。
            これまでの内容は消さず、末尾に追記される。

    Returns:
        書き込み先の絶対パスと、追記後の累計文字数。content が空、または
        書き込みに失敗した場合は例外を送出せず「エラー: ...」形式で返す。
    """
    if not content.strip():
        return "エラー: content が空です。"
    path = _scratch_notes_path()
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
        total_chars = len(path.read_text(encoding="utf-8"))
    except OSError as e:
        return f"エラー: スクラッチファイルへの書き込みに失敗しました: {e}"
    return f"書き込みました: {path}（累計 {total_chars} 文字）"
