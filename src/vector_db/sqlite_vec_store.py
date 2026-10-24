"""sqlite-vec によるベクターの保存先（[vector_db].dir/vectors.sqlite、サーバー不要）。

コレクションごとに vec0 仮想テーブル（vec_c<ID>、コサイン距離）を作り、チャンク本文は
通常テーブル chunks に同じ rowid で持つ。コレクション名をテーブル名に直接使わず
連番 ID を振るのは、名前に含まれうる記号を SQL 識別子に埋め込まないため。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

import sqlite_vec

from .store import ChunkRecord, Hit, StoreError

VECTORS_FILENAME = "vectors.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vec_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    dim INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    locator TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks (collection_id, source_path);
"""


class SqliteVecStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / VECTORS_FILENAME
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
        except (AttributeError, sqlite3.OperationalError) as e:
            self._conn.close()
            raise StoreError(f"sqlite-vec を読み込めません（この Python の sqlite3 は拡張の読み込みに対応していない可能性があります）: {e}") from e
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _collection(self, name: str) -> tuple[int, int] | None:
        row = self._conn.execute("SELECT id, dim FROM vec_collections WHERE name = ?", (name,)).fetchone()
        return (row[0], row[1]) if row else None

    def ensure_collection(self, name: str, dim: int) -> None:
        existing = self._collection(name)
        if existing is not None:
            if existing[1] != dim:
                raise StoreError(f"コレクション {name} のベクターの次元（{existing[1]}）と埋め込みの次元（{dim}）が違います。再構築してください。")
            return
        with self._conn:
            cur = self._conn.execute("INSERT INTO vec_collections (name, dim) VALUES (?, ?)", (name, dim))
            self._conn.execute(f"CREATE VIRTUAL TABLE vec_c{int(cur.lastrowid)} USING vec0(embedding float[{int(dim)}] distance_metric=cosine)")

    def drop_collection(self, name: str) -> None:
        existing = self._collection(name)
        if existing is None:
            return
        cid = existing[0]
        with self._conn:
            self._conn.execute(f"DROP TABLE IF EXISTS vec_c{cid}")
            self._conn.execute("DELETE FROM chunks WHERE collection_id = ?", (cid,))
            self._conn.execute("DELETE FROM vec_collections WHERE id = ?", (cid,))

    def _delete_source(self, cid: int, source_path: str) -> None:
        ids = [r[0] for r in self._conn.execute("SELECT id FROM chunks WHERE collection_id = ? AND source_path = ?", (cid, source_path))]
        for chunk_id in ids:
            self._conn.execute(f"DELETE FROM vec_c{cid} WHERE rowid = ?", (chunk_id,))
        self._conn.execute("DELETE FROM chunks WHERE collection_id = ? AND source_path = ?", (cid, source_path))

    def delete_source(self, name: str, source_path: str) -> None:
        existing = self._collection(name)
        if existing is None:
            return
        with self._conn:
            self._delete_source(existing[0], source_path)

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        existing = self._collection(name)
        if existing is None:
            raise StoreError(f"コレクション {name} がありません。")
        cid, dim = existing
        with self._conn:
            self._delete_source(cid, source_path)
            for chunk, vector in zip(chunks, vectors, strict=True):
                if len(vector) != dim:
                    raise StoreError(f"埋め込みの次元（{len(vector)}）がコレクション {name} の次元（{dim}）と違います。")
                cur = self._conn.execute(
                    "INSERT INTO chunks (collection_id, source_path, locator, text) VALUES (?, ?, ?, ?)",
                    (cid, source_path, chunk.locator, chunk.text),
                )
                self._conn.execute(
                    f"INSERT INTO vec_c{cid} (rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(vector)),
                )

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        existing = self._collection(name)
        if existing is None:
            return []
        cid, dim = existing
        if len(vector) != dim:
            raise StoreError(f"検索語の埋め込みの次元（{len(vector)}）がコレクション {name} の次元（{dim}）と違います。")
        rows = self._conn.execute(
            f"""
            SELECT c.source_path, c.locator, c.text, v.distance
            FROM (SELECT rowid, distance FROM vec_c{cid} WHERE embedding MATCH ? AND k = ?) v
            JOIN chunks c ON c.id = v.rowid
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(vector), int(top_k)),
        ).fetchall()
        return [Hit(collection=name, score=1.0 - float(d), source_path=p, locator=loc, text=t) for p, loc, t, d in rows]
