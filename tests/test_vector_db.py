"""src/vector_db（抽出・チャンク分割・埋め込み・保存先・取り込み・検索）のテスト。"""

from __future__ import annotations

import os

import httpx
import pytest

from _vector_db_fakes import DIM, FakeEmbeddingServer, FakeQdrant, fake_vector, make_config
from src.config import LLMEndpoint
from src.vector_db import catalog as vcat
from src.vector_db import ingest, search
from src.vector_db.chunking import split_text
from src.vector_db.embeddings import EmbeddingError, embed_texts
from src.vector_db.extract import ExtractError, extract_segments
from src.vector_db.qdrant_store import QdrantStore
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
    assert [(s.locator, s.text) for s in segments] == [("シート: 集計", "品番\t10")]

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "slide title"
    slide.notes_slide.notes_text_frame.text = "speaker note"
    prs.save(tmp_path / "a.pptx")
    segments = extract_segments(tmp_path / "a.pptx")
    assert segments[0].locator == "スライド1"
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


def test_embed_texts_batches_and_adds_prefix(tmp_path):
    server = FakeEmbeddingServer()
    cfg = make_config(tmp_path, vector_db_embedding_batch_size=2, vector_db_query_prefix="query: ", vector_db_document_prefix="passage: ")
    vectors = embed_texts(cfg, ["a", "b", "c"], kind="document", transport=server.transport)
    assert len(vectors) == 3 and len(vectors[0]) == DIM
    assert [r["input"] for r in server.requests] == [["passage: a", "passage: b"], ["passage: c"]]
    assert server.requests[0]["model"] == "fake-embed"
    embed_texts(cfg, ["q"], kind="query", transport=server.transport)
    assert server.requests[-1]["input"] == ["query: q"]


def test_embed_texts_fails_over_to_next_endpoint_only_on_connection_error(tmp_path):
    server = FakeEmbeddingServer(fail_hosts=("down",))
    endpoints = (
        LLMEndpoint(base_url="http://down/v1", api_key="k", model="fake-embed"),
        LLMEndpoint(base_url="http://up/v1", api_key="k", model="fake-embed"),
    )
    cfg = make_config(tmp_path, vector_db_embedding_endpoints=endpoints)
    embed_texts(cfg, ["a"], kind="query", transport=server.transport)
    assert server.requests[0]["url"] == "http://up/v1/embeddings"

    cfg = make_config(tmp_path, vector_db_embedding_endpoints=endpoints[:1])
    with pytest.raises(EmbeddingError):
        embed_texts(cfg, ["a"], kind="query", transport=server.transport)


def test_embed_texts_rejects_token_level_embeddings(tmp_path):
    server = FakeEmbeddingServer(two_dimensional=True)
    with pytest.raises(EmbeddingError, match="pooling"):
        embed_texts(make_config(tmp_path), ["a"], kind="query", transport=server.transport)


def test_embed_texts_reports_http_error(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="model not loaded"))
    with pytest.raises(EmbeddingError, match="HTTP 500"):
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


def test_sqlite_vec_store(tmp_path):
    store = SqliteVecStore(tmp_path)
    try:
        _exercise_store(store)
    finally:
        store.close()


def test_qdrant_store_uses_prefixed_collection_names():
    fake = FakeQdrant()
    store = QdrantStore("http://qdrant:6333/", prefix="locohane_x_", api_key="secret", transport=fake.transport)
    try:
        _exercise_store(store)
    finally:
        store.close()
    assert all(path.startswith("/collections/locohane_x_") for _, path in fake.calls)


def test_qdrant_store_sends_api_key_and_reports_connection_error():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("api-key")
        raise httpx.ConnectError("refused", request=request)

    store = QdrantStore("http://qdrant:6333", prefix="p_", api_key="secret", transport=httpx.MockTransport(handler))
    with pytest.raises(StoreError, match="接続できません"):
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
    (docs / "image.png").write_bytes(b"png")
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
    assert [p for p, _ in third.failed] == [str(docs / "empty.md")]

    result = search.search(cfg, "溶接ビードの幅", **kwargs)
    assert [h.text for h in result.hits] == ["溶接ビードの幅は6mm以上に改訂した。"]
    [collection] = search.list_collections(cfg)
    assert (collection.file_count, collection.model, collection.dim) == (1, "fake-embed", DIM)


def test_ingest_skips_files_over_size_limit_and_non_recursive(env):
    cfg = make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, vector_db_max_file_mb=0.00001)
    result = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert len(result.failed) == 2 and "上限" in result.failed[0][1]
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
    with pytest.raises(ingest.IngestError, match="再構築"):
        ingest.ingest_collection(other, "docs", **kwargs)
    # 検索も対象外になり、理由が分かる
    with pytest.raises(search.SearchError, match="other-embed"):
        search.search(other, "溶接", **kwargs)
    rebuilt = ingest.ingest_collection(other, "docs", rebuild=True, **kwargs)
    assert rebuilt.added == 2
    assert search.list_collections(other)[0].model == "other-embed"


def test_ingest_stops_without_deleting_unprocessed_files(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    result = ingest.ingest_collection(cfg, "docs", should_stop=lambda: True, **kwargs)
    assert result.removed == 0
    assert search.list_collections(cfg)[0].file_count == 2


def test_ingest_stop_reports_processed_count(env):
    # 1ファイル処理したところで停止すると、最後の進捗は処理済み1件を指す（以前は0件のまま止まっていた）
    cfg, kwargs = env["cfg"], env["kwargs"]
    progress = []
    result = ingest.ingest_collection(
        cfg, "docs", progress=lambda *a: progress.append(a), should_stop=lambda: len(progress) >= 1, **kwargs
    )
    assert result.added == 1
    assert progress[-1] == (1, 2, "")


def test_ingest_errors_for_missing_collection_or_folder(env):
    with pytest.raises(ingest.IngestError):
        ingest.ingest_collection(env["cfg"], "missing", **env["kwargs"])
    with vcat.open_catalog(env["cfg"].vector_db_dir) as catalog:
        catalog.update("docs", source_folder=str(env["docs"] / "nothing"))
    with pytest.raises(ingest.IngestError, match="フォルダ"):
        ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"])


def test_search_filters_by_collection_score_and_top_k(env, tmp_path):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("empty", str(tmp_path))
    result = search.search(cfg, "会議室の予約", top_k=1, **kwargs)
    assert [h.source_path.endswith("room.txt") for h in result.hits] == [True]
    assert result.skipped == [("empty", "まだ文書が取り込まれていません")]
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
