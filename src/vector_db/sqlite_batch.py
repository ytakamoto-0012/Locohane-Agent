"""SQLite の書き込みをまとめてコミットする仕組み（カタログ・sqlite-vec・キーワード索引で共通）。

取り込みはファイルごとに数回の書き込みをするため、1回ずつコミットすると（WAL でもコミットごとに
ディスクへの書き出しを待つため）ファイル数が多いほど遅くなる。begin_batch() から commit() までは
コミットせずに1つのトランザクションにまとめる。
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time

# journal_mode を WAL へ切り替えるときに、ほかの接続のロックが外れるのを待つ上限（秒。各接続の timeout と同じ）。
_WAL_SWITCH_TIMEOUT_SECONDS = 30.0


def configure_connection(conn: sqlite3.Connection) -> None:
    """WAL にし、コミットのたびのディスクへの書き出しの待ちを減らす（synchronous=NORMAL）。

    WAL で NORMAL なら、電源断・OS の停止で直前のコミットが失われることはあっても DB は壊れない
    （取り込みはカタログを最後に書くため、失われた分は次の差分取り込みで取り込み直される）。

    まだ WAL になっていない DB（作ったばかりのファイル）を複数の接続が同時に開くと、WAL への切り替えは
    接続の timeout（busy handler）で待たずに「database is locked」になるため、ここで間隔を空けて試し直す
    （同じインスタンスの複数のコレクションの取り込みを同時に始めたとき等）。
    """
    deadline = time.monotonic() + _WAL_SWITCH_TIMEOUT_SECONDS
    delay = 0.01
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as e:
            message = str(e).lower()
            if ("locked" not in message and "busy" not in message) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
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
