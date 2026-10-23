"""埋め込み（embedding）を推論サーバーの /v1/embeddings で計算する。"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import logging
import threading
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


def open_client(cfg: Config, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """埋め込みサーバー用の httpx クライアント（呼び出し側で close する）。

    取り込みのように何度も埋め込むときは、これを1つ作って embed_texts・embed_images の client に渡し、
    接続を使い回す。Windows では接続先が localhost だと新しく接続するたびに IPv6（::1）を試してから
    IPv4 へ戻るため約 2 秒かかり、ファイルごとにクライアントを作り直すとその分だけ遅くなる。
    httpx.Client はスレッド間で共有できる。
    """
    return httpx.Client(timeout=httpx.Timeout(cfg.vector_db_embedding_timeout_seconds, connect=10.0), transport=transport)


# shared_client() のクライアント（タイムアウト秒ごと）。プロセスが終わるまで閉じない。
_shared_clients: dict[float, httpx.Client] = {}
_shared_lock = threading.Lock()


def shared_client(cfg: Config) -> httpx.Client:
    """プロセス内で使い回す埋め込みサーバー用のクライアント（検索のように1件ずつ何度も埋め込む用。close しない）。

    検索のたびに open_client() で作り直すと、localhost では毎回約 2 秒かかる（open_client 参照）。
    """
    timeout = cfg.vector_db_embedding_timeout_seconds
    with _shared_lock:
        client = _shared_clients.get(timeout)
        if client is None or client.is_closed:
            client = _shared_clients[timeout] = open_client(cfg)
        return client


@contextmanager
def _client_scope(cfg: Config, client: httpx.Client | None, transport: httpx.BaseTransport | None):
    """client が渡されていればそれを（閉じずに）、無ければこの呼び出しの間だけのクライアントを使う。"""
    if client is not None:
        yield client
        return
    with open_client(cfg, transport=transport) as own:
        yield own


def embed_texts(
    cfg: Config,
    texts: list[str],
    *,
    kind: Literal["document", "query"],
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
) -> list[list[float]]:
    """texts の埋め込みベクターを入力と同じ順で返す。

    [vector_db].embedding_batch_size 件ずつまとめて送り、kind に応じて
    query_prefix / document_prefix を先頭に付ける。接続先が複数あれば、
    接続できなかった場合だけ次の接続先へ切り替える。

    Args:
        cfg: 設定。
        texts: 埋め込む文字列。
        kind: "document"（取り込み時）または "query"（検索時）。
        transport: テスト用に差し替える httpx のトランスポート（client を渡したときは使わない）。
        client: open_client() で作ったクライアント。省略時はこの呼び出しの間だけのものを作る。

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
    with _client_scope(cfg, client, transport) as http:
        for start in range(0, len(inputs), batch_size):
            vectors.extend(_embed_batch(http, endpoints, inputs[start : start + batch_size]))
    return vectors


def embed_images(
    cfg: Config,
    pngs: list[bytes],
    *,
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
) -> list[list[float]]:
    """PNG 画像の埋め込みベクターを入力と同じ順で返す（[vector_db].image_input = true の取り込み用）。

    llama-server の /v1/embeddings の画像入力（--mmproj 付きで起動したマルチモーダルの
    埋め込みモデル）の形式 {"content": [{"type": "image_url", ...}]} で送る。
    画像はデータが大きく、llama-server では同じリクエスト内の画像入力の結果が
    揺れる不具合も報告されているため、embedding_batch_size によらず1枚ずつ送る。
    document_prefix はテキスト用のため付けない。transport・client は embed_texts と同じ。

    Raises:
        EmbeddingError: どの接続先からも埋め込みを得られなかった場合
            （埋め込みモデルが画像入力に対応していない場合を含む）。
    """
    endpoints = cfg.vector_db_embedding_endpoints
    if not endpoints:
        raise EmbeddingError("[vector_db].embedding_url が設定されていません。")
    vectors: list[list[float]] = []
    with _client_scope(cfg, client, transport) as http:
        for png in pngs:
            url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            vectors.extend(_embed_batch(http, endpoints, [{"content": [{"type": "image_url", "image_url": {"url": url}}]}]))
    return vectors


def _embed_batch(client: httpx.Client, endpoints: tuple[LLMEndpoint, ...], batch: list[str | dict]) -> list[list[float]]:
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
