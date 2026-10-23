"""ベクターの保存先（バックエンド）の共通インターフェースと生成。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Config


class StoreError(Exception):
    """ベクターの保存先を操作できなかった（接続できない・次元が合わない等）。"""


@dataclass(frozen=True)
class ChunkRecord:
    """保存する1チャンク。locator はファイル内の位置（extract.Segment.locator）。

    image_path はページを画像として埋め込んだ（[vector_db].image_input）チャンクの
    ページ画像のパス。テキストとして埋め込んだチャンクは空文字。
    """

    locator: str
    text: str
    image_path: str = ""


@dataclass(frozen=True)
class Hit:
    """検索で見つかった1チャンク。score はコサイン類似度（1.0 が完全一致）。"""

    collection: str
    score: float
    source_path: str
    locator: str
    text: str
    image_path: str = ""


class VectorStore(Protocol):
    def ensure_collection(self, name: str, dim: int) -> None:
        """コレクションが無ければ作る。あって次元が違えば StoreError。"""

    def drop_collection(self, name: str) -> None:
        """コレクションのベクターをすべて消す（無ければ何もしない）。"""

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        """source_path のチャンクを丸ごと入れ替える。"""

    def delete_source(self, name: str, source_path: str) -> None:
        """source_path のチャンクを消す。"""

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        """vector に近い順に最大 top_k 件返す（コレクションが無ければ空リスト）。"""

    def close(self) -> None: ...


def open_store(cfg: Config, *, transport: httpx.BaseTransport | None = None) -> VectorStore:
    """[vector_db].backend に応じた保存先を開く。使い終わったら close() すること。

    Args:
        cfg: 設定。
        transport: backend=qdrant のとき、テスト用に差し替える httpx のトランスポート。
    """
    if cfg.vector_db_backend == "qdrant":
        from .qdrant_store import QdrantStore

        return QdrantStore(
            cfg.vector_db_qdrant_url,
            prefix=cfg.vector_db_qdrant_collection_prefix,
            api_key=cfg.vector_db_qdrant_api_key,
            timeout=cfg.vector_db_qdrant_timeout_seconds,
            transport=transport,
        )
    from .sqlite_vec_store import SqliteVecStore

    return SqliteVecStore(cfg.vector_db_dir)
