"""設定ダッシュボードの「ナレッジ」API（admin/knowledge.py）のテスト。"""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, make_config
from admin import instances as inst
from admin import knowledge


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "instances"
    for name, kind in (("org_a", inst.KIND_APP), ("lab1", inst.KIND_SKILL_LAB)):
        (root / name).mkdir(parents=True)
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=18001 if kind == inst.KIND_APP else 0, kind=kind))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "weld.md").write_text("溶接ビードの幅は5mm以上とする。", encoding="utf-8")
    (docs / "broken.docx").write_bytes(b"not a zip")
    configs = {"org_a": make_config(tmp_path / "org_a")}
    embed = FakeEmbeddingServer()
    qdrant = FakeQdrant()
    app = FastAPI()
    app.include_router(
        knowledge.build_router(
            require_login=lambda: "admin",
            require_csrf=lambda: None,
            instances_root=root,
            audit_log_path=tmp_path / "audit.log",
            client_ip=lambda _r: "127.0.0.1",
            config_loader=lambda name: configs[name],
            ingest_kwargs={"embedding_transport": embed.transport, "store_transport": qdrant.transport},
        )
    )
    return {"client": TestClient(app), "docs": docs, "configs": configs, "tmp": tmp_path}


def _wait_job(client, collection):
    for _ in range(200):
        job = client.get(f"/api/instances/org_a/knowledge/collections/{collection}/job").json()["job"]
        if job and job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("取り込みが終わらない")


def test_collection_lifecycle(env):
    client, docs = env["client"], env["docs"]
    res = client.post("/api/instances/org_a/knowledge/collections", json={"name": "docs", "source_folder": str(docs), "description": "品質"})
    assert res.status_code == 200 and res.json()["name"] == "docs"
    assert client.post("/api/instances/org_a/knowledge/collections", json={"name": "docs", "source_folder": str(docs)}).status_code == 400

    started = client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    assert started.status_code == 200 and started.json()["state"] == "running"
    job = _wait_job(client, "docs")
    assert job["state"] == "done", job
    assert job["result"]["added"] == 1 and [f["path"] for f in job["result"]["failed"]] == [str(docs / "broken.docx")]

    overview = client.get("/api/instances/org_a/knowledge").json()
    assert overview["enabled"] and overview["embedding_model"] == "fake-embed"
    [c] = overview["collections"]
    assert (c["file_count"], c["description"], c["job"]["state"]) == (1, "品質", "done")

    assert client.put("/api/instances/org_a/knowledge/collections/docs", json={"description": "改訂"}).json()["description"] == "改訂"
    assert client.delete("/api/instances/org_a/knowledge/collections/docs").json() == {"ok": True}
    assert client.get("/api/instances/org_a/knowledge").json()["collections"] == []

    actions = [json.loads(line)["action"] for line in (env["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert actions == ["knowledge_collection_create", "knowledge_ingest_start", "knowledge_collection_update", "knowledge_collection_delete"]


def test_rejects_bad_folder_skill_lab_and_disabled_ingest(env):
    client, docs = env["client"], env["docs"]
    assert client.post("/api/instances/org_a/knowledge/collections", json={"name": "a", "source_folder": "relative/path"}).status_code == 400
    assert client.post("/api/instances/org_a/knowledge/collections", json={"name": "a", "source_folder": str(docs / "none")}).status_code == 400
    assert client.get("/api/instances/lab1/knowledge").status_code == 400
    assert client.get("/api/instances/nothing/knowledge").status_code == 404
    assert client.post("/api/instances/org_a/knowledge/collections/missing/ingest", json={}).status_code == 404

    client.post("/api/instances/org_a/knowledge/collections", json={"name": "docs", "source_folder": str(docs)})
    env["configs"]["org_a"] = dataclasses.replace(env["configs"]["org_a"], vector_db_enabled=False)
    res = client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    assert res.status_code == 400 and "enabled" in res.json()["detail"]


def test_ingest_error_is_reported_on_job(env):
    client, docs = env["client"], env["docs"]
    client.post("/api/instances/org_a/knowledge/collections", json={"name": "docs", "source_folder": str(docs)})
    docs_renamed = docs.with_name("moved")
    docs.rename(docs_renamed)
    client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    job = _wait_job(client, "docs")
    assert job["state"] == "error" and "フォルダ" in job["error"]


def test_run_ingest_job_marks_stop(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("本文", encoding="utf-8")
    cfg = make_config(tmp_path)
    from src.vector_db import catalog as vcat

    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    job = knowledge.IngestJob(collection="docs", rebuild=False, actor="admin", stop_requested=True)
    knowledge.run_ingest_job(cfg, job, embedding_transport=FakeEmbeddingServer().transport)
    assert job.state == "stopped" and job.finished_at


def test_data_paths_include_vector_db_dir():
    assert "vector_db_dir" in {key for key, _ in inst.DATA_PATH_FIELDS}
