"""Qdrant（REST API を httpx で直接呼ぶ）によるベクターの保存先。

Qdrant 上のコレクション名は [vector_db].qdrant_collection_prefix + Locohane のコレクション名。
接頭辞の付いたコレクション以外には触れない（同じ Qdrant サーバーを他の用途と共有できる）。
コレクションの距離関数は [vector_db].distance（作るときに決まり、後から変えるには再構築が要る）。
索引・量子化・ディスク配置は [vector_db].qdrant_preset（QDRANT_PRESETS。既存のコレクションにも
apply_preset() で反映でき、埋め込み直しは要らない）。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

import httpx

from .store import DISTANCE_METRICS, ChunkRecord, Hit, StoreError, distance_to_score

# ポイント ID（UUID）を source_path とチャンク番号から決めるための名前空間。
_POINT_NAMESPACE = uuid.UUID("6b7f2f0e-3c1d-4f5a-9a51-1f0c2d7e8b90")
_UPSERT_BATCH = 256


@dataclass(frozen=True)
class QdrantPreset:
    """[vector_db].qdrant_preset の1つ分（想定するチャンク件数の規模ごとの Qdrant の設定）。

    どのプリセットも、ベクターを int8 にスカラー量子化した複製を RAM に置いて検索し
    （上位の候補は元のベクターで再採点する）、チャンク本文等の payload はディスクに置く。

    m / ef_construct: HNSW 索引のつながりの数・作るときの探索幅（大きいほど件数が多くても
        取りこぼしにくいが、索引の作成が遅くメモリーも増える）。
    hnsw_ef: 検索時の探索幅（大きいほど正確で遅い）。
    vectors_on_disk: 元の（float32 の）ベクターをディスク（mmap）に置くか。量子化した複製は RAM に置く。
    segments: コレクションを分けるセグメント数の目安（並列に検索する単位。CPU のコア数程度）。
    oversampling: 量子化した複製で top_k の何倍を候補にして、元のベクターで再採点するか。
    """

    m: int
    ef_construct: int
    hnsw_ef: int
    vectors_on_disk: bool
    segments: int
    oversampling: float


# config.ini の qdrant_preset の説明・README_DETAIL.md「Qdrant を使う」の表と一致させる。
# キーは src/config.py の VECTOR_DB_QDRANT_PRESETS（none 以外）。
QDRANT_PRESETS: dict[str, QdrantPreset] = {
    "1m": QdrantPreset(m=16, ef_construct=128, hnsw_ef=128, vectors_on_disk=False, segments=4, oversampling=1.5),
    "2m": QdrantPreset(m=16, ef_construct=200, hnsw_ef=160, vectors_on_disk=True, segments=6, oversampling=2.0),
    "3m": QdrantPreset(m=24, ef_construct=256, hnsw_ef=200, vectors_on_disk=True, segments=8, oversampling=2.0),
    "4m": QdrantPreset(m=32, ef_construct=256, hnsw_ef=256, vectors_on_disk=True, segments=8, oversampling=2.5),
}
_SCALAR_QUANTIZATION = {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}}


def _preset_config(preset: QdrantPreset) -> dict:
    """コレクションの作成・更新（PATCH）で共通に送る、ベクター以外の設定。"""
    return {
        "hnsw_config": {"m": preset.m, "ef_construct": preset.ef_construct, "on_disk": False},
        "optimizers_config": {"default_segment_number": preset.segments},
        "quantization_config": _SCALAR_QUANTIZATION,
    }


def _preset_matches(info: dict, preset: QdrantPreset) -> bool:
    """GET /collections/{name} の設定が preset と同じか（違えば PATCH で更新する）。"""
    config = info.get("result", {}).get("config", {})
    params = config.get("params", {})
    vectors = params.get("vectors", {})
    hnsw = config.get("hnsw_config") or {}
    optimizer = config.get("optimizer_config") or {}
    return (
        isinstance(vectors, dict)
        and bool(vectors.get("on_disk", False)) == preset.vectors_on_disk
        and hnsw.get("m") == preset.m
        and hnsw.get("ef_construct") == preset.ef_construct
        and optimizer.get("default_segment_number") == preset.segments
        and config.get("quantization_config") == _SCALAR_QUANTIZATION
        and bool(params.get("on_disk_payload", False))
    )


# shared_client() のクライアント（接続先・API キー・タイムアウトごと）。プロセスが終わるまで閉じない。
_shared_clients: dict[tuple[str, str, float], httpx.Client] = {}
_shared_lock = threading.Lock()


def _new_client(url: str, api_key: str, timeout: float, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    headers = {"api-key": api_key} if api_key else {}
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=timeout, transport=transport)


def shared_client(url: str, api_key: str, timeout: float) -> httpx.Client:
    """プロセス内で使い回す Qdrant 用のクライアント（検索のように何度も開く用。close しない）。

    検索のたびに作り直すと、Windows で接続先が localhost だと毎回 IPv6（::1）を試してから
    IPv4 へ戻るため約 2 秒かかる（embeddings.open_client と同じ）。
    """
    key = (url.rstrip("/"), api_key, timeout)
    with _shared_lock:
        client = _shared_clients.get(key)
        if client is None or client.is_closed:
            client = _shared_clients[key] = _new_client(url, api_key, timeout)
        return client


def _delete_operation(source_path: str) -> dict:
    return {"delete": {"filter": {"must": [{"key": "source_path", "match": {"value": source_path}}]}}}


def _point_count(operation: dict) -> int:
    return len(operation["upsert"]["points"]) if "upsert" in operation else 0


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
        preset: str = "none",
        transport: httpx.BaseTransport | None = None,
        shared: bool = False,
    ):
        """shared=True なら shared_client() を使い、close() でも閉じない（transport は使わない）。

        preset は [vector_db].qdrant_preset（QDRANT_PRESETS のキー。"none" なら Qdrant の既定のまま）。
        """
        if not url:
            raise StoreError("[vector_db].qdrant_url is not set.")
        self._owns_client = not shared
        self._client = shared_client(url, api_key, timeout) if shared else _new_client(url, api_key, timeout, transport)
        self._prefix = prefix
        self._distance = distance
        self._preset = QDRANT_PRESETS.get(preset)
        # 検索したコレクションの距離関数（作ったときの設定。今の [vector_db].distance と違うことがある）。
        self._distances: dict[str, str | None] = {}
        # begin_batch() から commit() までにためた更新（Qdrant 上のコレクション名 → points/batch の operations）。
        self._batching = False
        self._pending: dict[str, list[dict]] = {}
        self._pending_points = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _name(self, name: str) -> str:
        return self._prefix + name

    def _request(self, method: str, path: str, *, allow_404: bool = False, **kwargs) -> dict | None:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TransportError as e:
            raise StoreError(f"Cannot connect to Qdrant ({self._client.base_url}): {e}") from e
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise StoreError(f"Qdrant returned an error ({method} {path}, HTTP {response.status_code}): {response.text[:300]}")
        return response.json()

    def ensure_collection(self, name: str, dim: int) -> None:
        qname = self._name(name)
        info = self._request("GET", f"/collections/{qname}", allow_404=True)
        if info is not None:
            vectors = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
            size = vectors.get("size") if isinstance(vectors, dict) else None
            if size is not None and size != dim:
                raise StoreError(f"The vector dimension ({size}) of Qdrant collection {qname} differs from the embedding dimension ({dim}). Rebuild it.")
            distance = _collection_distance(info)
            if distance is not None and distance != self._distance:
                raise StoreError(
                    f"The distance ({distance}) of Qdrant collection {qname} differs from [vector_db].distance ({self._distance}). Rebuild it."
                )
            self._apply_preset(qname, info)
            return
        body: dict = {"vectors": {"size": dim, "distance": self._distance}}
        if self._preset is not None:
            body["vectors"]["on_disk"] = self._preset.vectors_on_disk
            body.update(_preset_config(self._preset), on_disk_payload=True)
        self._request("PUT", f"/collections/{qname}", json=body)
        # source_path での削除（再取り込み・ファイル削除時）を速くするための索引。
        self._request("PUT", f"/collections/{qname}/index", params={"wait": "true"}, json={"field_name": "source_path", "field_schema": "keyword"})

    def apply_preset(self, name: str) -> None:
        """既存のコレクション name の設定を [vector_db].qdrant_preset に合わせる（無い・preset が none なら何もしない）。

        索引・量子化は Qdrant が裏で作り直す（埋め込み直しは要らない。作り直す間も検索はできる）。
        """
        if self._preset is None:
            return
        qname = self._name(name)
        info = self._request("GET", f"/collections/{qname}", allow_404=True)
        if info is not None:
            self._apply_preset(qname, info)

    def _apply_preset(self, qname: str, info: dict) -> None:
        if self._preset is None or _preset_matches(info, self._preset):
            return
        # 名前の無いベクター（このストアは名前を付けない）の設定は "" の名前で更新する。
        body = {"vectors": {"": {"on_disk": self._preset.vectors_on_disk}}, **_preset_config(self._preset), "params": {"on_disk_payload": True}}
        self._request("PATCH", f"/collections/{qname}", json=body)

    def drop_collection(self, name: str) -> None:
        self._distances.pop(name, None)
        self._pending.pop(self._name(name), None)
        self._request("DELETE", f"/collections/{self._name(name)}", allow_404=True)

    # --- 更新（削除・追加）は POST /collections/{name}/points/batch でまとめて送る ---

    def begin_batch(self) -> None:
        """以後の delete_source・replace_source を commit() までためて、まとめて送る（取り込み用）。

        ためたポイントが _UPSERT_BATCH 件を超えたら commit() を待たずに送る。同じコレクションへの更新は
        ためた順に送るため、削除と追加の順序は変わらない。
        """
        self._batching = True

    def commit(self) -> None:
        """ためた更新を送り、反映されるまで待つ（wait=true）。"""
        pending, self._pending, self._pending_points = self._pending, {}, 0
        for qname, operations in pending.items():
            self._send_operations(qname, operations)

    def rollback(self) -> None:
        """まだ送っていない更新を捨てる（既に送った分は戻らない）。"""
        self._pending, self._pending_points = {}, 0

    def end_batch(self) -> None:
        self.commit()
        self._batching = False

    def _queue(self, name: str, operations: list[dict]) -> None:
        qname = self._name(name)
        if not self._batching:
            self._send_operations(qname, operations)
            return
        self._pending.setdefault(qname, []).extend(operations)
        self._pending_points += sum(_point_count(op) for op in operations)
        if self._pending_points >= _UPSERT_BATCH:
            self.commit()

    def _send_operations(self, qname: str, operations: list[dict]) -> None:
        """operations を、1リクエストあたりのポイントが _UPSERT_BATCH 件程度になるよう分けて順に送る。

        削除だけのリクエストはコレクションが無ければ何もしない（404 を無視する）。
        """
        group: list[dict] = []
        count = 0
        for op in operations:
            n = _point_count(op)
            if group and count + n > _UPSERT_BATCH:
                self._send_batch(qname, group)
                group, count = [], 0
            group.append(op)
            count += n
        if group:
            self._send_batch(qname, group)

    def _send_batch(self, qname: str, operations: list[dict]) -> None:
        only_deletes = all("delete" in op for op in operations)
        self._request(
            "POST", f"/collections/{qname}/points/batch", allow_404=only_deletes, params={"wait": "true"}, json={"operations": operations}
        )

    def delete_source(self, name: str, source_path: str) -> None:
        self._queue(name, [_delete_operation(source_path)])

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        points = [
            {
                "id": str(uuid.uuid5(_POINT_NAMESPACE, f"{source_path}#{i}")),
                "vector": vector,
                "payload": {"source_path": source_path, "locator": chunk.locator, "text": chunk.text},
            }
            for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]
        operations = [_delete_operation(source_path)]
        operations += [{"upsert": {"points": points[i : i + _UPSERT_BATCH]}} for i in range(0, len(points), _UPSERT_BATCH)]
        self._queue(name, operations)

    def _search_distance(self, name: str) -> str | None:
        """コレクション name の距離関数（無ければ None）。同じストアでは1回だけ問い合わせる。"""
        if name not in self._distances:
            info = self._request("GET", f"/collections/{self._name(name)}", allow_404=True)
            self._distances[name] = None if info is None else _collection_distance(info)
        return self._distances[name]

    def _query_body(self, vector: list[float], top_k: int) -> dict:
        body: dict = {"query": vector, "limit": int(top_k), "with_payload": True}
        if self._preset is not None:
            # 量子化していないコレクション（まだ apply_preset していないもの）では quantization は無視される。
            body["params"] = {"hnsw_ef": self._preset.hnsw_ef, "quantization": {"rescore": True, "oversampling": self._preset.oversampling}}
        return body

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        """Hit.score は、Cosine・Dot では Qdrant の値そのもの、Euclid・Manhattan では 1 / (1 + 距離)。"""
        distance = self._search_distance(name)
        if distance is None:
            return []
        body = self._request(
            "POST",
            f"/collections/{self._name(name)}/points/query",
            allow_404=True,
            json=self._query_body(vector, top_k),
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
