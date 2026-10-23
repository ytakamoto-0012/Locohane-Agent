"""[vector_db] image_input（ページを画像にして埋め込む）のテスト。

Office ファイルの画像化（COM で Word 等を起動する）は環境に依存するため、取り込みのテストでは
render_pages を偽物に差し替える。PDF の画像化だけは pypdfium2 で実際に行う。
"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, make_config
from src.vector_db import catalog as vcat
from src.vector_db import ingest, search
from src.vector_db.embeddings import embed_images
from src.vector_db.extract import ExtractError
from src.vector_db.page_images import PAGE_IMAGES_DIRNAME
from src.vector_db.render import PageImage, render_pages
from src.vector_db.sqlite_vec_store import SqliteVecStore

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# --- 画像化 -----------------------------------------------------------------------


def test_render_pages_renders_each_pdf_page_with_text(tmp_path):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(tmp_path / "a.pdf"))
    c.drawString(72, 720, "page one text")
    c.showPage()
    c.showPage()  # 文字の無いページ
    c.save()
    pages = render_pages(tmp_path / "a.pdf", dpi=50)
    assert [p.locator for p in pages] == ["p.1", "p.2"]
    assert "page one text" in pages[0].text and pages[1].text.strip() == ""
    assert all(p.png.startswith(_PNG_SIGNATURE) for p in pages)


def test_render_pages_rejects_unsupported_and_broken_files(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    with pytest.raises(ExtractError, match="対応していない"):
        render_pages(tmp_path / "a.md", dpi=50)
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    with pytest.raises(ExtractError):
        render_pages(tmp_path / "broken.pdf", dpi=50)


# --- 埋め込み ---------------------------------------------------------------------


def test_embed_images_sends_one_image_per_request_in_llama_server_format(tmp_path):
    cfg = make_config(tmp_path, vector_db_document_prefix="passage: ")
    server = FakeEmbeddingServer()
    vectors = embed_images(cfg, ["溶接".encode(), "検査".encode()], transport=server.transport)
    assert len(vectors) == 2
    assert [len(r["input"]) for r in server.requests] == [1, 1]
    [item] = server.requests[0]["input"]
    part = item["content"][0]
    assert part["type"] == "image_url"
    # 画像には document_prefix（テキスト用）を付けない
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == "溶接".encode()


# --- 取り込みと検索 ---------------------------------------------------------------


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    from reportlab.pdfgen import canvas

    # 中身の画像化は下の fake_render が代わりに行う（テキストとして取り込むときは実際の文字を読む）。
    c = canvas.Canvas(str(docs / "manual.pdf"))
    c.drawString(72, 720, "welding manual")
    c.showPage()
    c.save()
    (docs / "note.md").write_text("会議室の予約はポータルから行う。", encoding="utf-8")
    rendered = []

    def fake_render(path, dpi):
        rendered.append((path.name, dpi))
        # 「画像」の代わりに文字列のバイト列を渡す（偽の埋め込みサーバーはそれを読んでベクターにする）。
        return [PageImage("p.1", "溶接ビードの幅は5mm以上", "溶接ビードの幅の図".encode()), PageImage("p.2", "", "外観検査の写真".encode())]

    monkeypatch.setattr(ingest, "render_pages", fake_render)
    cfg = make_config(tmp_path, vector_db_backend=request.param, vector_db_image_input=True, vector_db_image_dpi=72)
    embed = FakeEmbeddingServer()
    qdrant = FakeQdrant()
    kwargs = {"embedding_transport": embed.transport, "store_transport": qdrant.transport}
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return {"cfg": cfg, "docs": docs, "kwargs": kwargs, "embed": embed, "rendered": rendered}


def _page_images(cfg):
    return sorted((cfg.vector_db_dir / PAGE_IMAGES_DIRNAME / "docs").glob("*.png"))


def test_ingest_embeds_pages_as_images_and_keeps_text_files_as_text(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert (result.added, result.chunks, result.failed) == (2, 3, [])
    assert env["rendered"] == [("manual.pdf", 72)]
    image_requests = [r for r in env["embed"].requests if isinstance(r["input"][0], dict)]
    assert len(image_requests) == 2
    assert len(_page_images(cfg)) == 2

    hits = search.search(cfg, "外観検査の写真", top_k=1, **kwargs).hits
    assert hits[0].locator == "p.2" and hits[0].image_path
    assert hits[0].text.startswith("（文字情報の無いページです")
    with open(hits[0].image_path, "rb") as f:
        assert f.read() == "外観検査の写真".encode()
    text_hit = search.search(cfg, "会議室の予約はポータルから行う。", top_k=1, **kwargs).hits[0]
    assert text_hit.source_path.endswith("note.md") and text_hit.image_path == ""

    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        modes = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: r.mode for p, r in catalog.files("docs").items()}
    assert modes == {"manual.pdf": "image", "note.md": "text"}

    # 変更が無ければ画像化し直さない
    assert ingest.ingest_collection(cfg, "docs", **kwargs).unchanged == 2
    assert len(env["rendered"]) == 1


def test_switching_image_input_reingests_only_affected_files(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    text_cfg = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_image_input=False)
    result = ingest.ingest_collection(text_cfg, "docs", **kwargs)
    # 内容の変わっていない PDF もテキストで取り込み直す。note.md はもともとテキストなので取り込み直さない
    assert (result.updated, result.unchanged, result.failed) == (1, 1, [])
    assert _page_images(cfg) == []
    [hit] = search.search(text_cfg, "welding manual", collection="docs", top_k=1, **kwargs).hits
    assert (hit.text, hit.image_path) == ("welding manual", "")
    # 画像に戻すと、また画像で取り込み直す
    again = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert (again.updated, again.unchanged) == (1, 1)
    assert len(_page_images(cfg)) == 2


def test_text_only_failure_suggests_image_input(env):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(env["docs"] / "manual.pdf"))
    c.showPage()  # 文字の無い（スキャンした図面のような）PDF
    c.save()
    text_cfg = make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, vector_db_image_input=False)
    result = ingest.ingest_collection(text_cfg, "docs", **env["kwargs"])
    assert "image_input = true" in result.failed[0][1]


def test_removed_files_and_rebuild_delete_page_images(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    ingest.ingest_collection(cfg, "docs", rebuild=True, **kwargs)
    assert len(_page_images(cfg)) == 2  # 消してから作り直す（重複しない）
    (env["docs"] / "manual.pdf").unlink()
    assert ingest.ingest_collection(cfg, "docs", **kwargs).removed == 1
    assert _page_images(cfg) == []


def test_long_page_text_is_cut_to_chunk_size(env, monkeypatch):
    monkeypatch.setattr(ingest, "render_pages", lambda path, dpi: [PageImage("p.1", "あ" * 500, b"x")])
    cfg = env["cfg"]
    ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    hit = search.search(cfg, "x", collection="docs", top_k=5, **env["kwargs"]).hits
    page = next(h for h in hit if h.locator == "p.1")
    assert page.text == "あ" * cfg.vector_db_chunk_size + "…"


def test_old_databases_get_new_columns(tmp_path):
    old = sqlite3.connect(tmp_path / "catalog.sqlite")
    old.executescript(
        """
        CREATE TABLE files (collection TEXT NOT NULL, source_path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL,
            sha256 TEXT NOT NULL, chunk_count INTEGER NOT NULL, PRIMARY KEY (collection, source_path));
        INSERT INTO files VALUES ('docs', '/a.pdf', 1, 1.0, 'h', 2);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER NOT NULL, source_path TEXT NOT NULL,
            locator TEXT NOT NULL, text TEXT NOT NULL);
        """
    )
    old.close()
    (tmp_path / "vectors.sqlite").write_bytes((tmp_path / "catalog.sqlite").read_bytes())
    with vcat.open_catalog(tmp_path) as catalog:
        assert catalog.files("docs")["/a.pdf"].mode == "text"
    store = SqliteVecStore(tmp_path)
    try:
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(chunks)")}
    finally:
        store.close()
    assert "image_path" in columns


def test_config_reads_image_input(tmp_path, monkeypatch):
    from src.config import load_config

    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    for key in ("VECTOR_DB_IMAGE_INPUT", "VECTOR_DB_IMAGE_DPI"):
        monkeypatch.delenv(key, raising=False)
    default = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert (default.vector_db_image_input, default.vector_db_image_dpi) == (False, 100)
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"vector_db": {"image_input": "true", "image_dpi": "150"}}), encoding="utf-8")
    cfg = load_config(overrides_path=path, instance_name="inst1")
    assert (cfg.vector_db_image_input, cfg.vector_db_image_dpi) == (True, 150)
    path.write_text(json.dumps({"vector_db": {"image_dpi": "0"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="image_dpi"):
        load_config(overrides_path=path, instance_name="inst1")
