"""取り込みの高速化（複数ファイルのまとめ埋め込み・バッチの同時送信・base64 の受け取り・まとめ書き込み・
子プロセスでの抽出・Office の使い回し）のテスト。"""

from __future__ import annotations

import sys
import threading
import time
import types

import httpx
import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, fake_vector, make_config
from src.vector_db import catalog as vcat
from src.vector_db import embeddings, ingest, render, search
from src.vector_db.embeddings import embed_texts


@pytest.fixture(autouse=True)
def _reset_embedding_state(monkeypatch):
    monkeypatch.setattr(embeddings, "_sleep", lambda seconds: None)
    monkeypatch.setattr(embeddings, "_failed_until", {})
    monkeypatch.setattr(embeddings, "_float_only", set())


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    cfg = make_config(tmp_path, vector_db_backend=request.param)
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return {
        "cfg": cfg,
        "docs": docs,
        "embed": embed,
        "qdrant": qdrant,
        "kwargs": {"embedding_transport": embed.transport, "store_transport": qdrant.transport},
    }


def _config(env, **changes):
    return make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, **changes)


def _slow(embed: FakeEmbeddingServer, seconds: float = 0.05):
    """embed へ渡す前に待つトランスポートと、同時に処理していたリクエスト数の最大を記録する state。"""
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def handler(request):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(seconds)
            return embed.handler(request)
        finally:
            with lock:
                state["active"] -= 1

    return httpx.MockTransport(handler), state


# --- 埋め込みの受け取り方（encoding_format） -------------------------------------------


def test_base64_embeddings_match_float(tmp_path):
    server = FakeEmbeddingServer()
    cfg = make_config(tmp_path, vector_db_embedding_encoding_format="base64")
    vectors = embed_texts(cfg, ["溶接", "検査"], kind="document", transport=server.transport)
    assert server.requests[0]["encoding_format"] == "base64"
    assert len(vectors) == 2
    assert vectors[0] == pytest.approx(fake_vector("溶接"), abs=1e-6)
    assert vectors[1] == pytest.approx(fake_vector("検査"), abs=1e-6)


def test_base64_falls_back_to_float_for_servers_without_support(tmp_path):
    server = FakeEmbeddingServer(reject_base64=True)
    cfg = make_config(tmp_path, vector_db_embedding_encoding_format="base64")
    assert embed_texts(cfg, ["a"], kind="document", transport=server.transport) == [pytest.approx(fake_vector("a"))]
    assert ["encoding_format" in r for r in server.requests] == [True, False]
    # 以後この接続先は最初から float で送る
    embed_texts(cfg, ["b"], kind="document", transport=server.transport)
    assert "encoding_format" not in server.requests[-1] and len(server.requests) == 3


def test_float_format_does_not_send_encoding_format(tmp_path):
    server = FakeEmbeddingServer()
    embed_texts(make_config(tmp_path), ["a"], kind="document", transport=server.transport)
    assert "encoding_format" not in server.requests[0]


def test_ingest_with_base64_embeddings(env):
    (env["docs"] / "weld.md").write_text("溶接ビードの幅は5mm以上とする。", encoding="utf-8")
    cfg = _config(env, vector_db_embedding_encoding_format="base64")
    assert ingest.ingest_collection(cfg, "docs", **env["kwargs"]).added == 1
    hits = search.search(cfg, "溶接ビードの幅", **env["kwargs"]).hits
    assert hits[0].text == "溶接ビードの幅は5mm以上とする。"


# --- 埋め込みのまとめ方・同時送信 ---------------------------------------------------------


def test_small_files_are_embedded_together(env):
    """埋め込みが動いている間に準備できた小さいファイルは、次のリクエストにまとめて送る。"""
    for i in range(12):
        (env["docs"] / f"note{i:02d}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    transport, _state = _slow(env["embed"])
    kwargs = {**env["kwargs"], "embedding_transport": transport}
    result = ingest.ingest_collection(env["cfg"], "docs", **kwargs)
    assert result.added == 12
    sizes = [len(r["input"]) for r in env["embed"].requests]
    # 1ファイルは本文1件＋パス1件。複数のファイルを1リクエストにまとめるため、ファイル数よりリクエストが少ない
    assert sum(sizes) == 24 and len(sizes) < 12 and max(sizes) > 2
    assert all(size <= env["cfg"].vector_db_embedding_batch_size for size in sizes)
    assert search.list_collections(env["cfg"])[0].file_count == 12


def test_batches_of_one_large_file_are_sent_concurrently(env):
    """大きいファイルのチャンクも、embedding_batch_size 件ずつのバッチを ingest_workers 個まで同時に送る。"""
    text = "\n\n".join(f"第{i}条 項目{i}の基準は{i}mmとする。" + "あ" * 150 for i in range(30))
    (env["docs"] / "big.md").write_text(text, encoding="utf-8")
    transport, state = _slow(env["embed"])
    cfg = _config(env, vector_db_ingest_workers=3, vector_db_embedding_batch_size=4)
    result = ingest.ingest_collection(cfg, "docs", **{**env["kwargs"], "embedding_transport": transport})
    assert result.added == 1 and result.chunks >= 20
    assert 1 < state["peak"] <= 3
    hits = search.search(cfg, "第7条 項目7の基準は7mmとする。" + "あ" * 150, top_k=1, **env["kwargs"]).hits
    assert hits[0].text.startswith("第7条") or "第7条" in hits[0].text


def test_stop_while_waiting_for_embeddings_records_only_finished_files(env):
    for i in range(4):
        (env["docs"] / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    stop = {"requested": False}

    def handler(request):
        stop["requested"] = True  # 最初のリクエストを受けたら停止を求める
        return env["embed"].handler(request)

    kwargs = {**env["kwargs"], "embedding_transport": httpx.MockTransport(handler)}
    result = ingest.ingest_collection(env["cfg"], "docs", should_stop=lambda: stop["requested"], **kwargs)
    assert result.removed == 0
    with vcat.open_catalog(env["cfg"].vector_db_dir) as catalog:
        recorded = len(catalog.files("docs"))
    assert recorded == result.added < 4
    again = ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"])
    assert again.added == 4 - recorded and again.unchanged == recorded


# --- 書き込み -------------------------------------------------------------------------


def test_qdrant_updates_are_sent_in_batches(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(5):
        (docs / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_backend="qdrant")
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport, store_transport=qdrant.transport)
    updates = [(m, p) for m, p in qdrant.calls if "/points" in p and not p.endswith("/query")]
    # ファイルごとに削除・追加を送らず、コレクション（本文・画像・パス）ごとに points/batch でまとめて送る
    assert {p.rsplit("/", 1)[-1] for _m, p in updates} == {"batch"}
    assert len(updates) <= 3
    assert len(qdrant.collections["locohane_test_docs"]["points"]) == 5
    assert len(qdrant.collections["locohane_test_docs.path"]["points"]) == 5


def test_error_discards_uncommitted_writes(env, monkeypatch):
    """書き込みの途中でエラーになったら、コミットしていない分は記録せず、次回取り込み直す。"""
    for i in range(3):
        (env["docs"] / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    real_write = ingest._Writer._write
    calls = {"n": 0}

    def failing_write(self, prepared):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        real_write(self, prepared)

    monkeypatch.setattr(ingest._Writer, "_write", failing_write)
    with pytest.raises(OSError):
        ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"])
    monkeypatch.setattr(ingest._Writer, "_write", real_write)
    with vcat.open_catalog(env["cfg"].vector_db_dir) as catalog:
        assert catalog.files("docs") == {}
    again = ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"])
    assert again.added == 3
    assert search.list_collections(env["cfg"])[0].chunk_count == 3


# --- 準備（抽出・画像化） ------------------------------------------------------------------


def test_scan_source_files_returns_same_stat_as_path_stat(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text("a", encoding="utf-8")
    [(path, st)] = ingest.scan_source_files(tmp_path, True, (".md",)).files
    assert (st.st_size, st.st_mtime) == (path.stat().st_size, path.stat().st_mtime)


@pytest.mark.parametrize("workers", [1, 2])
def test_small_files_start_first(env, workers):
    # パスの順（a → b）とサイズの順（b → a）を逆にしておく。
    (env["docs"] / "a_large.md").write_text("大" * 500, encoding="utf-8")
    (env["docs"] / "b_small.md").write_text("小", encoding="utf-8")
    started = []
    cfg = _config(env, vector_db_ingest_workers=workers)
    # 記録が終わったときの通知は直前に読み始めたファイルの path のまま来るため、続けて同じ path は数えない。
    progress = lambda done, total, path: path and started[-1:] != [path] and started.append(path)  # noqa: E731
    ingest.ingest_collection(cfg, "docs", progress=progress, **env["kwargs"])
    assert [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in started] == ["b_small.md", "a_large.md"]


def test_ingest_with_child_processes(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(4):
        (docs / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    (docs / "empty.md").write_text("  ", encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_ingest_processes=2)
    embed = FakeEmbeddingServer()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    result = ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport)
    assert (result.added, result.path_only, result.failed) == (5, 1, [])
    assert ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport).unchanged == 5


# --- Office の使い回し --------------------------------------------------------------------


class _FakeApp:
    def __init__(self, log, fail_open=False):
        self.log, self.fail_open = log, fail_open
        self.Documents = self

    def Open(self, *args, **kwargs):
        if self.fail_open:
            raise RuntimeError("RPC server is unavailable")
        self.log.append("open")
        return types.SimpleNamespace(SaveAs2=lambda path, fmt: self.log.append("save"), Close=lambda **kw: self.log.append("close"))

    def Quit(self):
        self.log.append("quit")


@pytest.fixture
def fake_com(monkeypatch):
    """pythoncom・win32com.client の偽物（DispatchEx は作ったアプリを記録する）。"""
    log: list[str] = []
    apps: list[_FakeApp] = []
    pythoncom = types.SimpleNamespace(CoInitialize=lambda: log.append("init"), CoUninitialize=lambda: log.append("uninit"))

    def dispatch(prog_id):
        app = _FakeApp(log)
        apps.append(app)
        return app

    client = types.SimpleNamespace(DispatchEx=dispatch)
    win32com = types.ModuleType("win32com")
    win32com.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(render, "_office", threading.local())
    return log, apps


def test_office_app_is_reused_while_kept(fake_com, tmp_path):
    log, apps = fake_com
    render.keep_office_apps()
    for _ in range(3):
        render._export_office_to_pdf(tmp_path / "a.docx", tmp_path)
    assert len(apps) == 1 and log.count("open") == 3 and "quit" not in log
    render.close_office_apps()
    assert log[-2:] == ["quit", "uninit"]


def test_office_app_is_closed_each_time_without_keep(fake_com, tmp_path):
    log, apps = fake_com
    render._export_office_to_pdf(tmp_path / "a.docx", tmp_path)
    render._export_office_to_pdf(tmp_path / "a.docx", tmp_path)
    assert len(apps) == 2 and log.count("quit") == 2 and log.count("uninit") == 2


def test_broken_reused_office_app_is_restarted_once(fake_com, tmp_path):
    log, apps = fake_com
    render.keep_office_apps()
    render._export_office_to_pdf(tmp_path / "a.docx", tmp_path)
    apps[0].fail_open = True  # 利用者が Word を閉じた等
    render._export_office_to_pdf(tmp_path / "a.docx", tmp_path)
    assert len(apps) == 2 and log.count("open") == 2
    render.close_office_apps()


# --- 走査（並列・ファイル数の上限） ----------------------------------------------------------


def _make_tree(root, count):
    for i in range(count):
        sub = root / f"dir{i % 4}" / f"sub{i % 3}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"f{i:03d}.md").write_text(f"文書{i}", encoding="utf-8")


def test_parallel_scan_finds_same_files_as_sequential(tmp_path):
    _make_tree(tmp_path, 30)
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.md").write_text("x", encoding="utf-8")
    sequential = ingest.scan_source_files(tmp_path, True, (".md",), workers=1)
    parallel = ingest.scan_source_files(tmp_path, True, (".md",), workers=8)
    assert [p for p, _ in parallel.files] == [p for p, _ in sequential.files]
    assert parallel.total == sequential.total == 30 and parallel.over_limit == 0


def test_scan_limit_prefers_known_files_then_path_order(tmp_path):
    _make_tree(tmp_path, 20)
    everything = [p for p, _ in ingest.scan_source_files(tmp_path, True, (".md",)).files]
    known = {str(everything[-1]), str(everything[-2])}
    result = ingest.scan_source_files(tmp_path, True, (".md",), workers=4, max_files=5, known=known)
    assert (result.total, result.over_limit, result.found_known) == (20, 15, known)
    assert [p for p, _ in result.files] == sorted(everything[:3] + everything[-2:], key=lambda p: str(p).lower())


def test_ingest_over_limit_takes_files_within_limit_and_keeps_the_rest(env):
    _make_tree(env["docs"], 6)
    cfg = _config(env, vector_db_max_files_per_collection=4)
    first = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert (first.added, first.over_limit, first.found_files, first.max_files) == (4, 2, 6, 4)
    assert first.to_dict()["over_limit"] == 2
    # 2回目も同じ4件を選び（入れ替わらない）、上限を超えて取り込まなかったファイルを削除扱いにしない
    second = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert (second.added, second.unchanged, second.removed, second.over_limit) == (0, 4, 0, 2)
    # 取り込み済みのファイルが消えたら削除し、空いた枠に新しいファイルを入れる
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        victim = sorted(catalog.files("docs"))[0]
    import os as _os

    _os.remove(victim)
    third = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert (third.removed, third.added, third.unchanged, third.over_limit) == (1, 1, 3, 1)
    # 上限を上げれば残りも取り込む
    raised = _config(env, vector_db_max_files_per_collection=100)
    assert ingest.ingest_collection(raised, "docs", **env["kwargs"]).added == 1


# --- 並列数の枠の共有（IngestSlots） -------------------------------------------------------


def test_shared_slots_limit_concurrency_across_collections(env):
    """同じ IngestSlots を渡した取り込み同士は、埋め込みのリクエストを合計で並列数までしか送らない。"""
    other = env["docs"].parent / "docs2"
    other.mkdir()
    with vcat.open_catalog(env["cfg"].vector_db_dir) as catalog:
        catalog.create("docs2", str(other))
    for folder in (env["docs"], other):
        text = "\n\n".join(f"第{i}条 項目{i}の基準は{i}mmとする。" + "あ" * 150 for i in range(20))
        (folder / "big.md").write_text(text, encoding="utf-8")
    transport, state = _slow(env["embed"])
    # コレクションごとの設定（ingest_workers=3）ではなく、共有の枠（2）に従う。
    cfg = _config(env, vector_db_ingest_workers=3, vector_db_embedding_batch_size=4)
    slots = ingest.IngestSlots(2)
    results = {}

    def run(name):
        results[name] = ingest.ingest_collection(
            cfg, name, slots=slots, instance="inst1", **{**env["kwargs"], "embedding_transport": transport}
        )

    threads = [threading.Thread(target=run, args=(name,)) for name in ("docs", "docs2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results["docs"].added == 1 and results["docs2"].added == 1
    assert 1 < state["peak"] <= 2
    snapshot = slots.snapshot()
    assert snapshot["load"] == [None, None] and snapshot["embed"] == [None, None] and snapshot["waiting"] == []


def test_slots_prefer_the_earlier_ingestion():
    """枠が空いたら、先に始めた取り込みが空きを待っている間は後の取り込みに渡さない。"""
    slots = ingest.IngestSlots(1)
    first, second = slots.join("inst1", "a"), slots.join("inst1", "b")

    def attempt(user, detail):
        slots.begin_round(user)
        index = slots.try_acquire(user, "embed", detail)
        slots.end_round(user)
        return index

    held = attempt(first, "a1")
    assert held == 0
    assert attempt(first, "a2") is None and first.hungry["embed"]
    assert attempt(second, "b1") is None
    assert slots.snapshot()["embed"] == [{"instance": "inst1", "collection": "a", "detail": "a1"}]
    assert slots.snapshot()["waiting"] == [{"instance": "inst1", "collection": "b"}]
    slots.release("embed", held)
    assert first.wakeup.done() and second.wakeup.done()
    # 後の取り込みが先に試しても、先の取り込みが空きを待っているので取れない。
    assert attempt(second, "b1") is None
    assert attempt(first, "a2") == 0
    # 先の取り込みが空きを待たなくなれば（終わる等）、後の取り込みが使える。
    slots.release("embed", 0)
    slots.leave(first)
    assert attempt(second, "b1") == 0
    # 並列数を増やすと、空きを待っていた取り込みを起こす。
    assert attempt(second, "b2") is None
    slots.resize(2, 0)
    assert second.wakeup.done() and attempt(second, "b2") == 1


def test_embed_requests_are_limited_separately_from_files():
    """埋め込みのリクエストの枠は ingest_embed_requests（0 なら ingest_workers）で、準備するファイルの枠とは別。"""
    slots = ingest.IngestSlots(2, 0, 5)
    assert (slots.capacity("load"), slots.capacity("embed")) == (2, 5)
    assert len(slots.snapshot()["embed"]) == 5 and slots.snapshot()["requests"] == 5
    slots.resize(3, 4, 0)
    assert (slots.capacity("load"), slots.capacity("embed")) == (4, 3)


def test_snapshot_lists_files_being_embedded(env):
    """snapshot() の files は、埋め込み中のファイルごとに件数・送信中のリクエスト数・最後に進んでからの秒数を出す。

    ingest_workers=1 でも、1ファイルのバッチを ingest_embed_requests 個まで同時に送る。
    """
    text = "\n\n".join(f"第{i}条 項目{i}の基準は{i}mmとする。" + "あ" * 150 for i in range(30))
    (env["docs"] / "big.md").write_text(text, encoding="utf-8")
    cfg = _config(env, vector_db_ingest_workers=1, vector_db_embedding_batch_size=4)
    slots = ingest.IngestSlots(1, 0, 4)
    seen: list[dict] = []
    transport, state = _slow(env["embed"])
    slow_handler = transport.handler

    def handler(request):
        seen.extend(slots.snapshot()["files"])
        return slow_handler(request)

    result = ingest.ingest_collection(
        cfg, "docs", slots=slots, instance="inst1", **{**env["kwargs"], "embedding_transport": httpx.MockTransport(handler)}
    )
    assert result.added == 1
    assert 1 < state["peak"] <= 4
    big = [f for f in seen if f["detail"].endswith("big.md")]
    assert big and all(f["instance"] == "inst1" and f["collection"] == "docs" for f in big)
    assert max(f["requests"] for f in big) > 1 and big[0]["total"] >= 20
    assert any(f["done"] > 0 for f in big) and all(0 <= f["done"] < f["total"] and f["idle_seconds"] >= 0 for f in big)
    assert slots.snapshot()["files"] == []
