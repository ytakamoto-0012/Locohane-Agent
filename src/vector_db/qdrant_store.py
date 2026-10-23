"""Qdrant（REST API を httpx で直接呼ぶ）によるベクターの保存先。

Qdrant 上のコレクション名は [vector_db].qdrant_collection_prefix + Locohane のコレクション名。
接頭辞の付いたコレクション以外には触れない（同じ Qdrant サーバーを他の用途と共有できる）。
コレクションの距離関数は [vector_db].distance（作るときに決まり、後から変えるには再構築が要る）。
"""

from __future__ import annotations

import uuid

import httpx

from .store import DISTANCE_METRICS, ChunkRecord, Hit, StoreError, distance_to_score

# ポイント ID（UUID）を source_path とチャンク番号から決めるための名前空間。
_POINT_NAMESPACE = uuid.UUID("6b7f2f0e-3c1d-4f5a-9a51-1f0c2d7e8b90")
_UPSERT_BATCH = 256


def _collection_distance(info: dict) -> str | None:
    vectors = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
    return vectors.get("distance") if isinstance(vectors, dict) else None


class QdrantStore:
    def __init__(
        self,
        url: str,
        *,
        prefix: str,
        api_key: str = "",
        timeout: float = 30,
        distance: str = "Cosine",
        transport: httpx.BaseTransport | None = None,
    ):
        if not url:
            raise StoreError("[vector_db].qdrant_url が設定されていません。")
        headers = {"api-key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)
        self._prefix = prefix
        self._distance = distance
        # 検索したコレクションの距離関数（作ったときの設定。今の [vector_db].distance と違うことがある）。
        self._distances: dict[str, str | None] = {}

    def close(self) -> None:
        self._client.close()

    def _name(self, name: str) -> str:
        return self._prefix + name

    def _request(self, method: str, path: str, *, allow_404: bool = False, **kwargs) -> dict | None:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TransportError as e:
            raise StoreError(f"Qdrant（{self._client.base_url}）に接続できません: {e}") from e
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise StoreError(f"Qdrant がエラーを返しました（{method} {path}、HTTP {response.status_code}）: {response.text[:300]}")
        return response.json()

    def ensure_collection(self, name: str, dim: int) -> None:
        qname = self._name(name)
        info = self._request("GET", f"/collections/{qname}", allow_404=True)
        if info is not None:
            vectors = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
            size = vectors.get("size") if isinstance(vectors, dict) else None
            if size is not None and size != dim:
                raise StoreError(f"Qdrant のコレクション {qname} のベクターの次元（{size}）と埋め込みの次元（{dim}）が違います。再構築してください。")
            distance = _collection_distance(info)
            if distance is not None and distance != self._distance:
                raise StoreError(
                    f"Qdrant のコレクション {qname} の距離関数（{distance}）と [vector_db].distance（{self._distance}）が違います。再構築してください。"
                )
            return
        self._request("PUT", f"/collections/{qname}", json={"vectors": {"size": dim, "distance": self._distance}})
        # source_path での削除（再取り込み・ファイル削除時）を速くするための索引。
        self._request("PUT", f"/collections/{qname}/index", params={"wait": "true"}, json={"field_name": "source_path", "field_schema": "keyword"})

    def drop_collection(self, name: str) -> None:
        self._distances.pop(name, None)
        self._request("DELETE", f"/collections/{self._name(name)}", allow_404=True)

    def delete_source(self, name: str, source_path: str) -> None:
        self._request(
            "POST",
            f"/collections/{self._name(name)}/points/delete",
            allow_404=True,
            params={"wait": "true"},
            json={"filter": {"must": [{"key": "source_path", "match": {"value": source_path}}]}},
        )

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        self.delete_source(name, source_path)
        points = [
            {
                "id": str(uuid.uuid5(_POINT_NAMESPACE, f"{source_path}#{i}")),
                "vector": vector,
                "payload": {"source_path": source_path, "locator": chunk.locator, "text": chunk.text},
            }
            for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]
        for start in range(0, len(points), _UPSERT_BATCH):
            self._request(
                "PUT",
                f"/collections/{self._name(name)}/points",
                params={"wait": "true"},
                json={"points": points[start : start + _UPSERT_BATCH]},
            )

    def _search_distance(self, name: str) -> str | None:
        """コレクション name の距離関数（無ければ None）。同じストアでは1回だけ問い合わせる。"""
        if name not in self._distances:
            info = self._request("GET", f"/collections/{self._name(name)}", allow_404=True)
            self._distances[name] = None if info is None else _collection_distance(info)
        return self._distances[name]

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        """Hit.score は、Cosine・Dot では Qdrant の値そのもの、Euclid・Manhattan では 1 / (1 + 距離)。"""
        distance = self._search_distance(name)
        if distance is None:
            return []
        body = self._request(
            "POST",
            f"/collections/{self._name(name)}/points/query",
            allow_404=True,
            json={"query": vector, "limit": int(top_k), "with_payload": True},
        )
        if body is None:
            return []
        hits = []
        for point in body.get("result", {}).get("points", []):
            payload = point.get("payload") or {}
            score = float(point.get("score", 0.0))
            if distance in DISTANCE_METRICS:
                score = distance_to_score(score)
            hits.append(
                Hit(
                    collection=name,
                    score=score,
                    source_path=str(payload.get("source_path", "")),
                    locator=str(payload.get("locator", "")),
                    text=str(payload.get("text", "")),
                )
            )
        return hits
