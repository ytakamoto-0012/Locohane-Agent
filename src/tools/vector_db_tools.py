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
# Hit.matched（一致した埋め込み）の表示名。
_MATCHED_LABELS = {"text": "本文", "keyword": "キーワード", "image": "画像"}
# search_document_paths の Hit.matched の表示名。
_PATH_MATCHED_LABELS = {"path": "意味", "path_keyword": "キーワード"}


def _enabled_config():
    cfg = _state._LLM_CONFIG
    if cfg is None or not cfg.vector_db_enabled:
        return None
    return cfg


@tool
def list_document_collections() -> str:
    """検索できる社内文書のコレクション（文書群）の一覧を返す。

    search_documents・search_document_paths の collection 引数に渡す名前を調べるときに使う。
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
    「一致: 画像」を含む結果は、ページの見た目（図・表・写真等）が一致している。
    中身を確かめるには、出典が画像ファイルならその `@N` を analyze_image に渡し、
    PDF・Office なら render 系スキル（pdf-tools / docx-render / pptx-render /
    excel-render）でそのページを画像にしてから analyze_image で見る。
    写真・図面のように中身の文字から見つからないファイルは、search_document_paths で
    ファイル名・フォルダ名から探せる。
    回答には出典（ファイル名とページ等）を添えること。
    読み取り専用のため、計画の有無に関わらずいつでも呼んでよい。

    Args:
        query: 探したい内容（例: "溶接ビード幅の検査基準"）。
        collection: 検索するコレクション名。空なら全コレクションを横断して探す。
            名前は list_document_collections で確認できる。
        top_k: 返す最大件数。0 なら既定値（config.ini の [vector_db] top_k）。

    Returns:
        関連の高い順に「類似度・一致（本文 / キーワード / 画像）・出典（@N とファイルパス・ページ等）・本文抜粋」を
        並べたテキスト。同じファイル・同じページのチャンクは1件にまとめ、最も一致したチャンクを抜粋にする。
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
        matched = "・".join(_MATCHED_LABELS[m] for m in hit.matched) or _MATCHED_LABELS["text"]
        # キーワードだけの一致には類似度が無い（BM25 の値はコサイン類似度と比べられない）。
        score = f"類似度 {hit.score:.2f}" if set(hit.matched) - {"keyword"} else "類似度 -"
        header = f"[{i}] {score}（一致: {matched}）/ {hit.collection} / {source}{locator}"
        if hit.other_chunks:
            header += f"（同じ位置で他 {hit.other_chunks} チャンクも一致）"
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


@tool
def search_document_paths(query: str, collection: str = "", top_k: int = 0) -> str:
    """取り込み済みの社内文書から、ファイル名・フォルダ名（パス）が query と意味の近いファイルを探す。

    写真・図面・スキャン画像のように中身の文字から探せないファイルは、ファイル名や
    フォルダ名に内容が書かれていることが多い。search_documents で見つからないとき、
    または「〜の写真」「〜の図面」のようにファイルそのものを探すときに使う。
    パスの文字列を意味とキーワード（型番・日付等の完全一致）の両方で比べるため、
    query は探したいものを短い語句で書けばよい。
    見つかった画像ファイルの中身は、その `@N` を analyze_image に渡して確かめる。
    PDF・Office ファイルの中身は Read（メインエージェントは explore へ委譲）で読む。
    読み取り専用のため、計画の有無に関わらずいつでも呼んでよい。

    Args:
        query: 探したい内容（例: "溶接部の外観写真"）。
        collection: 検索するコレクション名。空なら全コレクションを横断して探す。
            名前は list_document_collections で確認できる。
        top_k: 返す最大件数。0 なら既定値（config.ini の [vector_db] top_k）。

    Returns:
        関連の高い順に「類似度・一致（意味 / キーワード）・コレクション・出典（@N とファイルパス）・
        取り込み元フォルダからの相対パス」を
        並べたテキスト（1ファイル1件）。全体の文字数は上限で切り詰める。該当が無い・機能が無効・
        エラーの場合はその旨のテキスト（エラーは「エラー: ...」形式）。
    """
    cfg = _enabled_config()
    if cfg is None:
        return _DISABLED_MESSAGE
    try:
        result = vector_search.search_paths(cfg, query, collection=collection.strip() or None, top_k=top_k or None)
    except (vector_search.SearchError, EmbeddingError, StoreError, CatalogError) as e:
        return f"エラー: {e}"
    logger.info("search_document_paths: query=%r collection=%r hits=%d", query, collection, len(result.hits))
    notes = [f"（{name} は検索対象外: {reason}）" for name, reason in result.skipped]
    if not result.hits:
        return "\n".join(
            [
                "名前の一致するファイルは見つかりませんでした。言い換えた query で探し直すか、"
                "search_documents で文書の中身から探してください。",
                *notes,
            ]
        )

    path_refs = _register_path_memory(sorted({h.source_path for h in result.hits}), description="search_document_paths の出典")
    ref_of = {path: ref for ref, path in path_refs.items()}
    budget = cfg.vector_db_max_result_chars
    lines = [f"{len(result.hits)}件見つかりました。"]
    for i, hit in enumerate(result.hits, start=1):
        ref = ref_of.get(hit.source_path)
        source = f"{ref} {hit.source_path}" if ref else hit.source_path
        matched = "・".join(_PATH_MATCHED_LABELS[m] for m in hit.matched)
        # キーワードだけの一致には類似度が無い（BM25 の値はコサイン類似度と比べられない）。
        score = f"類似度 {hit.score:.2f}" if "path" in hit.matched else "類似度 -"
        line = f"[{i}] {score}（一致: {matched}）/ {hit.collection} / {source}\n相対パス: {hit.text}"
        if len(line) > budget:
            lines.append(f"（残り {len(result.hits) - i + 1} 件は文字数の上限のため省略しました）")
            break
        lines.append(line)
        budget -= len(line)
    lines.extend(notes)
    return "\n\n".join(lines)
