"""SQLite の書き込みをまとめてコミットする仕組み（カタログ・sqlite-vec・キーワード索引で共通）。

取り込みはファイルごとに数回の書き込みをするため、1回ずつコミットすると（WAL でもコミットごとに
ディスクへの書き出しを待つため）ファイル数が多いほど遅くなる。begin_batch() から commit() までは
コミットせずに1つのトランザクションにまとめる。
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3


def configure_connection(conn: sqlite3.Connection) -> None:
    """WAL にし、コミットのたびのディスクへの書き出しの待ちを減らす（synchronous=NORMAL）。

    WAL で NORMAL なら、電源断・OS の停止で直前のコミットが失われることはあっても DB は壊れない
    （取り込みはカタログを最後に書くため、失われた分は次の差分取り込みで取り込み直される）。
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


class SqliteBatchMixin:
    """self._conn への書き込みを、begin_batch() から commit() までまとめる。

    書き込むメソッドは `with self._transaction():` の中で書く。まとめていないときは従来どおり
    抜けるときにコミットする。
    """

    _conn: sqlite3.Connection
    _batching: bool = False

    @contextmanager
    def _transaction(self):
        if self._batching:
            yield
            return
        with self._conn:
            yield

    def begin_batch(self) -> None:
        self._batching = True

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def end_batch(self) -> None:
        self._conn.commit()
        self._batching = False
