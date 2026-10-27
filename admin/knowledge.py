"""設定ダッシュボードの「ナレッジ」タブの HTTP API（admin/server.py が build_router() で組み込む）。

インスタンスごとのベクターDB（src/vector_db/）のコレクションを作成・編集・削除し、
取り込み元フォルダの文書をバックグラウンドで取り込む。取り込みは管理ツールの
プロセス内のスレッドで行い、対象インスタンスの設定（config_overrides.json を重ねたもの）を使う。
本体インスタンスが起動中でも、sqlite は WAL モードのため検索と取り込みを同時に行える。

コレクションごとに時刻（と曜日）を指定した自動取り込み（差分）も行う（IngestScheduler）。
スケジュールは catalog.sqlite に持ち、管理ツールが起動している間だけ動く
（止まっていた間の時刻の分は、起動後にさかのぼって実行しない）。

インスタンス間のコピー（src/vector_db/collection_copy.py。取り込みと同じくジョブとしてスレッドで動かす）と、
別のインスタンスのコレクションの参照（catalog.sqlite の collection_refs。検索は src/vector_db/search.py）も扱う。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import threading
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import Config, load_config
from src.vector_db import catalog as vcat
from src.vector_db.collection_copy import CopyError, check_compatible, copy_collection
from src.vector_db.embeddings import EmbeddingError, embedding_model
from src.vector_db.ingest import IngestError, effective_extensions, ingest_collection
from src.vector_db.keyword_index import open_keyword_index
from src.vector_db.store import StoreError, open_store, path_collection_name, storage_collection_names

from . import audit
from . import instances as inst
from admin.i18n import t as _t

CONFIG_INI_PATH = Path(__file__).resolve().parent.parent / "config.ini"
SCHEDULER_ACTOR = "scheduler"

logger = logging.getLogger(__name__)


class CreateCollectionBody(BaseModel):
    name: str
    # 取り込み元フォルダ。source_folders（複数）か、旧版の API の source_folder（1つ）のどちらかを指定する。
    source_folders: list[str] | None = None
    source_folder: str | None = None
    description: str = ""
    recursive: bool = True
    # 取り込み除外キーワード（取り込み元フォルダより下のパスに含むフォルダ・ファイルは取り込まない）。
    exclude_keywords: list[str] = []
    # 取り込み対象キーワード（あれば、取り込み元フォルダより下のパスにどれかを含むファイルだけを取り込む）。
    include_keywords: list[str] = []
    schedule_enabled: bool = False
    schedule_times: list[str] = []
    schedule_weekdays: list[int] = []


class UpdateCollectionBody(BaseModel):
    source_folders: list[str] | None = None
    source_folder: str | None = None
    description: str | None = None
    recursive: bool | None = None
    exclude_keywords: list[str] | None = None
    include_keywords: list[str] | None = None
    schedule_enabled: bool | None = None
    schedule_times: list[str] | None = None
    schedule_weekdays: list[int] | None = None


class IngestBody(BaseModel):
    rebuild: bool = False


class CopyBody(BaseModel):
    dest_instance: str
    dest_name: str
    with_vectors: bool = True
    include_schedule: bool = False


class ReferenceBody(BaseModel):
    name: str
    source_instance: str
    source_collection: str


@dataclass
class IngestJob:
    """1コレクションの取り込み（またはコピー）のジョブの状態（画面のポーリング用）。"""

    collection: str
    rebuild: bool
    actor: str
    trigger: str = "manual"  # manual / schedule（時間指定の自動取り込み）
    kind: str = "ingest"  # ingest / copy（コピー先のコレクションのジョブ）
    source: str | None = None  # kind=copy のコピー元（"インスタンス名/コレクション名"）
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
    """(インスタンス名, コレクション名) ごとに最新の取り込みジョブを1件持つ。

    コピー中のコピー元のコレクション（copy_sources）は、コピーの途中で中身が変わらないよう取り込みを始めない。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], IngestJob] = {}
        self._copy_sources: dict[tuple[str, str], int] = {}

    def is_copy_source(self, instance: str, collection: str) -> bool:
        with self._lock:
            return self._copy_sources.get((instance, collection), 0) > 0

    def start_copy(self, instance: str, job: IngestJob, source_instance: str, source_collection: str) -> bool:
        """コピー先 (instance, job.collection) のジョブを登録し、コピー元を取り込み不可にする。

        コピー先・コピー元のどちらかが取り込み中なら False。
        """
        with self._lock:
            for key in ((instance, job.collection), (source_instance, source_collection)):
                current = self._jobs.get(key)
                if current is not None and current.state == "running":
                    return False
            self._jobs[(instance, job.collection)] = job
            key = (source_instance, source_collection)
            self._copy_sources[key] = self._copy_sources.get(key, 0) + 1
            return True

    def finish_copy(self, source_instance: str, source_collection: str) -> None:
        with self._lock:
            key = (source_instance, source_collection)
            count = self._copy_sources.get(key, 0) - 1
            if count > 0:
                self._copy_sources[key] = count
            else:
                self._copy_sources.pop(key, None)

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
            if self._copy_sources.get((instance, job.collection), 0) > 0:
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


def run_copy_job(
    src_cfg: Config,
    src_instance: str,
    src_name: str,
    dst_cfg: Config,
    job: IngestJob,
    jobs: _JobRegistry,
    *,
    with_vectors: bool,
    include_schedule: bool,
    store_transport=None,
) -> None:
    """コピーを実行し、結果・エラーを job に書き込む（スレッドの本体）。終わったらコピー元の取り込みを再び許す。"""

    def _progress(done: int, total: int, path: str) -> None:
        job.done, job.total, job.current = done, total, path

    try:
        job.result = copy_collection(
            src_cfg,
            src_name,
            dst_cfg,
            job.collection,
            with_vectors=with_vectors,
            include_schedule=include_schedule,
            progress=_progress,
            should_stop=lambda: job.stop_requested,
            src_store_transport=store_transport,
            dst_store_transport=store_transport,
        )
        job.state = "stopped" if job.result.get("stopped") else "done"
    except (CopyError, StoreError, vcat.CatalogError, OSError) as exc:
        job.error = str(exc)
        job.state = "error"
    except Exception as exc:  # noqa: BLE001 - 想定外の例外も画面に出して、ジョブが running のまま残らないようにする
        job.error = f"{type(exc).__name__}: {exc}"
        job.state = "error"
    finally:
        job.current = ""
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        jobs.finish_copy(src_instance, src_name)


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
                logger.exception("Failed to check scheduled knowledge ingestion.")

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
                logger.warning("Scheduled ingestion: cannot read the settings of instance %r: %s", name, exc)
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
                    logger.info("Scheduled ingestion: %s/%s is already ingesting or being copied, skipping this run.", name, c.name)
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
                logger.info("Scheduled ingestion: started diff ingestion of %s/%s.", name, c.name)
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
            raise HTTPException(status_code=400, detail=_t("スキル研究室のインスタンスにはナレッジを置けません。"))
        try:
            return loader(name)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=_t("インスタンス {0} の設定を読めません: {1}", name, exc)) from exc

    def _collection_dict(c: vcat.Collection, job: IngestJob | None, referenced_by: list[str] | None = None) -> dict:
        next_run = next_scheduled_run(c, datetime.now())
        return {
            **asdict(c),
            # 旧版の API（取り込み元フォルダが1つだけ）の利用者向けに、最初のフォルダも返す。
            "source_folder": c.source_folder,
            "next_scheduled_at": next_run.isoformat(timespec="seconds") if next_run else None,
            "job": job.to_dict() if job else None,
            "referenced_by": referenced_by or [],
        }

    def _app_instances() -> list[str]:
        """ナレッジを持てるインスタンス（スキル研究室を除く）の名前。"""
        names = []
        for n in inst.list_instance_names(instances_root):
            try:
                if not inst.read_instance(instances_root, n).is_skill_lab:
                    names.append(n)
            except (inst.InstanceError, OSError, ValueError):
                continue
        return names

    def _try_cfg(name: str) -> Config | None:
        try:
            return loader(name)
        except Exception:  # noqa: BLE001 - 設定の読めないインスタンスは一覧から外す
            logger.warning("Knowledge: cannot read the settings of instance %r.", name, exc_info=True)
            return None

    def _model_or_none(cfg: Config) -> str | None:
        try:
            return embedding_model(cfg)
        except EmbeddingError:
            return None

    def _source_check(owner: str, source_instance: str) -> str | None:
        """source_instance を owner の参照元にできなければ理由を返す。"""
        if source_instance == owner:
            return _t("自分自身のインスタンスは参照できません。")
        try:
            inst.validate_name(source_instance)
            meta = inst.read_instance(instances_root, source_instance)
        except inst.InstanceError:
            return _t("インスタンス {0} がありません。", source_instance)
        if meta.is_skill_lab:
            return _t("スキル研究室のインスタンスにはナレッジがありません。")
        return None

    def _reference_dict(owner: str, owner_cfg: Config, r: vcat.Reference) -> dict:
        """参照と、参照元のコレクションの状態（ok / incompatible / missing）。"""
        d = {**asdict(r), "status": "missing", "reason": None, "collection": None}
        reason = _source_check(owner, r.source_instance)
        src_cfg = None if reason else _try_cfg(r.source_instance)
        if reason is None and src_cfg is None:
            reason = _t("インスタンス {0} の設定を読めません。", r.source_instance)
        collection = None
        if src_cfg is not None and vcat.catalog_exists(src_cfg.vector_db_dir):
            with vcat.open_catalog(src_cfg.vector_db_dir) as catalog:
                collection = next((c for c in catalog.list_collections() if c.name == r.source_collection), None)
        if reason is None and collection is None:
            reason = _t("インスタンス {0} にコレクション {1} がありません。", r.source_instance, r.source_collection)
        if reason:
            d["reason"] = reason
            return d
        d["collection"] = asdict(collection)
        model = _model_or_none(owner_cfg)
        problems = []
        if not src_cfg.vector_db_enabled:
            problems.append(_t("参照元の [vector_db] enabled が false です"))
        if collection.model and collection.model != model:
            problems.append(_t("埋め込みモデルが違います（参照元 {0}、このインスタンス {1}）", collection.model, model))
        if collection.model and collection.document_prefix is not None and collection.document_prefix != owner_cfg.vector_db_document_prefix:
            problems.append(
                _t(
                    "document_prefix が違います（参照元 {0}、このインスタンス {1}）",
                    repr(collection.document_prefix),
                    repr(owner_cfg.vector_db_document_prefix),
                )
            )
        if problems:
            d["status"], d["reason"] = "incompatible", _t("、").join(problems)
        else:
            d["status"] = "ok"
        return d

    def _referenced_by(name: str) -> dict[str, list[str]]:
        """name のコレクションを参照している他のインスタンスの参照（コレクション名 → ["インスタンス/参照名", ...]）。"""
        result: dict[str, list[str]] = {}
        for other in _app_instances():
            if other == name:
                continue
            cfg = _try_cfg(other)
            if cfg is None or not vcat.catalog_exists(cfg.vector_db_dir):
                continue
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                refs = catalog.list_references()
            for r in refs:
                if r.source_instance == name:
                    result.setdefault(r.source_collection, []).append(f"{other}/{r.name}")
        return result

    @router.get("/api/instances/{name}/knowledge")
    def get_knowledge(name: str, user: str = Depends(require_login)):
        cfg = _cfg(name)
        collections = []
        refs = []
        if vcat.catalog_exists(cfg.vector_db_dir):
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                collections = catalog.list_collections()
                refs = catalog.list_references()
        running = jobs.for_instance(name)
        referenced_by = _referenced_by(name)
        names = {c.name for c in collections}
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
            "include_extensions": list(effective_extensions(cfg.vector_db_include_extensions)),
            "image_input": cfg.vector_db_image_input,
            "image_input_extensions": list(cfg.vector_db_image_input_extensions),
            "embedded_images": cfg.vector_db_embedded_images,
            "max_file_mb": cfg.vector_db_max_file_mb,
            "collections": [_collection_dict(c, running.get(c.name), referenced_by.get(c.name)) for c in collections],
            # コピーを始めた直後で、まだ catalog にできていないコピー先のジョブ・定義の作成で失敗したコピーのジョブ。
            "copy_jobs": [j.to_dict() for c, j in running.items() if j.kind == "copy" and c not in names],
            "references": [_reference_dict(name, cfg, r) for r in refs],
        }

    @router.get("/api/knowledge/sources")
    def get_knowledge_sources(user: str = Depends(require_login)):
        """コピー先・参照元に選べるインスタンスと、そのコレクションの一覧（埋め込みの設定と合わせて返す）。"""
        result = []
        for n in _app_instances():
            cfg = _try_cfg(n)
            if cfg is None:
                continue
            collections = []
            if vcat.catalog_exists(cfg.vector_db_dir):
                with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                    collections = catalog.list_collections()
            meta = inst.read_instance(instances_root, n)
            keys = ("name", "description", "model", "dim", "document_prefix", "file_count", "chunk_count")
            result.append(
                {
                    "name": n,
                    "display_name": meta.display_name,
                    "enabled": cfg.vector_db_enabled,
                    "backend": cfg.vector_db_backend,
                    "distance": cfg.vector_db_distance,
                    "embedding_model": _model_or_none(cfg),
                    "document_prefix": cfg.vector_db_document_prefix,
                    "collections": [{k: v for k, v in asdict(c).items() if k in keys} for c in collections],
                }
            )
        return {"instances": result}

    @router.post("/api/instances/{name}/knowledge/collections")
    def create_collection(name: str, body: CreateCollectionBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        folders = _body_folders(body.source_folders, body.source_folder)
        if folders is None:
            raise HTTPException(status_code=400, detail=_t("取り込み元フォルダを指定してください。"))
        folders = _check_folders(folders, body.recursive)
        try:
            times = vcat.normalize_schedule_times(body.schedule_times)
            weekdays = vcat.normalize_schedule_weekdays(body.schedule_weekdays)
            if body.schedule_enabled and not times:
                raise vcat.CatalogError(_t("自動取り込みを有効にするときは時刻を1つ以上指定してください。"))
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                catalog.create(body.name, folders, body.description, body.recursive, body.exclude_keywords, body.include_keywords)
                created = catalog.update(body.name, schedule_enabled=body.schedule_enabled, schedule_times=times, schedule_weekdays=weekdays)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(
            request,
            user,
            name,
            "knowledge_collection_create",
            collection=body.name,
            source_folders=list(created.source_folders),
            exclude_keywords=list(created.exclude_keywords),
            include_keywords=list(created.include_keywords),
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
        folders = _body_folders(body.source_folders, body.source_folder)
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                current = catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if folders is not None:
                folders = _check_folders(folders, body.recursive if body.recursive is not None else current.recursive)
            try:
                updated = catalog.update(
                    collection,
                    source_folders=folders,
                    description=body.description,
                    recursive=body.recursive,
                    exclude_keywords=body.exclude_keywords,
                    include_keywords=body.include_keywords,
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
            raise HTTPException(status_code=409, detail=_t("取り込み中のコレクションは削除できません。先に停止してください。"))
        if jobs.is_copy_source(name, collection):
            raise HTTPException(status_code=409, detail=_t("コピー中のコレクションは削除できません。コピーが終わってから削除してください。"))
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
                raise HTTPException(status_code=502, detail=_t("ベクターを削除できませんでした: {0}", exc)) from exc
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
            raise HTTPException(status_code=400, detail=_t("このインスタンスの [vector_db] enabled が false です。config.ini タブで有効にしてください。"))
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if jobs.is_copy_source(name, collection):
            raise HTTPException(status_code=409, detail=_t("このコレクションはコピー中です。コピーが終わってから取り込んでください。"))
        job = IngestJob(collection=collection, rebuild=body.rebuild, actor=user)
        if not jobs.start(name, job):
            raise HTTPException(status_code=409, detail=_t("このコレクションは取り込み中です。"))
        threading.Thread(
            target=run_ingest_job, args=(cfg, job), kwargs=extra_ingest_kwargs, name=f"knowledge-ingest-{name}-{collection}", daemon=True
        ).start()
        _audit(request, user, name, "knowledge_ingest_start", collection=collection, rebuild=body.rebuild)
        return job.to_dict()

    @router.post("/api/instances/{name}/knowledge/collections/{collection}/copy")
    def copy_collection_to(
        name: str, collection: str, body: CopyBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        src_cfg = _cfg(name)
        dst_cfg = _cfg(body.dest_instance)
        dest_name = body.dest_name.strip()
        with vcat.open_catalog(src_cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            vcat.validate_name(dest_name)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if vcat.catalog_exists(dst_cfg.vector_db_dir):
            with vcat.open_catalog(dst_cfg.vector_db_dir) as catalog:
                taken = {c.name for c in catalog.list_collections()} | {r.name for r in catalog.list_references()}
            if dest_name in taken:
                raise HTTPException(
                    status_code=400,
                    detail=_t("インスタンス {0} には {1} という名前のコレクション（または参照）が既にあります。別の名前を指定してください。", body.dest_instance, dest_name),
                )
        if body.with_vectors:
            reasons = check_compatible(src_cfg, collection, dst_cfg)
            if reasons:
                raise HTTPException(
                    status_code=400,
                    detail=_t("コピー先ではこのベクターを使えないため、ベクターごとはコピーできません（{0}）。定義だけをコピーしてコピー先で取り込んでください。", "; ".join(reasons)),
                )
        job = IngestJob(collection=dest_name, rebuild=False, actor=user, kind="copy", source=f"{name}/{collection}")
        if not jobs.start_copy(body.dest_instance, job, name, collection):
            raise HTTPException(status_code=409, detail=_t("コピー元かコピー先のコレクションが取り込み中です。終わってからコピーしてください。"))
        threading.Thread(
            target=run_copy_job,
            args=(src_cfg, name, collection, dst_cfg, job, jobs),
            kwargs={
                "with_vectors": body.with_vectors,
                "include_schedule": body.include_schedule,
                "store_transport": extra_ingest_kwargs.get("store_transport"),
            },
            name=f"knowledge-copy-{name}-{collection}",
            daemon=True,
        ).start()
        _audit(
            request,
            user,
            name,
            "knowledge_collection_copy",
            collection=collection,
            dest_instance=body.dest_instance,
            dest_name=dest_name,
            with_vectors=body.with_vectors,
            include_schedule=body.include_schedule,
        )
        return job.to_dict()

    @router.post("/api/instances/{name}/knowledge/references")
    def add_reference(name: str, body: ReferenceBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        reason = _source_check(name, body.source_instance)
        if reason:
            raise HTTPException(status_code=400, detail=reason)
        src_cfg = _cfg(body.source_instance)
        exists = False
        if vcat.catalog_exists(src_cfg.vector_db_dir):
            with vcat.open_catalog(src_cfg.vector_db_dir) as catalog:
                exists = any(c.name == body.source_collection for c in catalog.list_collections())
        if not exists:
            raise HTTPException(status_code=400, detail=_t("インスタンス {0} にコレクション {1} がありません。", body.source_instance, body.source_collection))
        try:
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                ref = catalog.add_reference(body.name.strip(), body.source_instance, body.source_collection)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(
            request, user, name, "knowledge_reference_add",
            reference=ref.name, source_instance=ref.source_instance, source_collection=ref.source_collection,
        )
        return _reference_dict(name, cfg, ref)

    @router.delete("/api/instances/{name}/knowledge/references/{reference}")
    def delete_reference(name: str, reference: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get_reference(reference)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            catalog.delete_reference(reference)
        _audit(request, user, name, "knowledge_reference_delete", reference=reference)
        return {"ok": True}

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
            raise HTTPException(status_code=409, detail=_t("取り込み中ではありません。"))
        job.stop_requested = True
        _audit(request, user, name, "knowledge_ingest_stop", collection=collection)
        return job.to_dict()

    return router


def _check_folder(value: str) -> Path:
    folder = Path(value.strip())
    if not value.strip() or not folder.is_absolute():
        raise HTTPException(status_code=400, detail=_t("取り込み元フォルダは絶対パスで指定してください。"))
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=_t("取り込み元フォルダがありません（管理ツールのPCから見えるパスを指定してください）: {0}", folder))
    return folder


def _body_folders(folders: list[str] | None, folder: str | None) -> list[str] | None:
    """リクエストの source_folders（複数）か source_folder（1つ・旧版の API）。どちらも無ければ None。"""
    if folders is not None:
        return folders
    return [folder] if folder is not None else None


def _check_folders(values: list[str], recursive: bool) -> list[str]:
    """取り込み元フォルダを検証し、重複を除いた絶対パスのリストにする。

    サブフォルダも含めるときは、別の取り込み元フォルダの中にあるフォルダを指定できない（同じファイルを2回たどるため）。
    """
    try:
        values = list(vcat.normalize_source_folders(values))
    except vcat.CatalogError as exc:
        raise HTTPException(status_code=400, detail=_t("取り込み元フォルダを指定してください。")) from exc
    folders = [_check_folder(v) for v in values]
    if recursive:
        keys = [os.path.normcase(os.path.normpath(f)) for f in folders]
        for i, inner in enumerate(keys):
            for j, outer in enumerate(keys):
                if i != j and inner.startswith(outer.rstrip("\\/") + os.sep):
                    raise HTTPException(
                        status_code=400,
                        detail=_t("取り込み元フォルダ {0} は {1} の中にあります（サブフォルダも含めるときは、外側のフォルダだけを指定してください）。", folders[i], folders[j]),
                    )
    return [str(f) for f in folders]
