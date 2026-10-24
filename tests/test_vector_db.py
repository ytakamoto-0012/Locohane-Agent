"""src/vector_db（抽出・チャンク分割・埋め込み・保存先・取り込み・検索）のテスト。"""

from __future__ import annotations

import os

import httpx
import pytest

from _vector_db_fakes import DIM, FakeEmbeddingServer, FakeQdrant, fake_vector, make_config
from src.config import LLMEndpoint
from src.vector_db import catalog as vcat
from src.vector_db import embeddings, ingest, search
from src.vector_db.chunking import split_text
from src.vector_db.embeddings import EmbeddingError, EmbeddingStopped, embed_images, embed_texts, open_client, shared_client
from src.vector_db.extract import ExtractError, extract_segments
from src.vector_db.qdrant_store import QDRANT_PRESETS, QdrantStore
from src.vector_db.sqlite_vec_store import SqliteVecStore
from src.vector_db.store import ChunkRecord, StoreError


# --- チャンク分割 ---------------------------------------------------------------


def test_split_text_keeps_short_paragraphs_together():
    assert split_text("段落1\n\n段落2", chunk_size=100, overlap=10) == ["段落1\n段落2"]


def test_split_text_respects_size_and_overlap():
    text = "\n\n".join(f"{i:02d}" + "あ" * 40 for i in range(10))
    chunks = split_text(text, chunk_size=100, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    # 2つ目以降は直前のチャンクの末尾を重ねている
    assert chunks[1].startswith(chunks[0][-20:])


def test_split_text_splits_long_line_by_characters():
    chunks = split_text("x" * 250, chunk_size=100, overlap=0)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_split_text_rejects_overlap_not_smaller_than_size():
    with pytest.raises(ValueError):
        split_text("abc", chunk_size=10, overlap=10)


# --- テキスト抽出 ---------------------------------------------------------------


def test_extract_text_falls_back_to_cp932(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("溶接の基準".encode("cp932"))
    assert extract_segments(path)[0].text == "溶接の基準"


def test_extract_office_and_pdf_formats(tmp_path):
    import docx
    import openpyxl
    from pptx import Presentation
    from reportlab.pdfgen import canvas

    d = docx.Document()
    d.add_paragraph("docx body")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "cell1"
    d.save(tmp_path / "a.docx")
    assert "docx body" in extract_segments(tmp_path / "a.docx")[0].text
    assert "cell1" in extract_segments(tmp_path / "a.docx")[0].text

    wb = openpyxl.Workbook()
    wb.active.title = "集計"
    wb.active.append(["品番", 10])
    wb.create_sheet("空")
    wb.save(tmp_path / "a.xlsx")
    segments = extract_segments(tmp_path / "a.xlsx")
    assert [(s.locator, s.text) for s in segments] == [("Sheet: 集計", "品番\t10")]

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "slide title"
    slide.notes_slide.notes_text_frame.text = "speaker note"
    prs.save(tmp_path / "a.pptx")
    segments = extract_segments(tmp_path / "a.pptx")
    assert segments[0].locator == "Slide 1"
    assert "slide title" in segments[0].text and "speaker note" in segments[0].text

    c = canvas.Canvas(str(tmp_path / "a.pdf"))
    c.drawString(72, 720, "page one text")
    c.showPage()
    c.drawString(72, 720, "page two text")
    c.save()
    segments = extract_segments(tmp_path / "a.pdf")
    assert [s.locator for s in segments] == ["p.1", "p.2"]
    assert "page two text" in segments[1].text


def test_extract_rejects_unsupported_and_broken_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x")
    with pytest.raises(ExtractError):
        extract_segments(tmp_path / "a.bin")
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    with pytest.raises(ExtractError):
        extract_segments(tmp_path / "broken.docx")


# --- 埋め込み ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_embedding_retry_wait(monkeypatch):
    """埋め込みの再試行の待ちを実際には眠らずに記録し、接続先の選び方の状態をテストごとに初期化する。"""
    sleeps: list[float] = []
    monkeypatch.setattr(embeddings, "_sleep", sleeps.append)
    monkeypatch.setattr(embeddings, "_round_robin_counters", {})
    monkeypatch.setattr(embeddings, "_failed_until", {})
    return sleeps


def _two_endpoints(*hosts: str) -> tuple[LLMEndpoint, ...]:
    return tuple(LLMEndpoint(base_url=f"http://{h}/v1", api_key="k", model="fake-embed") for h in hosts)


def _flaky_transport(server: FakeEmbeddingServer, failures: int, status: int = 503) -> tuple[httpx.MockTransport, list[str]]:
    """最初の failures 回は status を返し、その後は server に任せるトランスポート（送った先の URL を記録する）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= failures:
            return httpx.Response(status, text="busy")
        return server.handler(request)

    return httpx.MockTransport(handler), calls


def test_embed_texts_batches_and_adds_prefix(tmp_path):
    server = FakeEmbeddingServer()
    cfg = make_config(tmp_path, vector_db_embedding_batch_size=2, vector_db_query_prefix="query: ", vector_db_document_prefix="passage: ")
    vectors = embed_texts(cfg, ["a", "b", "c"], kind="document", transport=server.transport)
    assert len(vectors) == 3 and len(vectors[0]) == DIM
    assert [r["input"] for r in server.requests] == [["passage: a", "passage: b"], ["passage: c"]]
    assert server.requests[0]["model"] == "fake-embed"
    embed_texts(cfg, ["q"], kind="query", transport=server.transport)
    assert server.requests[-1]["input"] == ["query: q"]


def test_embed_texts_fails_over_to_next_endpoint_on_connection_error(tmp_path, _no_embedding_retry_wait):
    server = FakeEmbeddingServer(fail_hosts=("down",))
    endpoints = _two_endpoints("down", "up")
    cfg = make_config(tmp_path, vector_db_embedding_endpoints=endpoints)
    embed_texts(cfg, ["a"], kind="query", transport=server.transport)
    assert server.requests[0]["url"] == "http://up/v1/embeddings"
    assert _no_embedding_retry_wait == []

    cfg = make_config(tmp_path, vector_db_embedding_endpoints=endpoints[:1], vector_db_embedding_max_retries=2)
    with pytest.raises(EmbeddingError, match="retried 2 times"):
        embed_texts(cfg, ["a"], kind="document", transport=server.transport)


def test_embed_texts_fails_over_to_next_endpoint_on_http_error(tmp_path, _no_embedding_retry_wait):
    """HTTP 200 以外が返ったら、同じリクエストを次の接続先へ送り直す（待たない）。"""
    server = FakeEmbeddingServer()
    transport, calls = _flaky_transport(server, failures=1, status=500)
    cfg = make_config(tmp_path, vector_db_embedding_endpoints=_two_endpoints("a", "b"))
    assert len(embed_texts(cfg, ["x"], kind="query", transport=transport)) == 1
    assert calls == ["http://a/v1/embeddings", "http://b/v1/embeddings"]
    assert _no_embedding_retry_wait == []


def test_embed_texts_retries_after_interval_until_success(tmp_path, _no_embedding_retry_wait):
    """全接続先でエラーなら embedding_retry_interval_seconds 待って全接続先を試し直す。"""
    server = FakeEmbeddingServer()
    transport, calls = _flaky_transport(server, failures=4)
    cfg = make_config(
        tmp_path,
        vector_db_embedding_endpoints=_two_endpoints("a", "b"),
        vector_db_embedding_max_retries=2,
        vector_db_embedding_retry_interval_seconds=1.0,
    )
    embed_texts(cfg, ["x"], kind="document", transport=transport)
    # 1巡目（a, b）・再試行1（a, b）が失敗し、再試行2の a で成功する
    assert len(calls) == 5
    assert sum(_no_embedding_retry_wait) == pytest.approx(2.0)


def test_embed_texts_gives_up_after_max_retries(tmp_path, _no_embedding_retry_wait):
    transport, calls = _flaky_transport(FakeEmbeddingServer(), failures=10**6)
    cfg = make_config(tmp_path, vector_db_embedding_max_retries=3, vector_db_embedding_retry_interval_seconds=2.0)
    with pytest.raises(EmbeddingError, match="retried 3 times.*HTTP 503"):
        embed_texts(cfg, ["x"], kind="document", transport=transport)
    assert len(calls) == 4
    assert sum(_no_embedding_retry_wait) == pytest.approx(6.0)


def test_embed_texts_retries_without_limit_when_max_retries_is_zero(tmp_path, _no_embedding_retry_wait):
    transport, calls = _flaky_transport(FakeEmbeddingServer(), failures=25)
    cfg = make_config(tmp_path, vector_db_embedding_max_retries=0, vector_db_embedding_retry_interval_seconds=0.5)
    embed_texts(cfg, ["x"], kind="document", transport=transport)
    assert len(calls) == 26
    assert sum(_no_embedding_retry_wait) == pytest.approx(12.5)


def test_search_embedding_uses_its_own_bounded_retries(tmp_path, _no_embedding_retry_wait):
    """検索語の埋め込みは embedding_max_retries = 0（無制限）でも search_embedding_max_retries 回でやめる。"""
    cfg = make_config(tmp_path, vector_db_embedding_max_retries=0, vector_db_search_embedding_max_retries=2)
    transport, calls = _flaky_transport(FakeEmbeddingServer(), failures=10**6)
    with pytest.raises(EmbeddingError, match="retried 2 times"):
        embed_texts(cfg, ["x"], kind="query", transport=transport)
    assert len(calls) == 3
    assert sum(_no_embedding_retry_wait) == pytest.approx(20.0)

    # 0 は「試し直さない」
    _no_embedding_retry_wait.clear()
    cfg = make_config(tmp_path, vector_db_search_embedding_max_retries=0)
    transport, calls = _flaky_transport(FakeEmbeddingServer(), failures=10**6)
    with pytest.raises(EmbeddingError):
        embed_texts(cfg, ["x"], kind="query", transport=transport)
    assert len(calls) == 1 and _no_embedding_retry_wait == []


def test_embed_texts_does_not_retry_client_errors(tmp_path, _no_embedding_retry_wait):
    """408・429 以外の 4xx は待っても直らないため、全接続先を1巡したら試し直さない。"""
    transport, calls = _flaky_transport(FakeEmbeddingServer(), failures=10**6, status=400)
    cfg = make_config(tmp_path, vector_db_embedding_endpoints=_two_endpoints("a", "b"), vector_db_embedding_max_retries=0)
    with pytest.raises(EmbeddingError, match="HTTP 400"):
        embed_texts(cfg, ["x"], kind="document", transport=transport)
    assert len(calls) == 2 and _no_embedding_retry_wait == []

    transport, calls = _flaky_transport(FakeEmbeddingServer(), failures=2, status=429)
    embed_texts(cfg, ["x"], kind="document", transport=transport)
    assert len(calls) == 3


def test_embed_retry_wait_stops_when_requested(tmp_path):
    transport, calls = _flaky_transport(FakeEmbeddingServer(), failures=10**6)
    cfg = make_config(tmp_path, vector_db_embedding_max_retries=0)
    checks = iter([False, False, True])
    with pytest.raises(EmbeddingStopped):
        embed_images(cfg, [b"png"], transport=transport, should_stop=lambda: next(checks))
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("strategy", "expected_first"),
    [("priority_failover", ["a", "a", "a"]), ("round_robin", ["a", "b", "c"])],
)
def test_embed_texts_routing_strategy(tmp_path, strategy, expected_first):
    server = FakeEmbeddingServer()
    cfg = make_config(tmp_path, vector_db_embedding_endpoints=_two_endpoints("a", "b", "c"), vector_db_embedding_routing_strategy=strategy)
    for _ in range(3):
        embed_texts(cfg, ["x"], kind="query", transport=server.transport)
    assert [httpx.URL(r["url"]).host for r in server.requests] == expected_first


def test_embed_texts_random_strategy_uses_all_endpoints(tmp_path, monkeypatch):
    server = FakeEmbeddingServer()
    monkeypatch.setattr(embeddings.random, "sample", lambda seq, k: list(reversed(seq)))
    cfg = make_config(tmp_path, vector_db_embedding_endpoints=_two_endpoints("a", "b"), vector_db_embedding_routing_strategy="random")
    embed_texts(cfg, ["x"], kind="query", transport=server.transport)
    assert server.requests[0]["url"] == "http://b/v1/embeddings"


def test_priority_failover_puts_failed_endpoint_last_for_a_while(tmp_path):
    """priority_failover では、エラーが返った接続先を一定時間後回しにする（毎回タイムアウトを待たない）。"""
    server = FakeEmbeddingServer()
    transport, calls = _flaky_transport(server, failures=1)
    cfg = make_config(tmp_path, vector_db_embedding_endpoints=_two_endpoints("a", "b"))
    embed_texts(cfg, ["x"], kind="query", transport=transport)
    embed_texts(cfg, ["y"], kind="query", transport=transport)
    assert calls == ["http://a/v1/embeddings", "http://b/v1/embeddings", "http://b/v1/embeddings"]


def test_embed_texts_rejects_token_level_embeddings(tmp_path):
    server = FakeEmbeddingServer(two_dimensional=True)
    with pytest.raises(EmbeddingError, match="pooling"):
        embed_texts(make_config(tmp_path), ["a"], kind="query", transport=server.transport)


def test_embed_texts_reports_http_error(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="model not loaded"))
    with pytest.raises(EmbeddingError, match="HTTP 500.*model not loaded"):
        embed_texts(make_config(tmp_path), ["a"], kind="query", transport=transport)


# --- 保存先 ----------------------------------------------------------------------


def _exercise_store(store):
    store.ensure_collection("docs", DIM)
    store.ensure_collection("docs", DIM)  # 2回目は何もしない
    store.replace_source("docs", "/a.md", [ChunkRecord("p.1", "溶接の基準"), ChunkRecord("p.2", "検査の方法")], [fake_vector("溶接の基準"), fake_vector("検査の方法")])
    store.replace_source("docs", "/b.md", [ChunkRecord("", "会議室の予約")], [fake_vector("会議室の予約")])
    hits = store.search("docs", fake_vector("溶接の基準"), 2)
    assert hits[0].source_path == "/a.md" and hits[0].locator == "p.1" and hits[0].text == "溶接の基準"
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    # 入れ替えると古いチャンクは残らない
    store.replace_source("docs", "/a.md", [ChunkRecord("", "差し替え後")], [fake_vector("差し替え後")])
    texts = {h.text for h in store.search("docs", fake_vector("x"), 10)}
    assert texts == {"差し替え後", "会議室の予約"}
    store.delete_source("docs", "/b.md")
    assert {h.source_path for h in store.search("docs", fake_vector("x"), 10)} == {"/a.md"}
    with pytest.raises(StoreError):
        store.ensure_collection("docs", DIM + 1)
    assert store.search("missing", fake_vector("x"), 3) == []
    store.drop_collection("docs")
    store.drop_collection("docs")
    assert store.search("docs", fake_vector("x"), 3) == []


@pytest.mark.parametrize("distance", ["Cosine", "Euclid", "Manhattan"])
def test_sqlite_vec_store(tmp_path, distance):
    store = SqliteVecStore(tmp_path, distance=distance)
    try:
        # 完全一致の score が 1.0 になる（Euclid・Manhattan は 1 / (1 + 距離) に換算）
        _exercise_store(store)
    finally:
        store.close()


def test_sqlite_vec_store_rejects_dot(tmp_path):
    with pytest.raises(StoreError, match="Dot"):
        SqliteVecStore(tmp_path, distance="Dot")


def test_sqlite_vec_store_rejects_distance_mismatch(tmp_path):
    store = SqliteVecStore(tmp_path, distance="Euclid")
    try:
        store.ensure_collection("docs", DIM)
        store.replace_source("docs", "/a.md", [ChunkRecord("", "溶接の基準"), ChunkRecord("", "会議室の予約")], [fake_vector("溶接の基準"), fake_vector("会議室の予約")])
    finally:
        store.close()
    other = SqliteVecStore(tmp_path, distance="Cosine")
    try:
        with pytest.raises(StoreError, match="distance.*Rebuild"):
            other.ensure_collection("docs", DIM)
        # 検索は作ったときの距離関数（Euclid）で換算する
        hits = other.search("docs", fake_vector("溶接の基準"), 2)
        assert [h.text for h in hits] == ["溶接の基準", "会議室の予約"]
        assert 0.0 < hits[1].score < hits[0].score == pytest.approx(1.0)
        # 再構築（消して作り直す）すれば新しい距離関数で作れる
        other.drop_collection("docs")
        other.ensure_collection("docs", DIM)
    finally:
        other.close()


def test_sqlite_vec_store_migrates_db_without_distance_column(tmp_path):
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(str(tmp_path / "vectors.sqlite"))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.executescript(
        """
        CREATE TABLE vec_collections (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, dim INTEGER NOT NULL);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER NOT NULL, source_path TEXT NOT NULL,
                             locator TEXT NOT NULL, text TEXT NOT NULL);
        """
    )
    conn.execute("INSERT INTO vec_collections (name, dim) VALUES ('docs', ?)", (DIM,))
    conn.execute(f"CREATE VIRTUAL TABLE vec_c1 USING vec0(embedding float[{DIM}] distance_metric=cosine)")
    conn.commit()
    conn.close()
    # 旧版の DB はコサイン距離で作られているため、そのまま Cosine として使える
    store = SqliteVecStore(tmp_path)
    try:
        store.ensure_collection("docs", DIM)
        store.replace_source("docs", "/a.md", [ChunkRecord("", "溶接の基準")], [fake_vector("溶接の基準")])
        assert store.search("docs", fake_vector("溶接の基準"), 1)[0].score == pytest.approx(1.0, abs=1e-5)
    finally:
        store.close()


@pytest.mark.parametrize("distance", ["Cosine", "Euclid", "Dot", "Manhattan"])
def test_qdrant_store_uses_prefixed_collection_names(distance):
    fake = FakeQdrant()
    store = QdrantStore("http://qdrant:6333/", prefix="locohane_x_", api_key="secret", distance=distance, transport=fake.transport)
    try:
        # 完全一致の score が 1.0 になる（Euclid・Manhattan は 1 / (1 + 距離) に換算）
        _exercise_store(store)
    finally:
        store.close()
    assert all(path.startswith("/collections/locohane_x_") for _, path in fake.calls)


def test_qdrant_store_creates_collection_with_distance_and_rejects_mismatch():
    fake = FakeQdrant()
    store = QdrantStore("http://qdrant:6333", prefix="p_", distance="Euclid", transport=fake.transport)
    try:
        store.ensure_collection("docs", DIM)
        assert fake.collections["p_docs"]["distance"] == "Euclid"
        store.replace_source("docs", "/a.md", [ChunkRecord("", "溶接の基準"), ChunkRecord("", "会議室の予約")], [fake_vector("溶接の基準"), fake_vector("会議室の予約")])
        hits = store.search("docs", fake_vector("溶接の基準"), 2)
        # 距離が小さい順（換算後の score が大きい順）
        assert [h.text for h in hits] == ["溶接の基準", "会議室の予約"]
        assert 0.0 < hits[1].score < hits[0].score == pytest.approx(1.0)
    finally:
        store.close()
    # 距離関数を変えた設定では、既存のコレクションへの取り込みを拒否し再構築を求める
    other = QdrantStore("http://qdrant:6333", prefix="p_", distance="Cosine", transport=fake.transport)
    try:
        with pytest.raises(StoreError, match="distance.*Rebuild"):
            other.ensure_collection("docs", DIM)
        # 検索は作ったときの距離関数（Euclid）で換算する
        assert other.search("docs", fake_vector("溶接の基準"), 1)[0].score == pytest.approx(1.0)
    finally:
        other.close()


def test_qdrant_store_without_preset_keeps_qdrant_defaults():
    fake = FakeQdrant()
    store = QdrantStore("http://qdrant:6333", prefix="p_", transport=fake.transport)
    try:
        store.ensure_collection("docs", DIM)
        store.apply_preset("docs")
        store.search("docs", fake_vector("溶接"), 1)
    finally:
        store.close()
    config = fake.collections["p_docs"]["config"]
    assert config["quantization_config"] is None and "on_disk" not in config["params"]["vectors"]
    assert fake.patches == [] and "params" not in fake.queries[0]


@pytest.mark.parametrize("preset", ["1m", "2m", "3m", "4m"])
def test_qdrant_store_creates_collection_with_preset(preset):
    fake = FakeQdrant()
    store = QdrantStore("http://qdrant:6333", prefix="p_", preset=preset, transport=fake.transport)
    expected = QDRANT_PRESETS[preset]
    try:
        _exercise_store(store)
        # 作ったときに設定したので、もう一度 ensure・apply しても PATCH しない
        store.ensure_collection("docs", DIM)
        store.apply_preset("docs")
    finally:
        store.close()
    config = fake.collections["p_docs"]["config"]
    assert config["hnsw_config"]["m"] == expected.m and config["hnsw_config"]["ef_construct"] == expected.ef_construct
    assert config["params"]["vectors"]["on_disk"] is expected.vectors_on_disk and config["params"]["on_disk_payload"] is True
    assert config["optimizer_config"]["default_segment_number"] == expected.segments
    assert config["quantization_config"]["scalar"]["type"] == "int8"
    assert fake.patches == []
    assert fake.queries[-1]["params"] == {"hnsw_ef": expected.hnsw_ef, "quantization": {"rescore": True, "oversampling": expected.oversampling}}


def test_qdrant_presets_grow_with_scale_and_match_config_choices():
    from src.config import VECTOR_DB_QDRANT_PRESETS

    assert tuple(QDRANT_PRESETS) == VECTOR_DB_QDRANT_PRESETS[1:] and VECTOR_DB_QDRANT_PRESETS[0] == "none"
    presets = list(QDRANT_PRESETS.values())
    for smaller, larger in zip(presets, presets[1:]):
        assert larger.m >= smaller.m and larger.ef_construct >= smaller.ef_construct and larger.hnsw_ef > smaller.hnsw_ef


def test_qdrant_store_applies_preset_to_existing_collection():
    fake = FakeQdrant()
    old = QdrantStore("http://qdrant:6333", prefix="p_", transport=fake.transport)
    try:
        old.ensure_collection("docs", DIM)
        old.replace_source("docs", "/a.md", [ChunkRecord("", "溶接の基準")], [fake_vector("溶接の基準")])
    finally:
        old.close()
    store = QdrantStore("http://qdrant:6333", prefix="p_", preset="3m", transport=fake.transport)
    try:
        # 無いコレクションは何もしない
        store.apply_preset("missing")
        store.apply_preset("docs")
        store.apply_preset("docs")
        assert store.search("docs", fake_vector("溶接の基準"), 1)[0].text == "溶接の基準"
    finally:
        store.close()
    # 設定が違うときだけ1回 PATCH し、ベクターは消さない（埋め込み直し不要）
    assert [name for name, _ in fake.patches] == ["p_docs"]
    body = fake.patches[0][1]
    assert body["vectors"] == {"": {"on_disk": True}} and body["hnsw_config"]["m"] == QDRANT_PRESETS["3m"].m
    assert body["params"] == {"on_disk_payload": True}
    assert len(fake.collections["p_docs"]["points"]) == 1


def test_ingest_applies_qdrant_preset_without_changed_files(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("溶接の基準は JIS に従う。", encoding="utf-8")
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    cfg = make_config(tmp_path, vector_db_backend="qdrant")
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport, store_transport=qdrant.transport)
    assert qdrant.patches == []
    tuned = make_config(tmp_path, vector_db_backend="qdrant", vector_db_qdrant_preset="2m")
    result = ingest.ingest_collection(tuned, "docs", embedding_transport=embed.transport, store_transport=qdrant.transport)
    assert result.unchanged == 1
    assert [name for name, _ in qdrant.patches] == ["locohane_test_docs", "locohane_test_docs.path"]


def test_qdrant_store_sends_api_key_and_reports_connection_error():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("api-key")
        raise httpx.ConnectError("refused", request=request)

    store = QdrantStore("http://qdrant:6333", prefix="p_", api_key="secret", transport=httpx.MockTransport(handler))
    with pytest.raises(StoreError, match="Cannot connect"):
        store.search("docs", [0.1], 1)
    assert seen["key"] == "secret"


# --- カタログ --------------------------------------------------------------------


def test_catalog_validates_names_and_duplicates(tmp_path):
    with vcat.open_catalog(tmp_path) as catalog:
        with pytest.raises(vcat.CatalogError):
            catalog.create("日本語", "C:/x")
        catalog.create("docs", "C:/x", "説明")
        with pytest.raises(vcat.CatalogError):
            catalog.create("docs", "C:/y")
        updated = catalog.update("docs", description="新しい説明", recursive=False)
        assert (updated.description, updated.recursive) == ("新しい説明", False)
        catalog.delete("docs")
        assert catalog.list_collections() == []


# --- 取り込みと検索 ---------------------------------------------------------------


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "weld.md").write_text("溶接ビードの幅は5mm以上とする。", encoding="utf-8")
    (docs / "sub" / "room.txt").write_text("会議室の予約はポータルから行う。", encoding="utf-8")
    (docs / "~$lock.docx").write_bytes(b"office lock file")
    (docs / "image.xyz").write_bytes(b"unsupported")
    cfg = make_config(tmp_path, vector_db_backend=request.param)
    embed = FakeEmbeddingServer()
    qdrant = FakeQdrant()
    kwargs = {"embedding_transport": embed.transport, "store_transport": qdrant.transport}
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs), "テスト文書")
    return {"cfg": cfg, "docs": docs, "kwargs": kwargs, "embed": embed, "qdrant": qdrant}


def test_ingest_is_incremental(env):
    cfg, docs, kwargs = env["cfg"], env["docs"], env["kwargs"]
    progress = []
    first = ingest.ingest_collection(cfg, "docs", progress=lambda *a: progress.append(a), **kwargs)
    assert (first.added, first.unchanged, first.failed) == (2, 0, [])
    assert progress[-1] == (2, 2, "")
    requests_after_first = len(env["embed"].requests)

    second = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert (second.added, second.updated, second.unchanged) == (0, 0, 2)
    assert len(env["embed"].requests) == requests_after_first  # 変更が無ければ埋め込み直さない

    # 更新日時だけ変わって内容が同じなら埋め込み直さない
    weld = docs / "weld.md"
    os.utime(weld, (weld.stat().st_atime, weld.stat().st_mtime + 10))
    assert ingest.ingest_collection(cfg, "docs", **kwargs).unchanged == 2
    assert len(env["embed"].requests) == requests_after_first

    weld.write_text("溶接ビードの幅は6mm以上に改訂した。", encoding="utf-8")
    (docs / "sub" / "room.txt").unlink()
    (docs / "empty.md").write_text("   ", encoding="utf-8")
    third = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert (third.updated, third.removed) == (1, 1)
    # 本文を取り出せないファイルも、失敗にせずファイルパスだけを登録する（ファイル名で探せるように）。
    assert (third.added, third.path_only, third.failed) == (1, 1, [])

    result = search.search(cfg, "溶接ビードの幅", **kwargs)
    assert [h.text for h in result.hits] == ["溶接ビードの幅は6mm以上に改訂した。"]
    [collection] = search.list_collections(cfg)
    assert (collection.file_count, collection.chunk_count, collection.model, collection.dim) == (2, 1, "fake-embed", DIM)
    assert str(docs / "empty.md") in {h.source_path for h in search.search_paths(cfg, "empty", **kwargs).hits}


def test_ingest_reuses_one_embedding_client(env, monkeypatch):
    # ファイルごとに接続し直すと、Windows の localhost では毎回約2秒かかるため、取り込み全体で1つを使い回す。
    cfg, kwargs = env["cfg"], env["kwargs"]
    opened, closed = [], []
    real_open = ingest.open_client

    def counting_open(*args, **kw):
        client = real_open(*args, **kw)
        opened.append(client)
        real_close = client.close
        client.close = lambda: (closed.append(client), real_close())[1]
        return client

    monkeypatch.setattr(ingest, "open_client", counting_open)
    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert result.added == 2 and len(env["embed"].requests) == 2
    assert len(opened) == 1 and closed == opened


def test_embed_texts_does_not_close_given_client(tmp_path):
    server = FakeEmbeddingServer()
    cfg = make_config(tmp_path)
    with open_client(cfg, transport=server.transport) as client:
        embed_texts(cfg, ["a"], kind="document", client=client)
        embed_texts(cfg, ["b"], kind="document", client=client)
        assert not client.is_closed
    assert len(server.requests) == 2


def test_shared_client_is_reused_until_closed(tmp_path):
    cfg = make_config(tmp_path)
    client = shared_client(cfg)
    assert shared_client(cfg) is client
    client.close()
    reopened = shared_client(cfg)
    assert reopened is not client and not reopened.is_closed


def test_qdrant_shared_store_keeps_client_open(monkeypatch):
    from src.vector_db import qdrant_store

    monkeypatch.setattr(qdrant_store, "_shared_clients", {})
    first = QdrantStore("http://qdrant:6333/", prefix="p_", shared=True)
    first.close()
    second = QdrantStore("http://qdrant:6333", prefix="p_", shared=True)
    assert second._client is first._client and not first._client.is_closed
    own = QdrantStore("http://qdrant:6333", prefix="p_")
    own.close()
    assert own._client.is_closed and not first._client.is_closed


def test_ingest_skips_files_over_size_limit_and_non_recursive(env):
    cfg = make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, vector_db_max_file_mb=0.00001)
    result = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert len(result.failed) == 2 and "limit" in result.failed[0][1]
    assert result.too_large == 2 and result.to_dict()["too_large"] == 2
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.update("docs", recursive=False)
    files = ingest.list_source_files(env["docs"], False, cfg.vector_db_include_extensions)
    assert [p.name for p in files] == ["weld.md"]


def test_ingest_requires_rebuild_when_embedding_model_changes(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    other = make_config(
        cfg.vector_db_dir.parent,
        vector_db_backend=cfg.vector_db_backend,
        vector_db_embedding_endpoints=(LLMEndpoint(base_url="http://embed/v1", api_key="k", model="other-embed"),),
    )
    with pytest.raises(ingest.IngestError, match="rebuild"):
        ingest.ingest_collection(other, "docs", **kwargs)
    # 検索も対象外になり、理由が分かる
    with pytest.raises(search.SearchError, match="other-embed"):
        search.search(other, "溶接", **kwargs)
    rebuilt = ingest.ingest_collection(other, "docs", rebuild=True, **kwargs)
    assert rebuilt.added == 2
    assert search.list_collections(other)[0].model == "other-embed"


def test_ingest_requires_rebuild_when_document_prefix_changes(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    assert search.list_collections(cfg)[0].document_prefix == cfg.vector_db_document_prefix
    other = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_document_prefix="passage: ")
    with pytest.raises(ingest.IngestError, match="document_prefix"):
        ingest.ingest_collection(other, "docs", **kwargs)
    with pytest.raises(search.SearchError, match="document_prefix"):
        search.search(other, "溶接", **kwargs)
    # query_prefix だけの違いは再構築不要
    query_only = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_query_prefix="query: ")
    assert search.search(query_only, "溶接", **kwargs).hits
    ingest.ingest_collection(other, "docs", rebuild=True, **kwargs)
    assert search.list_collections(other)[0].document_prefix == "passage: "
    assert search.search(other, "溶接", **kwargs).hits


def test_catalog_adds_document_prefix_column_to_old_db(tmp_path):
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / vcat.CATALOG_FILENAME))
    conn.executescript(
        """
        CREATE TABLE collections (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', source_folder TEXT NOT NULL,
            recursive INTEGER NOT NULL DEFAULT 1, model TEXT, dim INTEGER, created_at TEXT NOT NULL, last_ingested_at TEXT);
        INSERT INTO collections (name, source_folder, model, dim, created_at) VALUES ('old', 'C:/docs', 'fake-embed', 8, '2026-01-01');
        """
    )
    conn.close()
    with vcat.open_catalog(tmp_path) as catalog:
        assert catalog.get("old").document_prefix is None


def test_ingest_stops_without_deleting_unprocessed_files(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    result = ingest.ingest_collection(cfg, "docs", should_stop=lambda: True, **kwargs)
    assert result.removed == 0
    assert search.list_collections(cfg)[0].file_count == 2


def test_ingest_runs_files_in_parallel_with_ingest_workers(env):
    """ingest_workers > 1 なら複数ファイルの埋め込みを同時に行い、結果は1つずつ処理したときと同じになる。"""
    import threading
    import time

    cfg, docs = env["cfg"], env["docs"]
    for i in range(6):
        (docs / f"extra{i}.md").write_text(f"追加の文書{i}。手順{i}を守る。", encoding="utf-8")
    (docs / "empty.md").write_text("   ", encoding="utf-8")
    embed = env["embed"]
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def slow_handler(request):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(0.05)
            return embed.handler(request)
        finally:
            with lock:
                state["active"] -= 1

    kwargs = {**env["kwargs"], "embedding_transport": httpx.MockTransport(slow_handler)}
    parallel = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_ingest_workers=3)
    progress = []
    result = ingest.ingest_collection(parallel, "docs", progress=lambda *a: progress.append(a), **kwargs)
    assert (result.added, result.unchanged, result.path_only, result.failed) == (9, 0, 1, [])
    assert 1 < state["peak"] <= 3
    assert progress[-1] == (9, 9, "")
    assert search.list_collections(parallel)[0].file_count == 9
    assert [h.text for h in search.search(parallel, "溶接ビードの幅", **kwargs).hits][0] == "溶接ビードの幅は5mm以上とする。"
    # 2回目は差分なし（並列でも記録が正しく残っている）
    again = ingest.ingest_collection(parallel, "docs", **kwargs)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 9)


def test_ingest_in_parallel_stops_on_embedding_error(env):
    cfg = make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, vector_db_ingest_workers=2)
    failing = FakeEmbeddingServer(fail_hosts=("embed",))
    kwargs = {**env["kwargs"], "embedding_transport": failing.transport}
    with pytest.raises(EmbeddingError):
        ingest.ingest_collection(cfg, "docs", **kwargs)
    assert search.list_collections(cfg)[0].file_count == 0


def test_ingest_stop_during_embedding_retry_keeps_file_for_next_run(env):
    """埋め込みの再試行を待っている間に停止したら、そのファイルを記録せずに止め、次回の取り込みで処理する。"""
    cfg = make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, vector_db_embedding_max_retries=0)
    stop = {"requested": False}

    def down(request: httpx.Request) -> httpx.Response:
        stop["requested"] = True
        return httpx.Response(503, text="loading")

    kwargs = {**env["kwargs"], "embedding_transport": httpx.MockTransport(down)}
    result = ingest.ingest_collection(cfg, "docs", should_stop=lambda: stop["requested"], **kwargs)
    assert result.added == 0
    assert search.list_collections(cfg)[0].file_count == 0

    again = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert again.added > 0 and again.removed == 0


def test_ingest_stop_reports_processed_count(env):
    """埋め込みの途中で停止しても、最後の進捗は記録まで終わったファイルの件数を指す。

    以前は停止すると最後の通知を送らず、最後に読み始めたファイルの時点の件数（ここでは0件）のまま残っていた。
    """
    cfg, kwargs = env["cfg"], env["kwargs"]
    progress = []
    result = ingest.ingest_collection(
        cfg, "docs", progress=lambda *a: progress.append(a), should_stop=lambda: len(env["embed"].requests) >= 1, **kwargs
    )
    assert 1 <= result.added < 2
    assert progress[-1] == (result.added, 2, "")


def test_ingest_errors_for_missing_collection_or_folder(env):
    with pytest.raises(ingest.IngestError):
        ingest.ingest_collection(env["cfg"], "missing", **env["kwargs"])
    with vcat.open_catalog(env["cfg"].vector_db_dir) as catalog:
        catalog.update("docs", source_folder=str(env["docs"] / "nothing"))
    with pytest.raises(ingest.IngestError, match="folder"):
        ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"])


def test_search_filters_by_collection_score_and_top_k(env, tmp_path):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("empty", str(tmp_path))
    result = search.search(cfg, "会議室の予約", top_k=1, **kwargs)
    assert [h.source_path.endswith("room.txt") for h in result.hits] == [True]
    assert result.skipped == [("empty", "no documents ingested yet")]
    strict = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_min_score=0.99)
    assert [h.text for h in search.search(strict, "会議室の予約はポータルから行う。", **kwargs).hits] == ["会議室の予約はポータルから行う。"]
    with pytest.raises(search.SearchError):
        search.search(cfg, "x", collection="missing", **kwargs)
    with pytest.raises(search.SearchError):
        search.search(cfg, "  ", **kwargs)


def test_list_collections_without_catalog_does_not_create_it(tmp_path):
    cfg = make_config(tmp_path)
    assert search.list_collections(cfg) == []
    assert not cfg.vector_db_dir.exists()


def test_list_source_files_skips_hidden_files_and_folders(tmp_path):
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / ".secret.md").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "b.md").write_text("x", encoding="utf-8")
    (tmp_path / "sub" / ".cache").mkdir(parents=True)
    (tmp_path / "sub" / ".cache" / "c.md").write_text("x", encoding="utf-8")
    (tmp_path / "sub" / "d.md").write_text("d", encoding="utf-8")
    files = ingest.list_source_files(tmp_path, True, (".md",))
    assert [p.relative_to(tmp_path).as_posix() for p in files] == ["a.md", "sub/d.md"]
