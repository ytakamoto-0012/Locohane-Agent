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
    router = knowledge.build_router(
        require_login=lambda: "admin",
        require_csrf=lambda: None,
        instances_root=root,
        audit_log_path=tmp_path / "audit.log",
        client_ip=lambda _r: "127.0.0.1",
        config_loader=lambda name: configs[name],
        ingest_kwargs={"embedding_transport": embed.transport, "store_transport": qdrant.transport},
    )
    app.include_router(router)
    # スケジューラーのスレッドは起動せず、テストから tick() を直接呼ぶ
    app.state.knowledge_scheduler = router.ingest_scheduler
    return {"client": TestClient(app), "docs": docs, "configs": configs, "tmp": tmp_path, "router": router}


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
    assert job["state"] == "error" and "folder" in job["error"]


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


def test_schedule_is_saved_validated_and_shown(env):
    client, docs = env["client"], env["docs"]
    base = "/api/instances/org_a/knowledge/collections"
    bad = client.post(base, json={"name": "docs", "source_folder": str(docs), "schedule_enabled": True, "schedule_times": ["25:00"]})
    assert bad.status_code == 400 and "times" in bad.json()["detail"]
    assert client.post(base, json={"name": "docs", "source_folder": str(docs), "schedule_enabled": True}).status_code == 400
    assert client.get("/api/instances/org_a/knowledge").json()["collections"] == []

    res = client.post(
        base,
        json={"name": "docs", "source_folder": str(docs), "schedule_enabled": True, "schedule_times": ["7:30", "02:00", "07:30"], "schedule_weekdays": [4, 0]},
    ).json()
    assert (res["schedule_enabled"], res["schedule_times"], res["schedule_weekdays"]) == (True, ["02:00", "07:30"], [0, 4])
    assert res["next_scheduled_at"]

    assert client.put(f"{base}/docs", json={"schedule_weekdays": [7]}).status_code == 400
    assert client.put(f"{base}/missing", json={"description": "x"}).status_code == 404
    # 説明だけ変えてもスケジュールは残る
    kept = client.put(f"{base}/docs", json={"description": "改訂"}).json()
    assert (kept["schedule_enabled"], kept["schedule_times"]) == (True, ["02:00", "07:30"])
    off = client.put(f"{base}/docs", json={"schedule_enabled": False}).json()
    assert (off["schedule_enabled"], off["schedule_times"], off["next_scheduled_at"]) == (False, ["02:00", "07:30"], None)


def test_next_scheduled_run():
    from datetime import datetime

    from src.vector_db.catalog import Collection

    c = Collection("docs", "", "/d", True, None, None, "", None, schedule_enabled=True, schedule_times=("02:00", "14:00"))
    assert knowledge.next_scheduled_run(c, datetime(2026, 10, 24, 1, 0)) == datetime(2026, 10, 24, 2, 0)
    assert knowledge.next_scheduled_run(c, datetime(2026, 10, 24, 2, 0, 30)) == datetime(2026, 10, 24, 14, 0)
    assert knowledge.next_scheduled_run(c, datetime(2026, 10, 24, 15, 0)) == datetime(2026, 10, 25, 2, 0)
    # 2026-10-24 は土曜。月曜（0）だけなら 10-26 の 02:00
    weekly = dataclasses.replace(c, schedule_weekdays=(0,))
    assert knowledge.next_scheduled_run(weekly, datetime(2026, 10, 24, 1, 0)) == datetime(2026, 10, 26, 2, 0)
    assert knowledge.next_scheduled_run(dataclasses.replace(c, schedule_enabled=False), datetime(2026, 10, 24)) is None


def test_scheduler_starts_due_ingest(env, monkeypatch):
    from datetime import datetime

    client, docs = env["client"], env["docs"]
    client.post(
        "/api/instances/org_a/knowledge/collections",
        json={"name": "docs", "source_folder": str(docs), "schedule_enabled": True, "schedule_times": ["02:00"]},
    )
    client.post("/api/instances/org_a/knowledge/collections", json={"name": "other", "source_folder": str(docs)})
    scheduler = _scheduler(client)

    # 起動直後の初回は基準の分を覚えるだけ
    scheduler._last_minute = datetime(2026, 10, 24, 1, 58)
    assert scheduler.tick(datetime(2026, 10, 24, 1, 59, 50)) == []
    # 起床が遅れて 02:00 を飛ばしても、前回の次の分から確かめて実行する
    assert scheduler.tick(datetime(2026, 10, 24, 2, 1, 5)) == [("org_a", "docs")]
    job = _wait_job(client, "docs")
    assert (job["state"], job["trigger"], job["actor"], job["rebuild"]) == ("done", "schedule", "scheduler", False)
    assert client.get("/api/instances/org_a/knowledge/collections/other/job").json()["job"] is None
    # 同じ分を2回確かめても二重には始めない
    assert scheduler.tick(datetime(2026, 10, 24, 2, 1, 40)) == []
    # 長く止まっていた分は MAX_CATCH_UP_MINUTES より前まで遡らない
    scheduler._last_minute = datetime(2026, 10, 24, 1, 0)
    assert scheduler.tick(datetime(2026, 10, 24, 3, 0)) == []

    entry = json.loads((env["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()[-1])
    assert (entry["action"], entry["actor"], entry["trigger"]) == ("knowledge_ingest_start", "scheduler", "schedule")

    # 無効なインスタンスでは動かさない
    env["configs"]["org_a"] = dataclasses.replace(env["configs"]["org_a"], vector_db_enabled=False)
    scheduler._last_minute = datetime(2026, 10, 25, 1, 59)
    assert scheduler.tick(datetime(2026, 10, 25, 2, 0)) == []


def test_scheduler_skips_running_collection(env):
    from datetime import datetime

    client, docs = env["client"], env["docs"]
    client.post(
        "/api/instances/org_a/knowledge/collections",
        json={"name": "docs", "source_folder": str(docs), "schedule_enabled": True, "schedule_times": ["02:00"]},
    )
    scheduler = _scheduler(client)
    scheduler._jobs.start("org_a", knowledge.IngestJob(collection="docs", rebuild=False, actor="admin"))
    scheduler._last_minute = datetime(2026, 10, 24, 1, 59)
    assert scheduler.tick(datetime(2026, 10, 24, 2, 0)) == []


def _scheduler(client) -> knowledge.IngestScheduler:
    return client.app.state.knowledge_scheduler


def test_running_jobs_by_instance():
    jobs = knowledge._JobRegistry()
    jobs.start("org_a", knowledge.IngestJob(collection="b", rebuild=False, actor="admin"))
    jobs.start("org_a", knowledge.IngestJob(collection="a", rebuild=False, actor="scheduler", trigger="schedule"))
    jobs.start("org_b", knowledge.IngestJob(collection="x", rebuild=False, actor="admin", state="done"))
    running = jobs.running()
    assert list(running) == ["org_a"]
    assert [j.collection for j in running["org_a"]] == ["a", "b"]
    assert running["org_a"][0].trigger == "schedule"


# --- インスタンス間のコピーと、別のインスタンスの参照 ---


@pytest.fixture
def multi(env):
    """org_a に取り込み済みの docs、org_b（同じ埋め込みモデル）・org_c（別の埋め込みモデル）を足した環境。"""
    root, tmp = env["tmp"] / "instances", env["tmp"]
    for i, name in enumerate(("org_b", "org_c")):
        (root / name).mkdir(parents=True)
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=18002 + i))
    env["configs"]["org_b"] = make_config(tmp / "org_b")
    other = make_config(tmp / "org_c")
    env["configs"]["org_c"] = dataclasses.replace(
        other, vector_db_embedding_endpoints=(dataclasses.replace(other.vector_db_embedding_endpoints[0], model="other-embed"),)
    )
    client = env["client"]
    client.post("/api/instances/org_a/knowledge/collections", json={"name": "docs", "source_folder": str(env["docs"]), "description": "品質"})
    client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    assert _wait_job(client, "docs")["state"] == "done"
    return env


def _wait_job_of(client, instance, collection):
    for _ in range(200):
        job = client.get(f"/api/instances/{instance}/knowledge/collections/{collection}/job").json()["job"]
        if job and job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("ジョブが終わらない")


def test_copy_collection_to_another_instance(multi):
    client = multi["client"]
    res = client.post("/api/instances/org_a/knowledge/collections/docs/copy", json={"dest_instance": "org_b", "dest_name": "manuals"})
    assert res.status_code == 200 and (res.json()["kind"], res.json()["source"]) == ("copy", "org_a/docs")
    job = _wait_job_of(client, "org_b", "manuals")
    assert job["state"] == "done" and (job["result"]["files"], job["result"]["with_vectors"]) == (1, True)
    [c] = client.get("/api/instances/org_b/knowledge").json()["collections"]
    assert (c["name"], c["file_count"], c["model"], c["description"]) == ("manuals", 1, "fake-embed", "品質")

    # 同じ名前は拒否し、埋め込みモデルの違うインスタンスへは定義だけならコピーできる。
    again = client.post("/api/instances/org_a/knowledge/collections/docs/copy", json={"dest_instance": "org_b", "dest_name": "manuals"})
    assert again.status_code == 400 and "manuals" in again.json()["detail"]
    bad = client.post("/api/instances/org_a/knowledge/collections/docs/copy", json={"dest_instance": "org_c", "dest_name": "docs"})
    assert bad.status_code == 400 and "embedding model differs" in bad.json()["detail"]
    ok = client.post("/api/instances/org_a/knowledge/collections/docs/copy", json={"dest_instance": "org_c", "dest_name": "docs", "with_vectors": False})
    assert ok.status_code == 200 and _wait_job_of(client, "org_c", "docs")["state"] == "done"
    [c] = client.get("/api/instances/org_c/knowledge").json()["collections"]
    assert (c["file_count"], c["model"]) == (0, None)
    assert client.post("/api/instances/org_a/knowledge/collections/docs/copy", json={"dest_instance": "lab1", "dest_name": "x"}).status_code == 400

    actions = [json.loads(line)["action"] for line in (multi["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert actions.count("knowledge_collection_copy") == 2


def test_copy_source_cannot_be_ingested_or_deleted_while_copying(multi):
    client, jobs = multi["client"], multi["router"].ingest_jobs
    assert jobs.start_copy("org_b", knowledge.IngestJob(collection="manuals", rebuild=False, actor="admin", kind="copy"), "org_a", "docs")
    res = client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    assert res.status_code == 409 and "コピー中" in res.json()["detail"]
    assert client.delete("/api/instances/org_a/knowledge/collections/docs").status_code == 409
    # コピー先が「コピー中」なので、同じコピー先への2つ目のコピーも始めない。
    res = client.post("/api/instances/org_a/knowledge/collections/docs/copy", json={"dest_instance": "org_b", "dest_name": "manuals"})
    assert res.status_code == 409
    jobs.finish_copy("org_a", "docs")
    assert not jobs.is_copy_source("org_a", "docs")
    assert client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={}).status_code == 200
    _wait_job(client, "docs")
    # コピー元が取り込み中ならコピーも始めない。
    busy = knowledge._JobRegistry()
    busy.start("org_a", knowledge.IngestJob(collection="docs", rebuild=False, actor="admin"))
    assert not busy.start_copy("org_b", knowledge.IngestJob(collection="m", rebuild=False, actor="admin", kind="copy"), "org_a", "docs")


def test_references_between_instances(multi):
    client = multi["client"]
    res = client.post("/api/instances/org_b/knowledge/references", json={"name": "a_docs", "source_instance": "org_a", "source_collection": "docs"})
    assert res.status_code == 200 and (res.json()["status"], res.json()["collection"]["file_count"]) == ("ok", 1)
    [ref] = client.get("/api/instances/org_b/knowledge").json()["references"]
    assert (ref["name"], ref["source_instance"], ref["status"]) == ("a_docs", "org_a", "ok")
    [c] = client.get("/api/instances/org_a/knowledge").json()["collections"]
    assert c["referenced_by"] == ["org_b/a_docs"]

    # 埋め込みモデルの違うインスタンスからの参照は追加できるが、状態は incompatible。
    res = client.post("/api/instances/org_c/knowledge/references", json={"name": "a_docs", "source_instance": "org_a", "source_collection": "docs"})
    assert res.json()["status"] == "incompatible" and "other-embed" in res.json()["reason"]

    for body in (
        {"name": "x", "source_instance": "org_b", "source_collection": "docs"},  # 自分自身
        {"name": "x", "source_instance": "lab1", "source_collection": "docs"},  # スキル研究室
        {"name": "x", "source_instance": "org_a", "source_collection": "none"},  # 無いコレクション
        {"name": "a_docs", "source_instance": "org_a", "source_collection": "docs"},  # 名前の重複
        {"name": "bad name", "source_instance": "org_a", "source_collection": "docs"},
    ):
        assert client.post("/api/instances/org_b/knowledge/references", json=body).status_code == 400, body
    # 参照と同じ名前のコレクションは作れない。
    assert client.post("/api/instances/org_b/knowledge/collections", json={"name": "a_docs", "source_folder": str(multi["docs"])}).status_code == 400

    sources = client.get("/api/knowledge/sources").json()["instances"]
    assert [s["name"] for s in sources] == ["org_a", "org_b", "org_c"]
    assert sources[0]["collections"][0]["name"] == "docs" and sources[2]["embedding_model"] == "other-embed"

    # 参照元のコレクションを消すと missing になる。
    assert client.delete("/api/instances/org_a/knowledge/collections/docs").status_code == 200
    [ref] = client.get("/api/instances/org_b/knowledge").json()["references"]
    assert ref["status"] == "missing"
    assert client.delete("/api/instances/org_b/knowledge/references/a_docs").json() == {"ok": True}
    assert client.delete("/api/instances/org_b/knowledge/references/a_docs").status_code == 404
    assert client.get("/api/instances/org_b/knowledge").json()["references"] == []
