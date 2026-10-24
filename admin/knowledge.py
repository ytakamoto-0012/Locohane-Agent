"""設定ダッシュボードの「ナレッジ」タブの HTTP API（admin/server.py が build_router() で組み込む）。

インスタンスごとのベクターDB（src/vector_db/）のコレクションを作成・編集・削除し、
取り込み元フォルダの文書をバックグラウンドで取り込む。取り込みは管理ツールの
プロセス内のスレッドで行い、対象インスタンスの設定（config_overrides.json を重ねたもの）を使う。
本体インスタンスが起動中でも、sqlite は WAL モードのため検索と取り込みを同時に行える。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import threading
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import Config, load_config
from src.vector_db import catalog as vcat
from src.vector_db.embeddings import EmbeddingError
from src.vector_db.ingest import IngestError, ingest_collection
from src.vector_db.store import StoreError, open_store

from . import audit
from . import instances as inst

CONFIG_INI_PATH = Path(__file__).resolve().parent.parent / "config.ini"


class CreateCollectionBody(BaseModel):
    name: str
    source_folder: str
    description: str = ""
    recursive: bool = True


class UpdateCollectionBody(BaseModel):
    source_folder: str | None = None
    description: str | None = None
    recursive: bool | None = None


class IngestBody(BaseModel):
    rebuild: bool = False


@dataclass
class IngestJob:
    """1コレクションの取り込みジョブの状態（画面のポーリング用）。"""

    collection: str
    rebuild: bool
    actor: str
    state: str = "running"  # running / done / error / stopped
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    done: int = 0
    total: int = 0
    current: str = ""
    result: dict | None = None
    error: str | None = None
    stop_requested: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class _JobRegistry:
    """(インスタンス名, コレクション名) ごとに最新の取り込みジョブを1件持つ。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], IngestJob] = {}

    def get(self, instance: str, collection: str) -> IngestJob | None:
        with self._lock:
            return self._jobs.get((instance, collection))

    def for_instance(self, instance: str) -> dict[str, IngestJob]:
        with self._lock:
            return {c: j for (i, c), j in self._jobs.items() if i == instance}

    def start(self, instance: str, job: IngestJob) -> bool:
        with self._lock:
            current = self._jobs.get((instance, job.collection))
            if current is not None and current.state == "running":
                return False
            self._jobs[(instance, job.collection)] = job
            return True

    def forget(self, instance: str, collection: str) -> None:
        with self._lock:
            self._jobs.pop((instance, collection), None)


def run_ingest_job(cfg: Config, job: IngestJob, **kwargs) -> None:
    """job の取り込みを実行し、結果・エラーを job に書き込む（スレッドの本体）。"""

    def _progress(done: int, total: int, path: str) -> None:
        job.done, job.total, job.current = done, total, path

    try:
        result = ingest_collection(
            cfg, job.collection, rebuild=job.rebuild, progress=_progress, should_stop=lambda: job.stop_requested, **kwargs
        )
        job.result = result.to_dict()
        job.state = "stopped" if job.stop_requested else "done"
    except (IngestError, EmbeddingError, StoreError, vcat.CatalogError, OSError) as exc:
        job.error = str(exc)
        job.state = "error"
    except Exception as exc:  # noqa: BLE001 - 想定外の例外も画面に出して、ジョブが running のまま残らないようにする
        job.error = f"{type(exc).__name__}: {exc}"
        job.state = "error"
    finally:
        job.current = ""
        job.finished_at = datetime.now().isoformat(timespec="seconds")


def build_router(
    *,
    require_login: Callable,
    require_csrf: Callable,
    instances_root: Path,
    audit_log_path: Path,
    client_ip: Callable[[Request], str],
    config_loader: Callable[[str], Config] | None = None,
    ingest_kwargs: dict | None = None,
) -> APIRouter:
    """ナレッジ API の router を作る。

    Args:
        config_loader: インスタンス名から設定を読む関数（テスト用の差し替え口。
            既定は instances/<名前>/config_overrides.json を重ねた load_config()）。
        ingest_kwargs: ingest_collection() へ追加で渡す引数（テスト用の httpx トランスポート等）。
    """
    router = APIRouter()
    jobs = _JobRegistry()
    extra_ingest_kwargs = dict(ingest_kwargs or {})

    def _audit(request: Request, user: str, instance: str, action: str, **extra) -> None:
        audit.append(audit_log_path, {"instance": instance, "actor": user, "remote_addr": client_ip(request), "action": action, **extra})

    def _default_loader(name: str) -> Config:
        return load_config(config_path=CONFIG_INI_PATH, overrides_path=inst.overrides_path(instances_root, name), instance_name=name)

    loader = config_loader or _default_loader

    def _cfg(name: str) -> Config:
        try:
            inst.validate_name(name)
            meta = inst.read_instance(instances_root, name)
        except inst.InstanceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if meta.is_skill_lab:
            raise HTTPException(status_code=400, detail="スキル研究室のインスタンスにはナレッジを置けません。")
        try:
            return loader(name)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"インスタンス {name} の設定を読めません: {exc}") from exc

    def _collection_dict(c: vcat.Collection, job: IngestJob | None) -> dict:
        return {**asdict(c), "job": job.to_dict() if job else None}

    @router.get("/api/instances/{name}/knowledge")
    def get_knowledge(name: str, user: str = Depends(require_login)):
        cfg = _cfg(name)
        collections = []
        if vcat.catalog_exists(cfg.vector_db_dir):
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                collections = catalog.list_collections()
        running = jobs.for_instance(name)
        endpoints = cfg.vector_db_embedding_endpoints
        return {
            "enabled": cfg.vector_db_enabled,
            "backend": cfg.vector_db_backend,
            "dir": str(cfg.vector_db_dir),
            "qdrant_url": cfg.vector_db_qdrant_url,
            "qdrant_collection_prefix": cfg.vector_db_qdrant_collection_prefix,
            "embedding_model": endpoints[0].model if endpoints else None,
            "include_extensions": list(cfg.vector_db_include_extensions),
            "collections": [_collection_dict(c, running.get(c.name)) for c in collections],
        }

    @router.post("/api/instances/{name}/knowledge/collections")
    def create_collection(name: str, body: CreateCollectionBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        folder = _check_folder(body.source_folder)
        try:
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                created = catalog.create(body.name, str(folder), body.description, body.recursive)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "knowledge_collection_create", collection=body.name, source_folder=str(folder))
        return _collection_dict(created, None)

    @router.put("/api/instances/{name}/knowledge/collections/{collection}")
    def update_collection(
        name: str, collection: str, body: UpdateCollectionBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        cfg = _cfg(name)
        folder = str(_check_folder(body.source_folder)) if body.source_folder is not None else None
        try:
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                updated = catalog.update(collection, source_folder=folder, description=body.description, recursive=body.recursive)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _audit(request, user, name, "knowledge_collection_update", collection=collection, **body.model_dump(exclude_none=True))
        return _collection_dict(updated, jobs.get(name, collection))

    @router.delete("/api/instances/{name}/knowledge/collections/{collection}")
    def delete_collection(name: str, collection: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        job = jobs.get(name, collection)
        if job is not None and job.state == "running":
            raise HTTPException(status_code=409, detail="取り込み中のコレクションは削除できません。先に停止してください。")
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            try:
                store = open_store(cfg, transport=extra_ingest_kwargs.get("store_transport"))
                try:
                    store.drop_collection(collection)
                finally:
                    store.close()
            except StoreError as exc:
                raise HTTPException(status_code=502, detail=f"ベクターを削除できませんでした: {exc}") from exc
            catalog.delete(collection)
        jobs.forget(name, collection)
        _audit(request, user, name, "knowledge_collection_delete", collection=collection)
        return {"ok": True}

    @router.post("/api/instances/{name}/knowledge/collections/{collection}/ingest")
    def start_ingest(
        name: str, collection: str, body: IngestBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        cfg = _cfg(name)
        if not cfg.vector_db_enabled:
            raise HTTPException(status_code=400, detail="このインスタンスの [vector_db] enabled が false です。config.ini タブで有効にしてください。")
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        job = IngestJob(collection=collection, rebuild=body.rebuild, actor=user)
        if not jobs.start(name, job):
            raise HTTPException(status_code=409, detail="このコレクションは取り込み中です。")
        threading.Thread(
            target=run_ingest_job, args=(cfg, job), kwargs=extra_ingest_kwargs, name=f"knowledge-ingest-{name}-{collection}", daemon=True
        ).start()
        _audit(request, user, name, "knowledge_ingest_start", collection=collection, rebuild=body.rebuild)
        return job.to_dict()

    @router.get("/api/instances/{name}/knowledge/collections/{collection}/job")
    def get_job(name: str, collection: str, user: str = Depends(require_login)):
        _cfg(name)
        job = jobs.get(name, collection)
        return {"job": job.to_dict() if job else None}

    @router.post("/api/instances/{name}/knowledge/collections/{collection}/stop")
    def stop_ingest(name: str, collection: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        _cfg(name)
        job = jobs.get(name, collection)
        if job is None or job.state != "running":
            raise HTTPException(status_code=409, detail="取り込み中ではありません。")
        job.stop_requested = True
        _audit(request, user, name, "knowledge_ingest_stop", collection=collection)
        return job.to_dict()

    return router


def _check_folder(value: str) -> Path:
    folder = Path(value.strip())
    if not value.strip() or not folder.is_absolute():
        raise HTTPException(status_code=400, detail="取り込み元フォルダは絶対パスで指定してください。")
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"取り込み元フォルダがありません（管理ツールのPCから見えるパスを指定してください）: {folder}")
    return folder
