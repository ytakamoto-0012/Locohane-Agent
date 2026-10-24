"""ベクターDB のテスト用の偽物（埋め込みサーバー・Qdrant サーバー）と設定の作り方。

pytest は各テストファイルのディレクトリを sys.path に足すため、
tests/ 配下のテストから `import _vector_db_fakes` で使える。
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import httpx

from src.config import LLMEndpoint, load_config

DIM = 32


def fake_vector(text: str) -> list[float]:
    """文字コードを DIM 個の箱に数えた正規化ベクター（同じ文字を多く含むほど近くなる）。"""
    v = [0.0] * DIM
    for ch in text:
        if not ch.isspace():
            v[ord(ch) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeEmbeddingServer:
    """/v1/embeddings の偽物。受け取った入力を requests に記録する。"""

    def __init__(self, *, fail_hosts: tuple[str, ...] = (), two_dimensional: bool = False):
        self.requests: list[dict] = []
        self.fail_hosts = fail_hosts
        self.two_dimensional = two_dimensional

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.fail_hosts:
            raise httpx.ConnectError("refused", request=request)
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), **body})
        data = []
        for i, text in enumerate(body["input"]):
            vec = fake_vector(text)
            data.append({"index": i, "embedding": [vec, vec] if self.two_dimensional else vec})
        return httpx.Response(200, json={"data": data, "model": body["model"]})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeQdrant:
    """Qdrant REST API のうち QdrantStore が使う操作だけを再現する偽物。"""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if parts[0] != "collections" or len(parts) < 2:
            return httpx.Response(404, json={"status": {"error": "not found"}})
        name = parts[1]
        rest = parts[2:]
        coll = self.collections.get(name)
        if not rest:
            if request.method == "GET":
                if coll is None:
                    return httpx.Response(404, json={"status": {"error": "Not found"}})
                return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"size": coll["size"], "distance": "Cosine"}}}}})
            if request.method == "PUT":
                self.collections[name] = {"size": body["vectors"]["size"], "points": {}}
                return httpx.Response(200, json={"result": True})
            if request.method == "DELETE":
                self.collections.pop(name, None)
                return httpx.Response(200, json={"result": coll is not None})
        if coll is None:
            return httpx.Response(404, json={"status": {"error": "Not found"}})
        if rest == ["index"]:
            return httpx.Response(200, json={"result": {}})
        if rest == ["points"] and request.method == "PUT":
            for p in body["points"]:
                assert len(p["vector"]) == coll["size"]
                coll["points"][p["id"]] = p
            return httpx.Response(200, json={"result": {}})
        if rest == ["points", "delete"]:
            value = body["filter"]["must"][0]["match"]["value"]
            coll["points"] = {k: p for k, p in coll["points"].items() if p["payload"]["source_path"] != value}
            return httpx.Response(200, json={"result": {}})
        if rest == ["points", "query"]:
            q = body["query"]
            scored = [
                {"id": k, "score": sum(a * b for a, b in zip(q, p["vector"])), "payload": p["payload"]}
                for k, p in coll["points"].items()
            ]
            scored.sort(key=lambda s: s["score"], reverse=True)
            return httpx.Response(200, json={"result": {"points": scored[: body["limit"]]}})
        return httpx.Response(400, json={"status": {"error": f"unexpected {request.method} {path}"}})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_config(tmp_path: Path, **changes):
    """[vector_db] を有効にした設定（データは tmp_path 配下）。"""
    cfg = load_config()
    defaults = {
        "vector_db_enabled": True,
        "vector_db_dir": tmp_path / "vector_db",
        "vector_db_embedding_endpoints": (LLMEndpoint(base_url="http://embed/v1", api_key="k", model="fake-embed"),),
        "vector_db_embedding_batch_size": 16,
        "vector_db_min_score": 0.0,
        "vector_db_chunk_size": 200,
        "vector_db_chunk_overlap": 20,
        "vector_db_qdrant_url": "http://qdrant:6333",
        "vector_db_qdrant_collection_prefix": "locohane_test_",
    }
    defaults.update(changes)
    return dataclasses.replace(cfg, **defaults)
