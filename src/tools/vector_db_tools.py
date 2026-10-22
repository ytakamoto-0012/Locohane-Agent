"""ベクターDB（社内文書の意味検索、src/vector_db/）のツール群（読み取り専用）。"""

from __future__ import annotations

from langchain_core.tools import tool
import logging

from ..vector_db import search as vector_search
from ..vector_db.catalog import CatalogError
from ..vector_db.embeddings import EmbeddingError
from ..vector_db.store import StoreError

from . import _state
from ._path_memory_helpers import _register_path_memory

logger = logging.getLogger(__name__)

_DISABLED_MESSAGE = "ベクターDB機能は無効です（config.ini の [vector_db] enabled = false）。社内文書の検索はできません。"


def _enabled_config():
    cfg = _state._LLM_CONFIG
    if cfg is None or not cfg.vector_db_enabled:
        return None
    return cfg


@tool
def list_document_collections() -> str:
    """検索できる社内文書のコレクション（文書群）の一覧を返す。

    search_documents の collection 引数に渡す名前を調べるときに使う。
    読み取り専用のため、計画の有無に関わらずいつでも呼んでよい。

    Returns:
        コレクションごとの名前・説明・文書数・チャンク数・最終取り込み日時のテキスト。
        機能が無効、またはコレクションが無い場合はその旨のテキスト。
    """
    cfg = _enabled_config()
    if cfg is None:
        return _DISABLED_MESSAGE
    try:
        collections = vector_search.list_collections(cfg)
    except (CatalogError, OSError) as e:
        return f"エラー: {e}"
    if not collections:
        return "コレクションはまだありません（管理者が設定ダッシュボードの「ナレッジ」タブで文書を取り込む必要があります）。"
    lines = [f"{len(collections)}件のコレクションがあります。"]
    for c in collections:
        description = f" - {c.description}" if c.description else ""
        ingested = c.last_ingested_at or "未取り込み"
        lines.append(f"- {c.name}{description}（文書 {c.file_count} 件 / チャンク {c.chunk_count} 件 / 最終取り込み {ingested}）")
    return "\n".join(lines)


@tool
def search_documents(query: str, collection: str = "", top_k: int = 0) -> str:
    """取り込み済みの社内文書から、query と意味の近い箇所を探す。

    社内の規程・手順書・仕様書等の内容を問われたときに使う。キーワードの完全一致
    ではなく意味で探すため、query は質問文や要点を短い文で書けばよい。
    結果は抜粋なので、詳しく確かめる必要があれば出典の `@N` を Read に渡して
    原文を読むこと（Read を持たないメインエージェントは explore へ委譲する）。
    回答には出典（ファイル名とページ等）を添えること。
    読み取り専用のため、計画の有無に関わらずいつでも呼んでよい。

    Args:
        query: 探したい内容（例: "溶接ビード幅の検査基準"）。
        collection: 検索するコレクション名。空なら全コレクションを横断して探す。
            名前は list_document_collections で確認できる。
        top_k: 返す最大件数。0 なら既定値（config.ini の [vector_db] top_k）。

    Returns:
        類似度の高い順に「類似度・出典（@N とファイルパス・ページ等）・本文抜粋」を並べたテキスト。
        全体の文字数は上限で切り詰める。該当が無い・機能が無効・エラーの場合はその旨のテキスト
        （エラーは「エラー: ...」形式）。
    """
    cfg = _enabled_config()
    if cfg is None:
        return _DISABLED_MESSAGE
    try:
        result = vector_search.search(cfg, query, collection=collection.strip() or None, top_k=top_k or None)
    except (vector_search.SearchError, EmbeddingError, StoreError, CatalogError) as e:
        return f"エラー: {e}"
    logger.info("search_documents: query=%r collection=%r hits=%d", query, collection, len(result.hits))
    notes = [f"（{name} は検索対象外: {reason}）" for name, reason in result.skipped]
    if not result.hits:
        return "\n".join(["一致する箇所は見つかりませんでした。言い換えた query で探し直すか、collection を確認してください。", *notes])

    path_refs = _register_path_memory(sorted({h.source_path for h in result.hits}), description="search_documents の出典")
    ref_of = {path: ref for ref, path in path_refs.items()}
    budget = cfg.vector_db_max_result_chars
    lines = [f"{len(result.hits)}件見つかりました。"]
    for i, hit in enumerate(result.hits, start=1):
        ref = ref_of.get(hit.source_path)
        source = f"{ref} {hit.source_path}" if ref else hit.source_path
        locator = f" {hit.locator}" if hit.locator else ""
        header = f"[{i}] 類似度 {hit.score:.2f} / {hit.collection} / {source}{locator}"
        text = hit.text.strip()
        remaining = budget - len(header)
        if remaining <= 0:
            lines.append(f"（残り {len(result.hits) - i + 1} 件は文字数の上限のため省略しました）")
            break
        if len(text) > remaining:
            text = text[:remaining] + "…（以下省略。全文は Read で確認）"
        lines.append(f"{header}\n{text}")
        budget -= len(header) + len(text)
    lines.extend(notes)
    return "\n\n".join(lines)
