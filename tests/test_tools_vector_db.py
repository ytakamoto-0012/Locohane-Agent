"""search_documents / list_document_collections ツールのテスト。"""

from __future__ import annotations

import dataclasses
import importlib

import pytest

from _vector_db_fakes import FakeEmbeddingServer, make_config
from src import tools
from src.vector_db import catalog as vcat
from src.vector_db import ingest

vector_db_tools = importlib.import_module("src.tools.vector_db_tools")


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"thread_id": "thread-1"}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


@pytest.fixture
def env(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "weld.md").write_text("溶接ビードの幅は5mm以上とする。", encoding="utf-8")
    (docs / "room.md").write_text("会議室の予約はポータルから行う。" * 20, encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_chunk_size=400, vector_db_chunk_overlap=0)
    embed = FakeEmbeddingServer()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs), "品質文書")
    ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport)

    # ツールは埋め込みサーバーへ実際に接続するため、偽物のトランスポートを差し込む。
    real_search = vector_db_tools.vector_search.search
    monkeypatch.setattr(
        vector_db_tools.vector_search, "search", lambda *a, **k: real_search(*a, embedding_transport=embed.transport, **k)
    )
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", cfg)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", tmp_path / "path_memory")
    (tmp_path / "path_memory").mkdir()
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    return {"cfg": cfg, "docs": docs}


def test_tools_report_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", make_config(tmp_path, vector_db_enabled=False))
    assert "無効" in tools.search_documents.func(query="溶接")
    assert "無効" in tools.list_document_collections.func()


def test_list_document_collections(env):
    text = tools.list_document_collections.func()
    assert "docs - 品質文書" in text and "文書 2 件" in text


def test_search_documents_returns_sources_with_path_memory_refs(env):
    text = tools.search_documents.func(query="溶接ビードの幅", top_k=1)
    assert text.startswith("1件見つかりました。")
    assert "@1 " + str(env["docs"] / "weld.md") in text
    assert "溶接ビードの幅は5mm以上とする。" in text


def test_search_documents_truncates_to_max_result_chars(env, monkeypatch):
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", dataclasses.replace(env["cfg"], vector_db_max_result_chars=150))
    text = tools.search_documents.func(query="会議室の予約")
    assert "以下省略" in text or "省略しました" in text
    assert len(text) < 400


def test_search_documents_returns_error_text(env):
    assert tools.search_documents.func(query="x", collection="missing").startswith("エラー: ")


def test_tools_are_registered_for_main_and_subagents():
    from src.agent_types import scan_agent_types
    from src.config import load_config

    names = {t.name for t in tools.get_all_tools()}
    assert {"search_documents", "list_document_collections"} <= names
    cfg = load_config()
    by_name = {a.name: a for a in scan_agent_types(cfg.agents_dir)}
    for agent in ("explore", "worker", "verifier"):
        resolved = {t.name for t in tools._resolve_agent_types([by_name[agent]])[agent].tools}
        assert {"search_documents", "list_document_collections"} <= resolved, agent
    allowed = {entry for entry, _ in cfg.main_agent_tool_guard_allow_entries}
    assert {"search_documents", "list_document_collections"} <= allowed


def test_search_documents_shows_page_image_refs(env, monkeypatch, tmp_path):
    from reportlab.pdfgen import canvas

    from src.vector_db.render import PageImage

    c = canvas.Canvas(str(env["docs"] / "drawing.pdf"))
    c.showPage()
    c.save()
    monkeypatch.setattr(ingest, "render_pages", lambda path, dpi: [PageImage("p.1", "", "配管の図面".encode())])
    cfg = dataclasses.replace(env["cfg"], vector_db_image_input=True)
    embed = FakeEmbeddingServer()
    ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", cfg)
    text = tools.search_documents.func(query="配管の図面", top_k=1)
    assert str(env["docs"] / "drawing.pdf") + " p.1" in text
    assert "ページ画像: @2 " in text and ".png" in text
