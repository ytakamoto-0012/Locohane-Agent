"""config.ini [vector_db] の読み込み・検証のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import load_config


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """実プロジェクトの data/ にテスト用ディレクトリを作らないよう、データ保存先を tmp へ逃がす。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    for key in ("VECTOR_DB_ENABLED", "VECTOR_DB_BACKEND", "VECTOR_DB_EMBEDDING_URL", "VECTOR_DB_QDRANT_URL", "VECTOR_DB_DISTANCE", "QDRANT_API_KEY"):
        monkeypatch.delenv(key, raising=False)


_EMBED = '[{"base_url": "http://localhost:12440/v1", "api_key": "x", "model": "bge-m3"}]'


def _load(tmp_path: Path, vector_db: dict, instance: str = "inst1"):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"vector_db": vector_db}), encoding="utf-8")
    return load_config(overrides_path=path, instance_name=instance)


def test_defaults_are_disabled_sqlite_vec(tmp_path):
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert cfg.vector_db_enabled is False
    assert cfg.vector_db_backend == "sqlite_vec"
    assert cfg.vector_db_dir == tmp_path / "data" / "inst1" / "vector_db"
    assert cfg.vector_db_embedding_endpoints == ()
    assert cfg.vector_db_qdrant_collection_prefix == "locohane_inst1_"
    assert cfg.vector_db_distance == "Cosine"
    assert ".pdf" in cfg.vector_db_include_extensions
    # 無効のときはディレクトリを作らない
    assert not cfg.vector_db_dir.exists()


def test_enabled_with_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("QDRANT_API_KEY", "secret")
    cfg = _load(
        tmp_path,
        {
            "enabled": "true",
            "backend": "qdrant",
            "qdrant_url": "http://localhost:6333",
            "embedding_url": _EMBED,
            "query_prefix": '"query: "',
            "include_extensions": "PDF, .md, md",
        },
    )
    assert cfg.vector_db_enabled and cfg.vector_db_backend == "qdrant"
    assert cfg.vector_db_embedding_endpoints[0].model == "bge-m3"
    assert cfg.vector_db_query_prefix == "query: "
    assert cfg.vector_db_include_extensions == (".pdf", ".md")
    assert cfg.vector_db_qdrant_api_key == "secret"
    assert cfg.vector_db_dir.is_dir()


@pytest.mark.parametrize(
    "vector_db, message",
    [
        ({"enabled": "true"}, "embedding_url"),
        ({"enabled": "true", "embedding_url": _EMBED, "backend": "qdrant"}, "qdrant_url"),
        ({"backend": "chroma"}, "backend"),
        ({"chunk_size": "100", "chunk_overlap": "100"}, "chunk_overlap"),
        ({"min_score": "1.5"}, "min_score"),
        ({"top_k": "0"}, "top_k"),
        (
            {"embedding_url": '[{"base_url": "http://a/v1", "model": "m1"}, {"base_url": "http://b/v1", "model": "m2"}]'},
            "同じ",
        ),
        ({"embedding_url": '[{"base_url": "http://a/v1", "model": "m1", "start": 9, "end": 18}]'}, "start/end"),
        ({"embedding_url": '[{"base_url": "http://a/v1"}]'}, r"\[vector_db\]\.embedding_url"),
    ],
)
def test_invalid_settings_are_rejected(tmp_path, vector_db, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, vector_db)


def test_admin_catalog_offers_embedding_url_editor():
    from admin.ini_catalog import parse_file

    catalog = parse_file(Path(__file__).resolve().parent.parent / "config.ini")
    info = {(k.section, k.key): k for k in catalog.keys}[("vector_db", "embedding_url")]
    assert info.ui_kind == "list"
    assert [f["name"] for f in info.schema["item"]["fields"]] == ["base_url", "api_key", "model"]


@pytest.mark.parametrize(("value", "expected"), [("euclid", "Euclid"), (" DOT ", "Dot"), ("Manhattan", "Manhattan")])
def test_distance_is_normalized(tmp_path, value, expected):
    assert _load(tmp_path, {"backend": "qdrant", "distance": value}).vector_db_distance == expected


def test_distance_rejects_unknown_value(tmp_path):
    with pytest.raises(ValueError, match="distance"):
        _load(tmp_path, {"distance": "l2"})


def test_sqlite_vec_rejects_dot(tmp_path):
    with pytest.raises(ValueError, match="sqlite-vec は Dot"):
        _load(tmp_path, {"backend": "sqlite_vec", "distance": "dot"})


def test_ingest_workers_default_and_validation(tmp_path):
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_ingest_workers == 1
    assert _load(tmp_path, {"ingest_workers": "4"}).vector_db_ingest_workers == 4
    with pytest.raises(ValueError, match="ingest_workers"):
        _load(tmp_path, {"ingest_workers": "0"})
