"""SQLite の書き込みの共通部分（カタログ・sqlite-vec・キーワード索引・取り込みジャーナルで共通）。

- 接続の busy timeout（SQLITE_TIMEOUT_SECONDS）と WAL の設定（connect・configure_connection）。
- インスタンス単位の書き込みロック（instance_write_lock）。同じインスタンスの DB（[vector_db].dir の下）へ書くときは、
  プロセス内でこのロックを取ってから書く。同じインスタンスの複数のコレクションを同時に取り込むと、SQLite の
  busy handler は待つ順番が公平でなく、運の悪い接続が待たされ続けて「database is locked」になるため、
  プロセス内では順番に並べる（別のプロセスとの競合は busy timeout とやり直し（retry_locked）で待つ）。
- まとめ書き（SqliteBatchMixin.apply_deferred）。取り込みはファイルごとに数回の書き込みをするため、1回ずつ
  コミットすると（WAL でもコミットごとにディスクへの書き出しを待つため）ファイル数が多いほど遅くなる。
  書き込みをためておき、1つの短いトランザクションでまとめて書く。
"""

from __future__ import annotations

from contextlib import contextmanager
import functools
import logging
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

# 接続の busy timeout（秒）。ほかの接続の書き込みのロックが外れるのを、この秒数まで待つ。
SQLITE_TIMEOUT_SECONDS = 120.0
# journal_mode を WAL へ切り替えるときに、ほかの接続のロックが外れるのを待つ上限（秒。各接続の timeout と同じ）。
_WAL_SWITCH_TIMEOUT_SECONDS = SQLITE_TIMEOUT_SECONDS
# retry_locked() が「database is locked」でやり直し続ける上限（秒）と、やり直すまでの間隔の最初・最大（秒）。
_RETRY_LOCKED_SECONDS = 600.0
_RETRY_FIRST_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 10.0

_T = TypeVar("_T")

# instance_write_lock() のロック（正規化した [vector_db].dir → ロック）。
_instance_locks: dict[str, threading.RLock] = {}
_instance_locks_guard = threading.Lock()


def instance_write_lock(directory: Path) -> threading.RLock:
    """directory（インスタンスの [vector_db].dir）の DB へ書き込むときに取る、プロセス内で共有するロック。

    同じスレッドで重ねて取れる（RLock）。
    """
    key = os.path.normcase(os.path.abspath(directory))
    with _instance_locks_guard:
        lock = _instance_locks.get(key)
        if lock is None:
            lock = _instance_locks[key] = threading.RLock()
        return lock


def connect(path: Path, **kwargs) -> sqlite3.Connection:
    """busy timeout を SQLITE_TIMEOUT_SECONDS にして path を開く。

    開いたスレッドと別のスレッドでも使えるようにする（取り込みは開いたスレッドとは別の書き込みのスレッドで書く。
    ingest._WriterThread）。同時に複数のスレッドから使わないことは呼び出し側で守る。
    """
    return sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SECONDS, check_same_thread=False, **kwargs)


def is_locked_error(error: BaseException) -> bool:
    """ほかの接続の書き込みのロックで失敗したか（「database is locked」・SQLITE_BUSY）。"""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return "locked" in message or "busy" in message


def retry_locked(func: Callable[[], _T], *, what: str, seconds: float | None = None) -> _T:
    """func() を呼び、ほかの接続のロックで失敗したら間隔を空けて（指数的に延ばし、揺らぎを加えて）やり直す。

    func は失敗したら書きかけを巻き戻し、何度呼んでも同じ結果になるものにする。seconds 秒
    （省略時は _RETRY_LOCKED_SECONDS）たっても失敗するなら、最後のエラーをそのまま投げる。
    """
    deadline = time.monotonic() + (_RETRY_LOCKED_SECONDS if seconds is None else seconds)
    delay = _RETRY_FIRST_DELAY_SECONDS
    attempt = 0
    while True:
        try:
            return func()
        except sqlite3.OperationalError as e:
            attempt += 1
            if not is_locked_error(e) or time.monotonic() >= deadline:
                raise
            wait = min(delay, _RETRY_MAX_DELAY_SECONDS) * random.uniform(0.5, 1.0)
            logger.warning("vector_db: %s failed because the database is locked (%s). Retrying in %.1fs (attempt %d).", what, e, wait, attempt)
            time.sleep(wait)
            delay *= 2


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
            if not is_locked_error(e) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
    conn.execute("PRAGMA synchronous=NORMAL")


class SqliteBatchMixin:
    """self._conn（self.path の DB）への書き込みの共通部分。

    書き込むメソッドは `with self._transaction():` の中で書く。apply_deferred() の中で呼ばれたときは
    そのトランザクションにまとめ、それ以外はインスタンスの書き込みロック（instance_write_lock）を取って
    その場でコミットする。
    """

    _conn: sqlite3.Connection
    path: Path
    _batching: bool = False

    @contextmanager
    def _write(self):
        """インスタンスの書き込みロックを取り、抜けるときにコミットする（例外なら巻き戻す）。"""
        with instance_write_lock(self.path.parent), self._conn:
            yield

    @contextmanager
    def _transaction(self):
        if self._batching:
            yield
            return
        with self._write():
            yield

    def apply_deferred(self, ops: list[Callable[[], None]], *, what: str = "write") -> None:
        """ops（このオブジェクトへの書き込みのメソッドの呼び出し）を、1つのトランザクションで順に行ってコミットする。

        インスタンスの書き込みロックを取り、最初に書き込みのロック（BEGIN IMMEDIATE）を取ってから書くため、
        ロックを持つのは書いている間だけ。ほかのプロセスのロックで失敗したら巻き戻し、間隔を空けて最初から
        やり直す（retry_locked。ops は何度呼んでも同じ結果になるよう、ためた値で書くものにする）。
        """
        if not ops:
            return

        def attempt() -> None:
            with instance_write_lock(self.path.parent):
                if not self._conn.in_transaction:
                    self._conn.execute("BEGIN IMMEDIATE")
                self._batching = True
                try:
                    for op in ops:
                        op()
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
                finally:
                    self._batching = False

        retry_locked(attempt, what=f"{what} ({self.path.name})")

    # --- collection_copy 用（begin_batch() から commit() までのトランザクションを開いたままにする） ---

    def begin_batch(self) -> None:
        self._batching = True

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def end_batch(self) -> None:
        self._conn.commit()
        self._batching = False


class DeferredWrites:
    """SqliteBatchMixin の DB への書き込みをためておき、apply() でまとめて書く（取り込み・コピー用）。

    書き込みのメソッドを呼ぶと、その場では書かずに呼び出しを記録する。apply() は記録した呼び出しを
    1つの短いトランザクションで順に行う（SqliteBatchMixin.apply_deferred）。Qdrant への送信や
    コピー元の読み出しを待つ間に書き込みのロックを持たないため。ロック競合でやり直すことがあるため、
    渡す値は後から変えないもの（リストはその場で作ったもの）にする。
    """

    def __init__(self, target: SqliteBatchMixin, what: str):
        self.target, self.what = target, what
        self.ops: list[Callable[[], None]] = []

    def __getattr__(self, name: str):
        method = getattr(self.target, name)

        def defer(*args, **kwargs) -> None:
            self.ops.append(functools.partial(method, *args, **kwargs))

        return defer

    def __len__(self) -> int:
        """ためている書き込みの数。"""
        return len(self.ops)

    def apply(self) -> None:
        self.target.apply_deferred(self.ops, what=self.what)
        self.ops = []

    def discard(self) -> None:
        self.ops = []
