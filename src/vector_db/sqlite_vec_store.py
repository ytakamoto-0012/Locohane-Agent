"""sqlite-vec によるベクターの保存先（[vector_db].dir/vectors.sqlite、サーバー不要）。

コレクションごとに vec0 仮想テーブル（vec_c<ID>、距離関数は作ったときの [vector_db].distance）を作り、チャンク本文は
通常テーブル chunks に同じ rowid で持つ。コレクション名をテーブル名に直接使わず
連番 ID を振るのは、名前に含まれうる記号を SQL 識別子に埋め込まないため。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

import sqlite_vec

from .sqlite_batch import SqliteBatchMixin, configure_connection
from .store import DISTANCE_METRICS, ChunkRecord, Hit, StoreError, distance_to_score

VECTORS_FILENAME = "vectors.sqlite"

# [vector_db].distance → vec0 の distance_metric（Dot は vec0 に無い。config.py で backend=sqlite_vec との組み合わせを拒否する）。
_METRICS = {"Cosine": "cosine", "Euclid": "l2", "Manhattan": "l1"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vec_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    dim INTEGER NOT NULL,
    distance TEXT NOT NULL DEFAULT 'Cosine'
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


class SqliteVecStore(SqliteBatchMixin):
    def __init__(self, directory: Path, *, distance: str = "Cosine"):
        if distance not in _METRICS:
            raise StoreError(f"sqlite-vec does not support distance {distance} (one of {'/'.join(_METRICS)}).")
        self._distance = distance
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / VECTORS_FILENAME
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
        except (AttributeError, sqlite3.OperationalError) as e:
            self._conn.close()
            raise StoreError(f"Cannot load sqlite-vec (this Python's sqlite3 may not support loading extensions): {e}") from e
        configure_connection(self._conn)
        self._conn.executescript(_SCHEMA)
        # 距離関数を選べるようになる前の DB（すべてコサイン距離で作っている）に列を足す。
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(vec_collections)")}
        if "distance" not in columns:
            with self._conn:
                self._conn.execute("ALTER TABLE vec_collections ADD COLUMN distance TEXT NOT NULL DEFAULT 'Cosine'")

    def close(self) -> None:
        self._conn.close()

    def _collection(self, name: str) -> tuple[int, int, str] | None:
        """(ID, 次元, 距離関数)。距離関数は作ったときの設定（今の [vector_db].distance と違うことがある）。"""
        row = self._conn.execute("SELECT id, dim, distance FROM vec_collections WHERE name = ?", (name,)).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def ensure_collection(self, name: str, dim: int) -> None:
        existing = self._collection(name)
        if existing is not None:
            if existing[1] != dim:
                raise StoreError(f"The vector dimension ({existing[1]}) of collection {name} differs from the embedding dimension ({dim}). Rebuild it.")
            if existing[2] != self._distance:
                raise StoreError(
                    f"The distance ({existing[2]}) of collection {name} differs from [vector_db].distance ({self._distance}). Rebuild it."
                )
            return
        with self._transaction():
            cur = self._conn.execute("INSERT INTO vec_collections (name, dim, distance) VALUES (?, ?, ?)", (name, dim, self._distance))
            self._conn.execute(
                f"CREATE VIRTUAL TABLE vec_c{int(cur.lastrowid)} USING vec0(embedding float[{int(dim)}] distance_metric={_METRICS[self._distance]})"
            )

    def apply_preset(self, name: str) -> None:
        """[vector_db].qdrant_preset は Qdrant 用のため何もしない（vec0 は全件と比べるため索引の設定が無い）。"""

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
        ids = self._conn.execute("SELECT id FROM chunks WHERE collection_id = ? AND source_path = ?", (cid, source_path)).fetchall()
        if not ids:
            return
        # vec0 は rowid の一致（=）でしか索引を使わないため、IN でまとめず1件ずつ（executemany で）消す。
        self._conn.executemany(f"DELETE FROM vec_c{cid} WHERE rowid = ?", ids)
        self._conn.execute("DELETE FROM chunks WHERE collection_id = ? AND source_path = ?", (cid, source_path))

    def delete_source(self, name: str, source_path: str) -> None:
        existing = self._collection(name)
        if existing is None:
            return
        with self._transaction():
            self._delete_source(existing[0], source_path)

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        existing = self._collection(name)
        if existing is None:
            raise StoreError(f"Collection {name} does not exist.")
        cid, dim, _ = existing
        pairs = list(zip(chunks, vectors, strict=True))
        for _chunk, vector in pairs:
            if len(vector) != dim:
                raise StoreError(f"The embedding dimension ({len(vector)}) differs from the dimension ({dim}) of collection {name}.")
        with self._transaction():
            self._delete_source(cid, source_path)
            if not pairs:
                return
            # まとめて（executemany で）入れるため、chunks の ID を AUTOINCREMENT の続きから自分で振る
            # （明示した ID は sqlite_sequence にも反映される）。読んでから入れるまでに他の接続が書き込まないよう、
            # まだ書き込みのトランザクションを始めていなければここで始める。
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'chunks'").fetchone()
            start = (row[0] if row else 0) + 1
            ids = range(start, start + len(pairs))
            self._conn.executemany(
                "INSERT INTO chunks (id, collection_id, source_path, locator, text) VALUES (?, ?, ?, ?, ?)",
                [(i, cid, source_path, chunk.locator, chunk.text) for i, (chunk, _v) in zip(ids, pairs)],
            )
            self._conn.executemany(
                f"INSERT INTO vec_c{cid} (rowid, embedding) VALUES (?, ?)",
                [(i, sqlite_vec.serialize_float32(vector)) for i, (_c, vector) in zip(ids, pairs)],
            )

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        """Hit.score は、Cosine では 1 - コサイン距離（コサイン類似度）、Euclid・Manhattan では 1 / (1 + 距離)。"""
        existing = self._collection(name)
        if existing is None:
            return []
        cid, dim, distance = existing
        if len(vector) != dim:
            raise StoreError(f"The dimension of the query embedding ({len(vector)}) differs from the dimension ({dim}) of collection {name}.")
        rows = self._conn.execute(
            f"""
            SELECT c.source_path, c.locator, c.text, v.distance
            FROM (SELECT rowid, distance FROM vec_c{cid} WHERE embedding MATCH ? AND k = ?) v
            JOIN chunks c ON c.id = v.rowid
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(vector), int(top_k)),
        ).fetchall()
        to_score = distance_to_score if distance in DISTANCE_METRICS else (lambda d: 1.0 - float(d))
        return [Hit(collection=name, score=to_score(d), source_path=p, locator=loc, text=t) for p, loc, t, d in rows]
