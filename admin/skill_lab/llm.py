"""研究室の LLM 呼び出し（修正・judge・下書きで共有）。

研究テーマの対象インスタンスの設定に、スキル調整ワーカーの LLM 接続先だけを重ねたもの
（worker.py の _llm_config()）で src.llm.build_model を使い、1回の chat completion を行う。モデルは本番と同じ想定。
ローカルの小さめのモデルは JSON を崩すことがあるため、JSON を求める呼び出しは
本文から JSON を取り出し、読めなければ理由を添えて1回だけ出し直させる。
"""

from __future__ import annotations

import asyncio
import json
import re

from src.config import Config

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LabLLMError(RuntimeError):
    """研究室の LLM から使える応答が得られなかった。"""


async def _ainvoke(config: Config, system: str, user_messages: list[tuple[str, str]]) -> str:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from src.llm import build_model

    model = await build_model(config)
    messages = [SystemMessage(content=system)]
    for role, text in user_messages:
        messages.append(HumanMessage(content=text) if role == "user" else AIMessage(content=text))
    response = await model.ainvoke(messages)
    return response.content if isinstance(response.content, str) else str(response.content)


def ask_text(config: Config, system: str, user: str) -> str:
    text = asyncio.run(_ainvoke(config, system, [("user", user)])).strip()
    if not text:
        raise LabLLMError("LLM の応答が空でした。")
    return text


def extract_json(text: str):
    """応答本文から JSON（オブジェクトまたは配列）を取り出す。"""
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            last_error = e
    raise ValueError(f"JSON を読み取れません: {last_error or '本文に JSON がありません'}")


def ask_json(config: Config, system: str, user: str):
    """JSON を求めて呼び、取り出した値を返す（読めなければ1回だけ出し直させる）。"""
    first = asyncio.run(_ainvoke(config, system, [("user", user)]))
    try:
        return extract_json(first)
    except ValueError as e:
        retry = asyncio.run(
            _ainvoke(
                config,
                system,
                [
                    ("user", user),
                    ("assistant", first),
                    ("user", f"今の出力は JSON として読めませんでした（{e}）。説明文を付けず、指定した形式の JSON だけを出力し直してください。"),
                ],
            )
        )
        try:
            return extract_json(retry)
        except ValueError as e2:
            raise LabLLMError(f"LLM の応答を JSON として読めませんでした: {e2}") from e2
