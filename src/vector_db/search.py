"""検索語に近いチャンクを取り込み済みのコレクションから探す。"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..config import Config
from .catalog import Collection, catalog_exists, open_catalog
from .embeddings import embed_texts, embedding_model
from .store import Hit, open_store


class SearchError(Exception):
    """検索できない（指定したコレクションが無い・取り込み前等）。"""


@dataclass
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    # 検索対象から外したコレクションとその理由（埋め込みモデル違い等）。
    skipped: list[tuple[str, str]] = field(default_factory=list)


def list_collections(cfg: Config) -> list[Collection]:
    """コレクション一覧（catalog.sqlite がまだ無ければ空）。"""
    if not catalog_exists(cfg.vector_db_dir):
        return []
    with open_catalog(cfg.vector_db_dir) as catalog:
        return catalog.list_collections()


def search(
    cfg: Config,
    query: str,
    *,
    collection: str | None = None,
    top_k: int | None = None,
    embedding_transport: httpx.BaseTransport | None = None,
    store_transport: httpx.BaseTransport | None = None,
) -> SearchResult:
    """query に意味の近いチャンクを、類似度の高い順に最大 top_k 件返す。

    collection を省略すると取り込み済みの全コレクションを横断して探す。
    今の埋め込みモデルと違うモデルで作られたコレクションは比べられないため外し、
    skipped に理由を載せる。類似度が [vector_db].min_score 未満のチャンクは返さない。

    Raises:
        SearchError: query が空、指定したコレクションが無い・まだ取り込まれていない、
            または検索できるコレクションが1つも無い場合。
        EmbeddingError / StoreError: 埋め込みサーバー・保存先を使えない場合。
    """
    if not query.strip():
        raise SearchError("検索語を指定してください。")
    limit = top_k if top_k and top_k > 0 else cfg.vector_db_top_k
    model = embedding_model(cfg)
    collections = list_collections(cfg)
    if collection:
        collections = [c for c in collections if c.name == collection]
        if not collections:
            raise SearchError(f"コレクション {collection} はありません。list_document_collections で一覧を確認してください。")
    result = SearchResult()
    targets = []
    for c in collections:
        if not c.model or not c.chunk_count:
            result.skipped.append((c.name, "まだ文書が取り込まれていません"))
        elif c.model != model:
            result.skipped.append((c.name, f"埋め込みモデル {c.model} で作られています（今の設定は {model}。管理者が再構築する必要があります）"))
        else:
            targets.append(c)
    if not targets:
        reasons = "、".join(f"{n}: {r}" for n, r in result.skipped) or "コレクションがありません"
        raise SearchError(f"検索できるコレクションがありません（{reasons}）。")

    vector = embed_texts(cfg, [query], kind="query", transport=embedding_transport)[0]
    store = open_store(cfg, transport=store_transport)
    try:
        hits: list[Hit] = []
        for c in targets:
            hits.extend(store.search(c.name, vector, limit))
    finally:
        store.close()
    hits = [h for h in hits if h.score >= cfg.vector_db_min_score]
    hits.sort(key=lambda h: h.score, reverse=True)
    result.hits = hits[:limit]
    return result
