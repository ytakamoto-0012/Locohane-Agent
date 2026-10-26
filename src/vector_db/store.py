"""ベクターの保存先（バックエンド）の共通インターフェースと生成。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Config


class StoreError(Exception):
    """ベクターの保存先を操作できなかった（接続できない・次元・距離関数が合わない等）。"""


# 値が小さいほど近い距離関数（[vector_db].distance）。Hit.score を「大きいほど近い」に
# そろえるため、検索結果の距離を distance_to_score() で換算する。
DISTANCE_METRICS = frozenset({"Euclid", "Manhattan"})


def distance_to_score(distance: float) -> float:
    """Euclid・Manhattan の距離（0 以上、小さいほど近い）を (0, 1] の類似度にする（1 / (1 + 距離)）。"""
    return 1.0 / (1.0 + max(float(distance), 0.0))


# [vector_db].image_input = true でページ画像を埋め込んだベクターは、本文のベクターと
# 類似度の出方が違う（検索語はテキストのため、画像との類似度は低めに出る）ので、
# 同じ順位付けに混ぜないよう別のコレクションに入れる。コレクション名には "." を
# 使えない（catalog.validate_name）ため、利用者のコレクション名とは衝突しない。
IMAGE_COLLECTION_SUFFIX = ".image"


def image_collection_name(name: str) -> str:
    """コレクション name のページ画像のベクターを入れる保存先のコレクション名。"""
    return name + IMAGE_COLLECTION_SUFFIX


# ファイルのパス（取り込み元フォルダからの相対パス）を1ファイル1件で埋め込んだベクターの保存先。
# 写真・図面のように中身から内容を読み取れないファイルでも、ファイル名・フォルダ名に内容が
# 書かれていることが多いため、パスの文字列でも意味検索できるようにする（search.search_paths）。
PATH_COLLECTION_SUFFIX = ".path"


def path_collection_name(name: str) -> str:
    """コレクション name のファイルパスのベクターを入れる保存先のコレクション名。"""
    return name + PATH_COLLECTION_SUFFIX


def storage_collection_names(name: str) -> tuple[str, str, str]:
    """コレクション name が使う保存先のコレクション（本文・ページ画像・ファイルパス）の名前。"""
    return name, image_collection_name(name), path_collection_name(name)


@dataclass(frozen=True)
class ChunkRecord:
    """保存する1チャンク。locator はファイル内の位置（extract.Segment.locator）。

    ページ画像のチャンクの text は、検索結果の抜粋に使うそのページの文字。
    ファイルパスのチャンクは locator が空で、text が取り込み元フォルダからの相対パス。
    """

    locator: str
    text: str


@dataclass(frozen=True)
class Hit:
    """検索で見つかった1チャンク。score は大きいほど近い類似度（1.0 が完全一致）。

    コレクションの距離関数が Cosine ならコサイン類似度、Dot（Qdrant のみ）なら内積、
    Euclid・Manhattan なら distance_to_score() で換算した値。キーワード検索では 0.0。

    matched は一致した検索（"text" = 本文の埋め込み、"keyword" = 本文のキーワード、
    "image" = ページ画像、"path" = ファイルパスの埋め込み、"path_keyword" = ファイルパスのキーワード
    （この2つは search.search_paths()））。other_chunks は同じ位置で
    他に一致した本文のチャンクの数。どちらも保存先の search() は既定値のまま返し、search.search() が
    結果を統合するときに入れる。
    """

    collection: str
    score: float
    source_path: str
    locator: str
    text: str
    matched: tuple[str, ...] = ()
    other_chunks: int = 0


class VectorStore(Protocol):
    def ensure_collection(self, name: str, dim: int) -> None:
        """コレクションが無ければ [vector_db].distance の距離関数で作る。あって次元・距離関数が違えば StoreError（再構築が必要）。"""

    def apply_preset(self, name: str) -> None:
        """既存のコレクションの設定を [vector_db].qdrant_preset に合わせる（Qdrant のみ。sqlite-vec では何もしない）。"""

    def drop_collection(self, name: str) -> None:
        """コレクションのベクターをすべて消す（無ければ何もしない）。"""

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        """source_path のチャンクを丸ごと入れ替える。"""

    def delete_source(self, name: str, source_path: str) -> None:
        """source_path のチャンクを消す。"""

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        """vector に近い順に最大 top_k 件返す（コレクションが無ければ空リスト）。"""

    def dim(self, name: str) -> int | None:
        """コレクションのベクターの次元（無ければ None）。"""

    def get_source(self, name: str, source_path: str) -> tuple[list[ChunkRecord], list[list[float]]]:
        """source_path のチャンクとベクターを入れた順に返す（インスタンス間のコピー用。無ければ空）。"""

    def close(self) -> None: ...


def open_store(cfg: Config, *, transport: httpx.BaseTransport | None = None, shared: bool = False) -> VectorStore:
    """[vector_db].backend に応じた保存先を開く。使い終わったら close() すること。

    Args:
        cfg: 設定。
        transport: backend=qdrant のとき、テスト用に差し替える httpx のトランスポート。
        shared: backend=qdrant のとき、Qdrant への接続をプロセス内で使い回す（検索用。
            qdrant_store.shared_client 参照）。transport を渡したときは使わない。
    """
    if cfg.vector_db_backend == "qdrant":
        from .qdrant_store import QdrantStore

        return QdrantStore(
            cfg.vector_db_qdrant_url,
            prefix=cfg.vector_db_qdrant_collection_prefix,
            api_key=cfg.vector_db_qdrant_api_key,
            timeout=cfg.vector_db_qdrant_timeout_seconds,
            distance=cfg.vector_db_distance,
            preset=cfg.vector_db_qdrant_preset,
            transport=transport,
            shared=shared and transport is None,
        )
    from .sqlite_vec_store import SqliteVecStore

    return SqliteVecStore(cfg.vector_db_dir, distance=cfg.vector_db_distance)
