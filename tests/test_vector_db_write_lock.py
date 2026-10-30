"""SQLite の書き込みのロック（sqlite_batch.py）と、取り込みが書き込みのロックを長く持たないことのテスト。

- インスタンス単位の書き込みロック（instance_write_lock）と、ロック競合のやり直し（retry_locked・apply_deferred）。
- 取り込み（ingest._Writer）は SQLite へ書く分をためておき、コミットのときだけ短く書く
  （Qdrant への送信・埋め込みを待つ間にロックを持たない）。
- 同じインスタンスの複数のコレクションを同時に取り込んでも「database is locked」にならない。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import time

import httpx
import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, make_config
from src.vector_db import catalog as vcat
from src.vector_db import collection_copy, embeddings, ingest, qdrant_store, sqlite_batch
from src.vector_db.keyword_index import KEYWORDS_FILENAME


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(embeddings, "_sleep", lambda seconds: None)
    monkeypatch.setattr(embeddings, "_failed_until", {})
    monkeypatch.setattr(embeddings, "_float_only", set())
    monkeypatch.setattr(qdrant_store, "_sleep", lambda seconds: None)


def _try_write(path: Path) -> str:
    """別の接続（別のプロセスの代わり）から、すぐに書き込みのロックを取れるか試す（取れたら "ok"）。"""
    conn = sqlite3.connect(str(path), timeout=0.2)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return "ok"
    except sqlite3.OperationalError as e:
        return str(e)
    finally:
        conn.close()


# --- sqlite_batch ---------------------------------------------------------------------------


def test_retry_locked_retries_only_lock_errors(monkeypatch):
    monkeypatch.setattr(sqlite_batch.time, "sleep", lambda seconds: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "done"

    assert sqlite_batch.retry_locked(flaky, what="test") == "done"
    assert calls["n"] == 3

    def broken():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: x")

    calls["n"] = 0
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        sqlite_batch.retry_locked(broken, what="test")
    assert calls["n"] == 1


def test_retry_locked_gives_up_after_the_deadline(monkeypatch):
    monkeypatch.setattr(sqlite_batch.time, "sleep", lambda seconds: None)

    def locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sqlite_batch.retry_locked(locked, what="test", seconds=0)


def test_instance_write_lock_is_shared_per_directory(tmp_path):
    a = tmp_path / "inst_a" / "vector_db"
    b = tmp_path / "inst_b" / "vector_db"
    assert sqlite_batch.instance_write_lock(a) is sqlite_batch.instance_write_lock(a / ".." / "vector_db")
    assert sqlite_batch.instance_write_lock(a) is not sqlite_batch.instance_write_lock(b)


def test_apply_deferred_retries_while_another_process_holds_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_batch, "SQLITE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(sqlite_batch, "_RETRY_FIRST_DELAY_SECONDS", 0.05)
    directory = tmp_path / "vector_db"
    with vcat.open_catalog(directory) as catalog:
        catalog.create("docs", str(tmp_path))
        # ロックはタイマーのスレッドで外す。
        other = sqlite3.connect(str(directory / vcat.CATALOG_FILENAME), timeout=1, check_same_thread=False)
        other.execute("BEGIN IMMEDIATE")
        releaser = threading.Timer(0.5, other.rollback)
        releaser.start()
        try:
            catalog.apply_deferred([lambda: catalog.set_model("docs", "m", 3)])
        finally:
            releaser.join()
            other.close()
        assert (catalog.get("docs").model, catalog.get("docs").dim) == ("m", 3)


# --- 取り込み ---------------------------------------------------------------------------------


def test_sqlite_locks_are_not_held_while_sending_to_qdrant(tmp_path, monkeypatch):
    """Qdrant へ送って応答を待っている間も、別のプロセスがカタログ・キーワード索引へすぐ書き込める。"""
    monkeypatch.setattr(ingest._Writer, "_COMMIT_EVERY_FILES", 2)
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(6):
        (docs / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_backend="qdrant")
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/batch"):
            for name in (vcat.CATALOG_FILENAME, KEYWORDS_FILENAME):
                attempts.append(_try_write(cfg.vector_db_dir / name))
        return qdrant.handler(request)

    result = ingest.ingest_collection(
        cfg, "docs", embedding_transport=embed.transport, store_transport=httpx.MockTransport(handler)
    )
    assert result.added == 6
    assert attempts and set(attempts) == {"ok"}


def test_copy_does_not_hold_sqlite_locks_while_sending_to_qdrant(tmp_path, monkeypatch):
    """コレクションのコピーも、コピー先の Qdrant へ送っている間はコピー先のカタログ・キーワード索引のロックを持たない。"""
    monkeypatch.setattr(collection_copy, "_COMMIT_INTERVAL_SECONDS", 0.0)
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(5):
        (docs / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    src = make_config(tmp_path / "src", vector_db_backend="qdrant")
    dst = make_config(tmp_path / "dst", vector_db_backend="qdrant", vector_db_qdrant_collection_prefix="locohane_dst_")
    with vcat.open_catalog(src.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    assert ingest.ingest_collection(src, "docs", embedding_transport=embed.transport, store_transport=qdrant.transport).added == 5
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/batch") and "locohane_dst_" in request.url.path:
            for name in (vcat.CATALOG_FILENAME, KEYWORDS_FILENAME):
                attempts.append(_try_write(dst.vector_db_dir / name))
        return qdrant.handler(request)

    t = httpx.MockTransport(handler)
    result = collection_copy.copy_collection(src, "docs", dst, "copied", src_store_transport=t, dst_store_transport=t)
    assert result["files"] == 5
    assert attempts and set(attempts) == {"ok"}


def test_embeddings_keep_being_sent_while_qdrant_is_slow(tmp_path, monkeypatch):
    """書き込みは専用のスレッドで行うため、Qdrant の応答を待つ間も次の埋め込みを送り続ける。"""
    monkeypatch.setattr(ingest._Writer, "_COMMIT_EVERY_FILES", 2)
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(30):
        (docs / f"note{i:02d}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_backend="qdrant", vector_db_ingest_workers=1, vector_db_embedding_batch_size=2)
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    state = {"embeds": 0, "sent_while_blocked": None}

    def embed_handler(request: httpx.Request) -> httpx.Response:
        state["embeds"] += 1
        return embed.handler(request)

    def qdrant_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/batch") and state["sent_while_blocked"] is None:
            # 最初の書き込みの応答を返さずに、その間に埋め込みが送られるかを見る（送られなければ 3 秒で諦める）。
            before = state["embeds"]
            deadline = time.monotonic() + 3
            while state["embeds"] <= before and time.monotonic() < deadline:
                time.sleep(0.01)
            state["sent_while_blocked"] = state["embeds"] > before
        return qdrant.handler(request)

    result = ingest.ingest_collection(
        cfg, "docs", embedding_transport=httpx.MockTransport(embed_handler), store_transport=httpx.MockTransport(qdrant_handler)
    )
    assert result.added == 30
    assert state["sent_while_blocked"] is True


@pytest.mark.parametrize("backend", ["sqlite_vec", "qdrant"])
def test_parallel_ingests_of_one_instance_do_not_lock(tmp_path, monkeypatch, backend):
    """同じインスタンスの複数のコレクションを同時に取り込んでも、互いのロックで失敗しない。"""
    # busy timeout を短くしても、プロセス内はインスタンスの書き込みロックで順番に並ぶため失敗しない。
    monkeypatch.setattr(sqlite_batch, "SQLITE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(ingest._Writer, "_COMMIT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(ingest._Writer, "_COMMIT_EVERY_FILES", 3)
    cfg = make_config(tmp_path, vector_db_backend=backend, vector_db_embedding_batch_size=4)
    names = [f"c{i}" for i in range(4)]
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        for name in names:
            folder = tmp_path / name
            folder.mkdir()
            for j in range(15):
                (folder / f"doc{j}.md").write_text(f"{name}の手順{j}。" * 5, encoding="utf-8")
            catalog.create(name, str(folder))
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    lock = threading.Lock()

    def slow_qdrant(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/batch"):
            time.sleep(0.01)
        with lock:
            return qdrant.handler(request)

    slots = ingest.IngestSlots(4, 0, 8)
    results: dict[str, object] = {}

    def run(name: str) -> None:
        try:
            results[name] = ingest.ingest_collection(
                cfg, name, embedding_transport=embed.transport, store_transport=httpx.MockTransport(slow_qdrant), slots=slots
            )
        except Exception as e:  # noqa: BLE001 - テストで失敗の内容を見る
            results[name] = e

    threads = [threading.Thread(target=run, args=(name,)) for name in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    for name in names:
        result = results[name]
        assert isinstance(result, ingest.IngestResult), result
        assert (result.added, result.failed) == (15, [])
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        assert all(len(catalog.files(name)) == 15 for name in names)
