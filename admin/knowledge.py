"""設定ダッシュボードの「ナレッジ」タブの HTTP API（admin/server.py が build_router() で組み込む）。

インスタンスごとのベクターDB（src/vector_db/）のコレクションを作成・編集・削除し、
取り込み元フォルダの文書をバックグラウンドで取り込む。取り込みは管理ツールの
プロセス内のスレッドで行い、対象インスタンスの設定（config_overrides.json を重ねたもの）を使う。
本体インスタンスが起動中でも、sqlite は WAL モードのため検索と取り込みを同時に行える。

コレクションごとに時刻（と曜日）を指定した自動取り込み（差分）も行う（IngestScheduler）。
スケジュールは catalog.sqlite に持ち、管理ツールが起動している間だけ動く
（止まっていた間の時刻の分は、起動後にさかのぼって実行しない）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import logging
from pathlib import Path
import threading
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import Config, load_config
from src.vector_db import catalog as vcat
from src.vector_db.embeddings import EmbeddingError
from src.vector_db.ingest import IngestError, effective_extensions, ingest_collection
from src.vector_db.keyword_index import open_keyword_index
from src.vector_db.store import StoreError, open_store, path_collection_name, storage_collection_names

from . import audit
from . import instances as inst

CONFIG_INI_PATH = Path(__file__).resolve().parent.parent / "config.ini"
SCHEDULER_ACTOR = "scheduler"

logger = logging.getLogger(__name__)


class CreateCollectionBody(BaseModel):
    name: str
    source_folder: str
    description: str = ""
    recursive: bool = True
    schedule_enabled: bool = False
    schedule_times: list[str] = []
    schedule_weekdays: list[int] = []


class UpdateCollectionBody(BaseModel):
    source_folder: str | None = None
    description: str | None = None
    recursive: bool | None = None
    schedule_enabled: bool | None = None
    schedule_times: list[str] | None = None
    schedule_weekdays: list[int] | None = None


class IngestBody(BaseModel):
    rebuild: bool = False


@dataclass
class IngestJob:
    """1コレクションの取り込みジョブの状態（画面のポーリング用）。"""

    collection: str
    rebuild: bool
    actor: str
    trigger: str = "manual"  # manual / schedule（時間指定の自動取り込み）
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

    def running(self) -> dict[str, list[IngestJob]]:
        """取り込み中のジョブをインスタンス名ごとに返す（インスタンス一覧のカード用）。"""
        with self._lock:
            result: dict[str, list[IngestJob]] = {}
            for (i, _c), j in sorted(self._jobs.items()):
                if j.state == "running":
                    result.setdefault(i, []).append(j)
            return result

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


def next_scheduled_run(c: vcat.Collection, now: datetime) -> datetime | None:
    """c の自動取り込みが次に動く日時（now より後の最初の時刻。無効なら None）。"""
    if not c.schedule_enabled or not c.schedule_times:
        return None
    base = now.replace(second=0, microsecond=0)
    for day in range(8):
        date = base + timedelta(days=day)
        if c.schedule_weekdays and date.weekday() not in c.schedule_weekdays:
            continue
        for t in c.schedule_times:
            hour, minute = (int(x) for x in t.split(":"))
            candidate = date.replace(hour=hour, minute=minute)
            if candidate > now:
                return candidate
    return None


def _is_due(c: vcat.Collection, minute: datetime) -> bool:
    if not c.schedule_enabled:
        return False
    if c.schedule_weekdays and minute.weekday() not in c.schedule_weekdays:
        return False
    return minute.strftime("%H:%M") in c.schedule_times


class IngestScheduler:
    """コレクションに指定された時刻に差分取り込みを始める（管理ツールのプロセス内のスレッド）。

    前回確認した分の次の分から現在の分までを順に見て、時刻・曜日が一致するコレクションの
    取り込みを始める（スレッドの起床が遅れても分を飛ばさない。ただし遡るのは MAX_CATCH_UP_MINUTES 分まで）。
    同じコレクションが取り込み中なら、その回は実行しない。
    """

    MAX_CATCH_UP_MINUTES = 10
    POLL_SECONDS = 15

    def __init__(
        self,
        *,
        instances_root: Path,
        jobs: _JobRegistry,
        loader: Callable[[str], Config],
        audit_log_path: Path,
        ingest_kwargs: dict,
    ) -> None:
        self._instances_root = instances_root
        self._jobs = jobs
        self._loader = loader
        self._audit_log_path = audit_log_path
        self._ingest_kwargs = ingest_kwargs
        self._last_minute: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._last_minute = datetime.now().replace(second=0, microsecond=0)
        self._thread = threading.Thread(target=self._run, name="knowledge-ingest-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_SECONDS):
            try:
                self.tick(datetime.now())
            except Exception:  # noqa: BLE001 - 1回の失敗でスケジューラーを止めない
                logger.exception("ナレッジの自動取り込みの確認に失敗しました。")

    def tick(self, now: datetime) -> list[tuple[str, str]]:
        """前回から now までに来た時刻の取り込みを始め、始めた (インスタンス名, コレクション名) を返す。"""
        current = now.replace(second=0, microsecond=0)
        last = self._last_minute
        self._last_minute = current
        if last is None or current <= last:
            return []
        minutes = []
        m = max(last + timedelta(minutes=1), current - timedelta(minutes=self.MAX_CATCH_UP_MINUTES - 1))
        while m <= current:
            minutes.append(m)
            m += timedelta(minutes=1)

        started: list[tuple[str, str]] = []
        for name in inst.list_instance_names(self._instances_root):
            try:
                if inst.read_instance(self._instances_root, name).is_skill_lab:
                    continue
                cfg = self._loader(name)
            except Exception as exc:  # noqa: BLE001 - 設定の読めないインスタンスは飛ばして他を続ける
                logger.warning("自動取り込み: インスタンス %r の設定を読めません: %s", name, exc)
                continue
            if not cfg.vector_db_enabled or not vcat.catalog_exists(cfg.vector_db_dir):
                continue
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                collections = catalog.list_collections()
            for c in collections:
                if not any(_is_due(c, minute) for minute in minutes):
                    continue
                job = IngestJob(collection=c.name, rebuild=False, actor=SCHEDULER_ACTOR, trigger="schedule")
                if not self._jobs.start(name, job):
                    logger.info("自動取り込み: %s/%s は取り込み中のため、今回は実行しません。", name, c.name)
                    continue
                threading.Thread(
                    target=run_ingest_job,
                    args=(cfg, job),
                    kwargs=self._ingest_kwargs,
                    name=f"knowledge-ingest-{name}-{c.name}",
                    daemon=True,
                ).start()
                audit.append(
                    self._audit_log_path,
                    {
                        "instance": name,
                        "actor": SCHEDULER_ACTOR,
                        "remote_addr": "-",
                        "action": "knowledge_ingest_start",
                        "collection": c.name,
                        "rebuild": False,
                        "trigger": "schedule",
                    },
                )
                logger.info("自動取り込み: %s/%s の差分取り込みを始めました。", name, c.name)
                started.append((name, c.name))
        return started


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

    返す router の ingest_scheduler 属性が時間指定の自動取り込みの IngestScheduler
    （手動の取り込みとジョブの記録を共有する）。start()・stop() は呼び出し側（admin/server.py の lifespan）で行う。
    ingest_jobs 属性はジョブの記録（_JobRegistry）。
    """
    router = APIRouter()
    jobs = _JobRegistry()
    extra_ingest_kwargs = dict(ingest_kwargs or {})

    def _audit(request: Request, user: str, instance: str, action: str, **extra) -> None:
        audit.append(audit_log_path, {"instance": instance, "actor": user, "remote_addr": client_ip(request), "action": action, **extra})

    def _default_loader(name: str) -> Config:
        return load_config(config_path=CONFIG_INI_PATH, overrides_path=inst.overrides_path(instances_root, name), instance_name=name)

    loader = config_loader or _default_loader
    # インスタンス一覧のカード（admin/server.py の /api/monitor/overview）が取り込み中のジョブを読む口
    router.ingest_jobs = jobs
    router.ingest_scheduler = IngestScheduler(
        instances_root=instances_root, jobs=jobs, loader=loader, audit_log_path=audit_log_path, ingest_kwargs=extra_ingest_kwargs
    )

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
        next_run = next_scheduled_run(c, datetime.now())
        return {
            **asdict(c),
            "next_scheduled_at": next_run.isoformat(timespec="seconds") if next_run else None,
            "job": job.to_dict() if job else None,
        }

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
            "qdrant_preset": cfg.vector_db_qdrant_preset,
            "distance": cfg.vector_db_distance,
            "embedding_model": endpoints[0].model if endpoints else None,
            "query_prefix": cfg.vector_db_query_prefix,
            "document_prefix": cfg.vector_db_document_prefix,
            "include_extensions": list(effective_extensions(cfg.vector_db_include_extensions, cfg.vector_db_image_input)),
            "image_input": cfg.vector_db_image_input,
            "max_file_mb": cfg.vector_db_max_file_mb,
            "collections": [_collection_dict(c, running.get(c.name)) for c in collections],
        }

    @router.post("/api/instances/{name}/knowledge/collections")
    def create_collection(name: str, body: CreateCollectionBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        folder = _check_folder(body.source_folder)
        try:
            times = vcat.normalize_schedule_times(body.schedule_times)
            weekdays = vcat.normalize_schedule_weekdays(body.schedule_weekdays)
            if body.schedule_enabled and not times:
                raise vcat.CatalogError("自動取り込みを有効にするときは時刻を1つ以上指定してください。")
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                catalog.create(body.name, str(folder), body.description, body.recursive)
                created = catalog.update(body.name, schedule_enabled=body.schedule_enabled, schedule_times=times, schedule_weekdays=weekdays)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(
            request,
            user,
            name,
            "knowledge_collection_create",
            collection=body.name,
            source_folder=str(folder),
            schedule_enabled=created.schedule_enabled,
            schedule_times=list(created.schedule_times),
            schedule_weekdays=list(created.schedule_weekdays),
        )
        return _collection_dict(created, None)

    @router.put("/api/instances/{name}/knowledge/collections/{collection}")
    def update_collection(
        name: str, collection: str, body: UpdateCollectionBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        cfg = _cfg(name)
        folder = str(_check_folder(body.source_folder)) if body.source_folder is not None else None
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            try:
                updated = catalog.update(
                    collection,
                    source_folder=folder,
                    description=body.description,
                    recursive=body.recursive,
                    schedule_enabled=body.schedule_enabled,
                    schedule_times=body.schedule_times,
                    schedule_weekdays=body.schedule_weekdays,
                )
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                    for storage in storage_collection_names(collection):
                        store.drop_collection(storage)
                finally:
                    store.close()
            except StoreError as exc:
                raise HTTPException(status_code=502, detail=f"ベクターを削除できませんでした: {exc}") from exc
            with open_keyword_index(cfg.vector_db_dir) as keywords:
                keywords.drop_collection(collection)
                keywords.drop_collection(path_collection_name(collection))
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
