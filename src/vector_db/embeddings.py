"""埋め込み（embedding）を推論サーバーの /v1/embeddings で計算する。"""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from ..config import Config, LLMEndpoint

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """埋め込みを計算できなかった（接続できない・応答が不正等）。"""


def embedding_model(cfg: Config) -> str:
    """設定中の埋め込みモデル名（コレクションに記録し、検索時の一致確認に使う）。

    Raises:
        EmbeddingError: [vector_db].embedding_url が未設定の場合。
    """
    if not cfg.vector_db_embedding_endpoints:
        raise EmbeddingError("[vector_db].embedding_url が設定されていません。")
    return cfg.vector_db_embedding_endpoints[0].model


def embed_texts(
    cfg: Config,
    texts: list[str],
    *,
    kind: Literal["document", "query"],
    transport: httpx.BaseTransport | None = None,
) -> list[list[float]]:
    """texts の埋め込みベクターを入力と同じ順で返す。

    [vector_db].embedding_batch_size 件ずつまとめて送り、kind に応じて
    query_prefix / document_prefix を先頭に付ける。接続先が複数あれば、
    接続できなかった場合だけ次の接続先へ切り替える。

    Args:
        cfg: 設定。
        texts: 埋め込む文字列。
        kind: "document"（取り込み時）または "query"（検索時）。
        transport: テスト用に差し替える httpx のトランスポート。

    Raises:
        EmbeddingError: どの接続先からも埋め込みを得られなかった場合。
    """
    endpoints = cfg.vector_db_embedding_endpoints
    if not endpoints:
        raise EmbeddingError("[vector_db].embedding_url が設定されていません。")
    prefix = cfg.vector_db_query_prefix if kind == "query" else cfg.vector_db_document_prefix
    inputs = [prefix + t for t in texts]
    vectors: list[list[float]] = []
    batch_size = cfg.vector_db_embedding_batch_size
    with httpx.Client(timeout=httpx.Timeout(cfg.vector_db_embedding_timeout_seconds, connect=10.0), transport=transport) as client:
        for start in range(0, len(inputs), batch_size):
            vectors.extend(_embed_batch(client, endpoints, inputs[start : start + batch_size]))
    return vectors


def _embed_batch(client: httpx.Client, endpoints: tuple[LLMEndpoint, ...], batch: list[str]) -> list[list[float]]:
    last_error: Exception | None = None
    for endpoint in endpoints:
        url = endpoint.base_url.rstrip("/") + "/embeddings"
        try:
            response = client.post(
                url,
                json={"model": endpoint.model, "input": batch},
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
            )
        except httpx.TransportError as e:
            logger.warning("embeddings: %s に接続できません: %s", url, e)
            last_error = e
            continue
        if response.status_code != 200:
            raise EmbeddingError(f"{url} がエラーを返しました（HTTP {response.status_code}）: {response.text[:300]}")
        return _parse_response(url, response.json(), len(batch))
    raise EmbeddingError(f"埋め込みの接続先に接続できません: {last_error}")


def _parse_response(url: str, body: dict, expected: int) -> list[list[float]]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError(f"{url} の応答の data が入力件数（{expected}）と一致しません。")
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    vectors = []
    for item in ordered:
        embedding = item.get("embedding")
        # llama-server は --pooling none のとき トークンごとのベクター（2次元）を返す。検索には使えない。
        if not isinstance(embedding, list) or not embedding or isinstance(embedding[0], list):
            raise EmbeddingError(f"{url} の応答が1次元のベクターではありません（llama-server は --pooling を none 以外にして起動してください）。")
        vectors.append([float(x) for x in embedding])
    return vectors
