"""埋め込み（embedding）を推論サーバーの /v1/embeddings で計算する。"""

from __future__ import annotations

from array import array
import base64
from contextlib import contextmanager
import logging
import random
import sys
import threading
import time
from typing import Callable, Literal

import httpx

from ..config import Config, LLMEndpoint

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """埋め込みを計算できなかった（接続できない・応答が不正等）。"""


class EmbeddingStopped(EmbeddingError):
    """再試行を待っている間に should_stop() が True を返した（取り込みの「停止」）。"""


# 接続先の選び方（[vector_db].embedding_routing_strategy）のための状態。管理ツールのプロセスでは
# 複数インスタンスの設定が混在するため、接続先の base_url（の並び）をキーにする。
_routing_lock = threading.Lock()
_round_robin_counters: dict[tuple[str, ...], int] = {}
# priority_failover で、エラーが返った接続先を後回しにする期限（base_url → time.monotonic()）。
_failed_until: dict[str, float] = {}
_FAILOVER_COOLDOWN_SECONDS = 60.0
# encoding_format = base64 に対応していない（400・422 を返した）接続先の base_url。以後は float で受け取る。
_float_only: set[str] = set()
# 再試行を待つ間に should_stop() を確かめる間隔（秒）。
_STOP_POLL_SECONDS = 0.5
# 再試行の待ちに使う sleep（テストで差し替える）。
_sleep = time.sleep


def embedding_model(cfg: Config) -> str:
    """設定中の埋め込みモデル名（コレクションに記録し、検索時の一致確認に使う）。

    Raises:
        EmbeddingError: [vector_db].embedding_url が未設定の場合。
    """
    if not cfg.vector_db_embedding_endpoints:
        raise EmbeddingError("[vector_db].embedding_url is not set.")
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
    should_stop: Callable[[], bool] | None = None,
) -> list[list[float]]:
    """texts の埋め込みベクターを入力と同じ順で返す。

    [vector_db].embedding_batch_size 件ずつまとめて送り、kind に応じて
    query_prefix / document_prefix を先頭に付ける。接続先の選び方とエラー時の
    切り替え・再試行は _embed_batch 参照（再試行の回数は kind="document" なら
    embedding_max_retries、kind="query" なら search_embedding_max_retries）。

    Args:
        cfg: 設定。
        texts: 埋め込む文字列。
        kind: "document"（取り込み時）または "query"（検索時）。
        transport: テスト用に差し替える httpx のトランスポート（client を渡したときは使わない）。
        client: open_client() で作ったクライアント。省略時はこの呼び出しの間だけのものを作る。
        should_stop: 再試行を待つ間に確かめ、True を返したら EmbeddingStopped を投げてやめる。

    Raises:
        EmbeddingError: どの接続先からも埋め込みを得られなかった場合
            （EmbeddingStopped は再試行の待ち中に止められた場合）。
    """
    endpoints = cfg.vector_db_embedding_endpoints
    if not endpoints:
        raise EmbeddingError("[vector_db].embedding_url is not set.")
    prefix = cfg.vector_db_query_prefix if kind == "query" else cfg.vector_db_document_prefix
    inputs = [prefix + t for t in texts]
    vectors: list[list[float]] = []
    batch_size = cfg.vector_db_embedding_batch_size
    # 検索は止める手段が無いため、無制限（embedding_max_retries = 0）にはしない。
    max_retries = cfg.vector_db_search_embedding_max_retries if kind == "query" else (cfg.vector_db_embedding_max_retries or None)
    with _client_scope(cfg, client, transport) as http:
        for start in range(0, len(inputs), batch_size):
            vectors.extend(_embed_batch(cfg, http, endpoints, inputs[start : start + batch_size], max_retries, should_stop))
    return vectors


def embed_images(
    cfg: Config,
    pngs: list[bytes],
    *,
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[list[float]]:
    """PNG 画像の埋め込みベクターを入力と同じ順で返す（[vector_db].image_input = true の取り込み用）。

    llama-server の /v1/embeddings の画像入力（--mmproj 付きで起動したマルチモーダルの
    埋め込みモデル）の形式 {"content": [{"type": "image_url", ...}]} で送る。
    画像はデータが大きく、llama-server では同じリクエスト内の画像入力の結果が
    揺れる不具合も報告されているため、embedding_batch_size によらず1枚ずつ送る。
    document_prefix はテキスト用のため付けない。transport・client・should_stop は embed_texts と同じ。

    Raises:
        EmbeddingError: どの接続先からも埋め込みを得られなかった場合
            （埋め込みモデルが画像入力に対応していない場合を含む）。
    """
    endpoints = cfg.vector_db_embedding_endpoints
    if not endpoints:
        raise EmbeddingError("[vector_db].embedding_url is not set.")
    vectors: list[list[float]] = []
    with _client_scope(cfg, client, transport) as http:
        for png in pngs:
            url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            batch = [{"content": [{"type": "image_url", "image_url": {"url": url}}]}]
            vectors.extend(_embed_batch(cfg, http, endpoints, batch, cfg.vector_db_embedding_max_retries or None, should_stop))
    return vectors


def _endpoint_order(cfg: Config, endpoints: tuple[LLMEndpoint, ...]) -> list[LLMEndpoint]:
    """[vector_db].embedding_routing_strategy に従い、1回のリクエストで試す接続先の順を返す。

    - round_robin: リクエストごとに先頭を1つずつ回す（並列の取り込みで負荷を分ける）。
    - random: リクエストごとに無作為に並べる。
    - priority_failover: 書いた順。直近 _FAILOVER_COOLDOWN_SECONDS 秒以内にエラーが返った接続先は後回しにする。

    どの戦略でも全接続先を並べ、先頭でエラーが返れば次を試す（_embed_batch）。
    """
    if len(endpoints) <= 1:
        return list(endpoints)
    strategy = cfg.vector_db_embedding_routing_strategy
    if strategy == "round_robin":
        key = tuple(e.base_url for e in endpoints)
        with _routing_lock:
            counter = _round_robin_counters.get(key, 0)
            _round_robin_counters[key] = counter + 1
        offset = counter % len(endpoints)
        return list(endpoints[offset:] + endpoints[:offset])
    if strategy == "random":
        return random.sample(endpoints, len(endpoints))
    now = time.monotonic()
    with _routing_lock:
        cooling = {e.base_url for e in endpoints if _failed_until.get(e.base_url, 0.0) > now}
    return [e for e in endpoints if e.base_url not in cooling] + [e for e in endpoints if e.base_url in cooling]


def _mark_failed(endpoint: LLMEndpoint) -> None:
    """endpoint を priority_failover で一時的に後回しにする（他の戦略では参照しない）。"""
    with _routing_lock:
        _failed_until[endpoint.base_url] = time.monotonic() + _FAILOVER_COOLDOWN_SECONDS


def _retryable_status(status: int) -> bool:
    """時間を置いて試し直す意味のある HTTP ステータスか（4xx は 408・429 以外は入力・設定の誤りとみなす）。"""
    return not (400 <= status < 500) or status in (408, 429)


def _wait_retry(seconds: float, should_stop: Callable[[], bool] | None) -> None:
    """seconds 秒待つ。待つ間に should_stop() が True を返したら EmbeddingStopped を投げる。"""
    remaining = seconds
    while True:
        if should_stop is not None and should_stop():
            raise EmbeddingStopped("Stopped retrying the embeddings because a stop was requested.")
        if remaining <= 0:
            return
        step = min(remaining, _STOP_POLL_SECONDS)
        _sleep(step)
        remaining -= step


def _embed_batch(
    cfg: Config,
    client: httpx.Client,
    endpoints: tuple[LLMEndpoint, ...],
    batch: list[str | dict],
    max_retries: int | None,
    should_stop: Callable[[], bool] | None,
) -> list[list[float]]:
    """batch を埋め込む。エラーが返れば次の接続先へ切り替え、全接続先でエラーなら時間を置いて試し直す。

    接続できない・HTTP 200 以外が返ったら、_endpoint_order() の順で次の接続先を試す。全接続先で
    エラーになったら [vector_db].embedding_retry_interval_seconds 秒待って全接続先を試し直し、これを
    max_retries 回まで（None なら成功するまで無制限に、0 なら試し直さずに）繰り返す。ただし、すべてのエラーが
    408・429 以外の 4xx（入力・設定の誤りで、待っても直らない）なら試し直さない。
    応答の形が不正な場合（_parse_response）は設定の誤りのため、切り替えずにすぐ投げる。
    """
    retries = 0
    while True:
        errors: list[str] = []
        retryable = False
        for endpoint in _endpoint_order(cfg, endpoints):
            url = endpoint.base_url.rstrip("/") + "/embeddings"
            try:
                response = _post(cfg, client, endpoint, url, batch)
            except httpx.TransportError as e:
                logger.warning("embeddings: cannot connect to %s: %s", url, e)
                errors.append(f"Cannot connect to {url} ({e})")
                retryable = True
                _mark_failed(endpoint)
                continue
            if response.status_code != 200:
                logger.warning("embeddings: %s returned an error (HTTP %d): %s", url, response.status_code, response.text[:300])
                errors.append(f"{url} returned an error (HTTP {response.status_code}): {response.text[:300]}")
                if _retryable_status(response.status_code):
                    retryable = True
                    _mark_failed(endpoint)
                continue
            return _parse_response(url, response.json(), len(batch))
        detail = " / ".join(errors)
        if not retryable or (max_retries is not None and retries >= max_retries):
            suffix = f" (retried {retries} times)" if retries else ""
            raise EmbeddingError(f"Could not compute the embeddings{suffix}: {detail}")
        retries += 1
        interval = cfg.vector_db_embedding_retry_interval_seconds
        logger.warning(
            "embeddings: every endpoint returned an error. Retrying in %g seconds (%d/%s): %s",
            interval,
            retries,
            "無制限" if max_retries is None else max_retries,
            detail,
        )
        _wait_retry(interval, should_stop)


def _post(cfg: Config, client: httpx.Client, endpoint: LLMEndpoint, url: str, batch: list[str | dict]) -> httpx.Response:
    """endpoint へ batch を送る。

    [vector_db].embedding_encoding_format = base64 なら encoding_format を付けて送り、接続先が 400・422 を返したら
    （encoding_format に対応していない）、その接続先を以後 float で受け取ることにして同じリクエストを送り直す。
    """
    headers = {"Authorization": f"Bearer {endpoint.api_key}"}
    body: dict = {"model": endpoint.model, "input": batch}
    if cfg.vector_db_embedding_encoding_format != "base64" or endpoint.base_url in _float_only:
        return client.post(url, json=body, headers=headers)
    response = client.post(url, json={**body, "encoding_format": "base64"}, headers=headers)
    if response.status_code in (400, 422):
        logger.warning(
            "embeddings: %s did not accept encoding_format=base64 (HTTP %d). Using float for this endpoint from now on: %s",
            url,
            response.status_code,
            response.text[:300],
        )
        with _routing_lock:
            _float_only.add(endpoint.base_url)
        response = client.post(url, json=body, headers=headers)
    return response


def _decode_base64_vector(text: str) -> list[float]:
    """encoding_format = base64 の埋め込み（リトルエンディアンの float32 の並び）をベクターにする。"""
    raw = base64.b64decode(text)
    if not raw or len(raw) % 4:
        raise ValueError("The base64 length is not a sequence of float32")
    values = array("f")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tolist()


def _parse_response(url: str, body: dict, expected: int) -> list[list[float]]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError(f"data in the response from {url} does not match the number of inputs ({expected}).")
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    vectors = []
    for item in ordered:
        embedding = item.get("embedding")
        if isinstance(embedding, str):
            # encoding_format = base64 の応答（対応していない接続先は無視して数値の配列で返すため、どちらも受け付ける）。
            try:
                vectors.append(_decode_base64_vector(embedding))
            except ValueError as e:
                raise EmbeddingError(f"Cannot read the base64 embeddings in the response from {url}: {e}") from e
            continue
        # llama-server は --pooling none のとき トークンごとのベクター（2次元）を返す。検索には使えない。
        if not isinstance(embedding, list) or not embedding or isinstance(embedding[0], (list, str)):
            raise EmbeddingError(f"The response from {url} is not a one-dimensional vector (start llama-server with --pooling other than none).")
        vectors.append(list(map(float, embedding)))
    return vectors
