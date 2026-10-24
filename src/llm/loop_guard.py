"""LLM応答の反復ループ検知（thinking_loop_guard）。

ChatLlamaCpp（chat_model.py）がストリーミング中の応答を監視するために使う。
他の llm パッケージ内モジュールへの依存を持たない自己完結した部品。
"""

from __future__ import annotations

import logging
import random
from difflib import SequenceMatcher
from typing import Any

import httpx
import openai

logger = logging.getLogger(__name__)


class ThinkingLoopDetected(Exception):
    """LLM応答（thinking/本文）が反復ループに陥ったと判定され、生成を打ち切ったことを示す。

    ChatLlamaCpp._stream/_astream がストリーム中に送出する。呼び出し元
    （src/graph.py の ainvoke_ensuring_final_text、app.py の on_message、
    src/subagent.py の run_subagent）がこれを捕捉し、注意メッセージを注入して
    再試行する。

    Attributes:
        snippet: 検知時点までにバッファされていた直近テキスト（末尾最大数百文字）。
            真の反復ループ（同一文の繰り返し）なのか、たまたま圧縮率が下がった
            構造化テキスト（表・JSON等）による誤検知なのかを事後に判別するための
            診断用データ。呼び出し元はこれをログ・eval結果へ残すことができる。
        client_broken: このループ検知に伴うストリームの後始末（_astreamの
            finally節でのagen.aclose()）が失敗し、httpx.AsyncClientの内部
            状態が壊れている可能性が高いことを示すフラグ。True の場合、
            同じクライアント（延いてはそれを使うグラフ）で次のリトライ
            リクエストを送ると、サーバー側に残った旧ストリームのせいで
            応答ヘッダーが永遠に返らずハングする（本番incident・2026-07-20で
            実際に7分11秒間ハングした事例を確認）。呼び出し元はリトライ前に
            aclose_active_llm_clients() でクライアントを強制クローズしてから
            再試行すること（app.pyのon_messageはさらにグラフも再構築する。
            src/subagent.pyの_invoke_with_loop_retryはモデルのみ再構築する。
            src/graph.pyのainvoke_ensuring_final_text（eval用ハーネス、本番
            動作には直結しない）は本対応の優先度を下げており未対応のまま）。
    """

    def __init__(self, message: str, snippet: str = "", client_broken: bool = False) -> None:
        super().__init__(message)
        self.snippet = snippet
        self.client_broken = client_broken


class ToolCallLoopDetected(ThinkingLoopDetected):
    """全く同じ応答（ツール呼び出し）を連続で繰り返すループを、LLMを呼ぶ直前に検知したことを示す。

    src/graph.py がモデル呼び出しの直前に送出する（[tool_loop_guard]、
    src/tool_loop_guard.py の detect_tool_call_loop 参照）。ThinkingLoopDetected
    のサブクラスにして、注意メッセージを注入して再試行する・上限で停止を
    通知する・成功後に注意メッセージを履歴から消す、という既存の仕組みを
    そのまま使う。ストリームは開いていないため client_broken は常に False。

    Attributes:
        detail: 何を繰り返したかの説明（注意メッセージ・停止の通知に添える。
            どうすべきかの指示は含めず、[tool_loop_guard].nudge_messages に任せる）。
        remove_ids: 繰り返した区間のメッセージ id。再試行の前にチェックポイント
            から取り除く（残すとモデルが同じ tool_call を書き写し続けるため）。
    """

    def __init__(self, detail: str, remove_ids: tuple[str, ...] = ()) -> None:
        super().__init__("全く同じツール呼び出しが繰り返されたため打ち切りました", snippet=detail)
        self.detail = detail
        self.remove_ids = remove_ids


def tool_loop_nudge_text(messages: list[str], attempt_index: int, exc: ToolCallLoopDetected) -> str:
    """ツール呼び出しのループを検知したときに注入する注意メッセージを作る。

    [tool_loop_guard].nudge_messages から pick_loop_nudge_message と同じ規則で
    選び、何を繰り返したかの説明（exc.detail）を末尾に添える（どの呼び出しを
    繰り返したのかが分からないと、モデルが同じ呼び出しをまた選びやすいため）。

    Args:
        messages: [tool_loop_guard].nudge_messages 由来の候補（0件でもよい）。
        attempt_index: 0始まりの、ツール呼び出しのループで注意した回数
            （思考のループとは別に数える）。
        exc: 検知した例外。

    Returns:
        注入するメッセージ文字列。
    """
    text = pick_loop_nudge_message(messages, attempt_index, default=_DEFAULT_TOOL_LOOP_NUDGE)
    return f"{text}\n\n{exc.detail}"


_DEFAULT_LOOP_NUDGE = "直前の応答は同じ内容を繰り返すループに陥ったため打ち切りました。" "落ち着いて、今のタスクの続きを行ってください。"
_DEFAULT_TOOL_LOOP_NUDGE = (
    "全く同じツール呼び出しを繰り返しています。同じ呼び出しを繰り返さず、"
    "直前の結果に書かれた指示に従うか、別の手段に切り替えてください。"
)


# openai SDK が httpx 例外をラップしないケースがあるため、両系統を1箇所に集約。
# APIConnectionError はサブクラスに APITimeoutError も含む。
# InternalServerError は 5xx。4xx はリトライ対象に含めない（クライアント側の誤り）。
#
# httpx側は個別の例外クラスを列挙するのではなく、その共通基底クラスである
# httpx.TransportError を使う。ストリーミング応答のヘッダー受信後（チャンク
# 受信中）に発生する低レベル例外はopenai SDKにもラップされず生のhttpx例外が
# そのまま上がってくるため（ChatLlamaCpp._astream参照）、個別列挙方式だと
# 新しい例外型（例: llama-serverプロセスが応答途中で落ちた際のhttpx.ReadError）
# が1つ漏れるたびに on_message の回復経路（_rebuild_checkpointer/
# _rebuild_graph）を素通りしてセッションが復旧不能になる再発を繰り返す
# （本番incident・2026-07-31: httpx.ReadErrorがこのタプルに含まれておらず
# Chainlit最上位ハンドラまで伝播し、グラフ・checkpointerが再構築されないまま
# セッションが壊れ続けた）。基底クラスにしておけば
# ConnectError/ReadError/WriteError/WriteTimeout/PoolTimeout/
# RemoteProtocolError等のトランスポート層例外を将来にわたって網羅できる。
LLM_CONNECTION_ERRORS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    openai.InternalServerError,
    httpx.TransportError,
)


def pick_loop_nudge_message(messages: list[str], attempt_index: int, default: str = _DEFAULT_LOOP_NUDGE) -> str:
    """ループ検知時に注入する注意メッセージを選ぶ。

    attempt_index（0始まり、何回目のループ検知リトライか）が messages の
    範囲内ならその順番のメッセージを、範囲外（用意した数を使い切った場合）は
    ランダムに選ぶ（同じ文言の繰り返しで同じ堂々巡りを誘発しないため）。

    Args:
        messages: config.ini の [thinking_loop_guard].nudge_messages（または
            [tool_loop_guard].nudge_messages）由来のメッセージ候補（0件でもよい）。
        attempt_index: 0始まりのループ検知リトライ回数。
        default: messages が空のときに返す組み込みの既定文言。

    Returns:
        注入するメッセージ文字列。messages が空なら default を返す。
    """
    if not messages:
        return default
    if attempt_index < len(messages):
        return messages[attempt_index]
    return random.choice(messages)


class _ThinkingLoopDetector:
    """LLM応答（thinking/content）のストリーミング中に文章の反復（暴走ループ）を検知する。

    直近window_chars文字（recent window）が、それより前の履歴（最大
    max_history_chars文字に制限、以下MAX_K）の中にどれだけ長く一致する
    部分文字列を持つかを、difflib.SequenceMatcher.find_longest_match()で
    動的に測定する。match_ratio = 最長一致長 / window_chars が
    match_ratio_threshold を上回る状態が confirm_count 回連続したらループと
    判定する。

    旧実装（zlib圧縮率 + 累積n-gram新規性のAND条件）は、固定長ウィンドウ内
    だけを見る設計のため、反復ブロックの周期がウィンドウ長に近い・それを
    超えるケースで検知力が不安定になる根本的な脆弱性を持っていた
    （evals/tuning_log.md iter25参照: 実際の暴走で反復周期422文字に対し
    window_chars=600だと1周期分がウィンドウに収まらず、圧縮率が閾値0.3を
    一度も下回らずThinkingLoopDetectedが51分間発火しない重大な回帰が発生）。
    本方式は固定長Kを仮定せず、その時点で実際にどれだけ長く過去のコピーに
    なっているかを直接測定するため、反復周期の大小に依存しない。真の反復
    ループはmatch_ratioが1.0に収束する一方、値が都度変わる正当なJSON生成は
    低いmatch_ratioを維持する（iter25の合成データ・実測暴走テキスト検証で
    真のループ=1.000、JSON生成=0.09〜0.12、自然文=0.02程度と明確に分離
    することを確認済み）。
    """

    def __init__(
        self,
        window_chars: int,
        check_interval_chars: int,
        confirm_count: int,
        max_history_chars: int,
        match_ratio_threshold: float,
    ) -> None:
        self._buffer = ""
        self._window_chars = window_chars
        self._check_interval_chars = max(check_interval_chars, 1)
        self._confirm_count = max(confirm_count, 1)
        self._max_history_chars = max_history_chars
        self._match_ratio_threshold = match_ratio_threshold
        self._checked_len = 0
        self._consecutive_hits = 0

    def snippet(self, max_chars: int = 400) -> str:
        """検知時点の診断ログ用に、バッファ末尾の直近テキストを返す。"""
        return self._buffer[-max_chars:]

    def feed(self, text: str) -> bool:
        """新しいテキスト断片を追加し、ループが確定判定されたら True を返す。"""
        if not text:
            return False
        self._buffer += text
        if len(self._buffer) < self._window_chars:
            return False
        if len(self._buffer) - self._checked_len < self._check_interval_chars:
            return False
        self._checked_len = len(self._buffer)

        recent = self._buffer[-self._window_chars :]
        history_end = len(self._buffer) - self._window_chars
        history_start = max(0, history_end - self._max_history_chars)
        history = self._buffer[history_start:history_end]

        if history:
            # autojunk=False: 既定Trueだと高反復テキストでマッチ長が0扱いに
            # なる罠がある（iter22の時間軸類似度検証で確認済みの注意点）。
            matcher = SequenceMatcher(None, history, recent, autojunk=False)
            match = matcher.find_longest_match(0, len(history), 0, len(recent))
            match_ratio = match.size / len(recent)
        else:
            match_ratio = 0.0

        hit = match_ratio > self._match_ratio_threshold
        if hit:
            self._consecutive_hits += 1
        else:
            self._consecutive_hits = 0
        if logger.isEnabledFor(logging.DEBUG):
            # 生成が長時間停止ボタン等で強制中断された場合、on_llm_end
            # （正常完了時のみ発火）に頼った全文ログは残らない（2026-07-21、
            # 21分間ThinkingLoopDetectedが一度も発火しないまま暴走し、
            # 停止後に生成内容を一切追跡できなかった事例で判明）。
            # check_interval_charsごとのこのチェックのたびに直近テキストの
            # 断片を残しておけば、強制中断されても暴走時に実際どんな内容が
            # 生成されていたか事後に追跡できる。
            logger.debug(
                "Loop detection check: buffer_len=%d match_ratio=%.3f consecutive_hits=%d recent text=%r",
                len(self._buffer),
                match_ratio,
                self._consecutive_hits,
                recent[-300:],
            )
        return self._consecutive_hits >= self._confirm_count


def _chunk_delta_text(chunk: Any, *, include_reasoning: bool = True, include_content: bool = True) -> str:
    """ストリームチャンク（ChatGenerationChunk）から、監視対象のテキストを取り出す。

    content と reasoning_content（ChatLlamaCpp._convert_chunk_to_generation_chunk
    が additional_kwargs へ拾い上げたもの）のデルタのみを対象にする。tool_call
    引数のJSONは別チャンネル（message.tool_call_chunks）で流れるため、ここでは
    自然に対象外となる。

    include_reasoning=False（[thinking_loop_guard].target = content_only）なら
    content だけを対象にする。思考のループは思考予算（reasoning_budget）で
    打ち切る運用向け。include_content=False（target = thinking_only）なら
    reasoning_content だけを対象にする。
    """
    message = getattr(chunk, "message", None)
    if message is None:
        return ""
    parts = []
    content = getattr(message, "content", None)
    if include_content and isinstance(content, str) and content:
        parts.append(content)
    reasoning = (getattr(message, "additional_kwargs", None) or {}).get("reasoning_content") if include_reasoning else None
    if reasoning:
        parts.append(reasoning)
    return "".join(parts)
