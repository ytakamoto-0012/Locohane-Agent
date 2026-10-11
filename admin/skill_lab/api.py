"""スキル研究室の HTTP API（admin/server.py が build_router() で組み込む）。

テーマの操作はすべて対象インスタンスの下（/api/instances/{name}/skill-lab/...）にぶら下げる。
テーマ ID は対象インスタンスのテーマ置き場の中でだけ解決し、theme.json の instance とも照合するため、
他のインスタンスのテーマには触れられない。
"""

from __future__ import annotations

import base64
import difflib
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
import yaml

from evals.config_patch import ConfigPatchError
from .. import audit
from .. import instances as inst
from . import promotion, sources, themes

# アップロードできる入力ファイル1つの上限（バイト）。
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# 差分を表示するテキストファイルの上限（文字）。
_MAX_DIFF_CHARS = 200_000


class CreateThemeBody(BaseModel):
    title: str
    lab: str | None = None


class UpdateThemeBody(BaseModel):
    title: str | None = None
    lab: str | None = None


class AddAssetBody(BaseModel):
    # draft: 利用者のドラフトを取り込む / official: 正式資産を複製 / new: 内容を指定して新規 / ai: AI に下書きさせる
    mode: str
    asset_type: str
    name: str
    origin: str | None = None  # mode=draft のときドラフトのフォルダ
    content: str | None = None  # mode=new
    request: str | None = None  # mode=ai


class WriteFileBody(BaseModel):
    path: str
    content: str


class UploadBody(BaseModel):
    folder: str
    filename: str
    content_base64: str


class CaseBody(BaseModel):
    # evals/case_schema.py の項目（turns・expect・judge・work_dir・scripted_text_answers・auto_approve・timeout_seconds・notes 等）
    data: dict[str, Any]


class TaskBody(BaseModel):
    type: str
    params: dict[str, Any] = {}


class StartBody(BaseModel):
    mode: str = "loop"  # loop（スキル調整ループ）/ tryout（トライアウトだけ）


class PromoteBody(BaseModel):
    distribute_to: list[str] = []
    discard_source_changes: bool = False
    note: str = ""


class ReturnBody(BaseModel):
    reason: str
    apply_fixes: bool = False
    reject: bool = False


def build_router(
    *,
    require_login: Callable,
    require_csrf: Callable,
    instances_root: Path,
    supervisor,
    audit_log_path: Path,
    client_ip: Callable[[Request], str],
    backup_keep: int,
) -> APIRouter:
    router = APIRouter()

    def _audit(request: Request, user: str, instance: str, action: str, **extra) -> None:
        audit.append(audit_log_path, {"instance": instance, "actor": user, "remote_addr": client_ip(request), "action": action, **extra})

    def _app_instance(name: str):
        try:
            inst.validate_name(name)
            meta = inst.read_instance(instances_root, name)
        except inst.InstanceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if meta.is_skill_lab:
            raise HTTPException(status_code=400, detail="スキル研究室のインスタンスには研究テーマを置けません。")
        return meta

    def _cfg(name: str):
        try:
            return promotion.instance_config(instances_root, name)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"インスタンス {name} の設定を読めません: {exc}") from exc

    def _root(name: str) -> Path:
        return themes.themes_root(_cfg(name).common_data_dir)

    def _theme(name: str, theme_id: str) -> Path:
        _app_instance(name)
        try:
            directory = themes.theme_dir(_root(name), theme_id)
        except themes.ThemeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if themes.read(directory).get("instance") != name:
            raise HTTPException(status_code=404, detail="このインスタンスのテーマではありません。")
        return directory

    def _labs() -> list[dict]:
        result = []
        for n in inst.list_instance_names(instances_root):
            meta = inst.read_instance(instances_root, n)
            if meta.is_skill_lab:
                result.append({"name": n, "display_name": meta.display_name, "state": supervisor.status(n).state.value})
        return result

    def _lab_workload(labs: list[dict]) -> None:
        """各 スキル調整ワーカーの、処理中のテーマ・待ち件数・LLM 接続先を labs に足す（カード表示用）。"""
        sole = labs[0]["name"] if len(labs) == 1 else None
        by_name = {x["name"]: {**x, "running": [], "queued": 0, "tasks": 0} for x in labs}
        for n in inst.list_instance_names(instances_root):
            if inst.read_instance(instances_root, n).is_skill_lab:
                continue
            try:
                root = themes.themes_root(promotion.instance_config(instances_root, n).common_data_dir)
            except Exception:  # noqa: BLE001 - 設定エラーのインスタンスは数えない
                continue
            for d in themes.list_theme_dirs(root):
                data = themes.read(d)
                row = by_name.get(data.get("lab") or sole)
                if row is None:
                    continue
                if data.get("status") == themes.STATUS_RUNNING:
                    row["running"].append({"instance": n, "id": data["id"], "title": data["title"]})
                elif data.get("status") == themes.STATUS_QUEUED:
                    row["queued"] += 1
                row["tasks"] += sum(1 for t in data.get("tasks") or [] if t.get("status") in ("queued", "running"))
        for lab in labs:
            row = by_name[lab["name"]]
            try:
                cfg = promotion.instance_config(instances_root, lab["name"])
                row["endpoint"] = cfg.main_endpoints[0].base_url if cfg.main_endpoints else None
            except Exception as exc:  # noqa: BLE001
                row["endpoint_error"] = str(exc)
            lab.update(row)

    def _check_lab(lab: str | None) -> None:
        if lab and lab not in {x["name"] for x in _labs()}:
            raise HTTPException(status_code=400, detail=f"スキル研究室 {lab} が見つかりません。")

    def _guard_editable(directory: Path) -> dict:
        data = themes.read(directory)
        if data.get("status") in themes.CLOSED_STATUSES:
            raise HTTPException(status_code=409, detail=f"このテーマは終了しています（{data.get('status')}）。")
        if data.get("status") in themes.ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="ループ・トライアウトの実行中は編集できません。先に停止してください。")
        return data

    def _summary(directory: Path) -> dict:
        data = themes.read(directory)
        iterations = data.get("iterations") or []
        last = iterations[-1] if iterations else None
        return {
            "id": data["id"],
            "title": data["title"],
            "status": data["status"],
            "lab": data.get("lab"),
            "updated_at": data.get("updated_at"),
            "created_by": data.get("created_by"),
            "assets": themes.list_assets(directory),
            "cases": len(list((directory / themes.CASES_DIRNAME).glob("*.yaml"))),
            "iterations": len(iterations),
            "last": {"n": last["n"], "phase": last["phase"], "verdict": last["verdict"], "cases": last["cases"]} if last else None,
            "message": (data.get("loop") or {}).get("message"),
        }

    # --- 一覧 ---------------------------------------------------------------

    @router.get("/api/skill-lab/overview")
    def overview(user: str = Depends(require_login)):
        rows = []
        for n in inst.list_instance_names(instances_root):
            meta = inst.read_instance(instances_root, n)
            if meta.is_skill_lab:
                continue
            row = {"name": n, "display_name": meta.display_name, "state": supervisor.status(n).state.value}
            try:
                cfg = promotion.instance_config(instances_root, n)
                dirs = themes.list_theme_dirs(themes.themes_root(cfg.common_data_dir))
                statuses = [themes.read(d).get("status") for d in dirs]
                row["counts"] = {
                    "drafts": len(sources.list_drafts(cfg)),
                    "open": sum(1 for s in statuses if s not in themes.CLOSED_STATUSES),
                    "active": sum(1 for s in statuses if s in themes.ACTIVE_STATUSES),
                    "review": statuses.count(themes.STATUS_REVIEW),
                }
            except Exception as exc:  # noqa: BLE001 - 1インスタンスの設定エラーで一覧全体を止めない
                row["error"] = str(exc)
            rows.append(row)
        labs = _labs()
        _lab_workload(labs)
        return {"instances": rows, "labs": labs}

    @router.get("/api/instances/{name}/skill-lab/sources")
    def get_sources(name: str, user: str = Depends(require_login)):
        _app_instance(name)
        cfg = _cfg(name)
        imported = sources.imported_drafts(themes.themes_root(cfg.common_data_dir))
        drafts = sources.list_drafts(cfg)
        for d in drafts:
            d["imported_theme"] = imported.get(str(Path(d["path"]).resolve()))
        try:
            tools = list(sources.subagent_tool_names())
        except RuntimeError as exc:
            tools, tools_error = [], str(exc)
        else:
            tools_error = None
        return {
            "drafts": drafts,
            "skills": sources.list_official_skills(cfg),
            "agents": sources.list_official_agents(cfg),
            "tools": tools,
            "tools_error": tools_error,
            "instance_locohane_dir": str(cfg.instance_locohane_dir),
        }

    @router.get("/api/instances/{name}/skill-lab/themes")
    def list_themes(name: str, user: str = Depends(require_login)):
        _app_instance(name)
        return {"themes": [_summary(d) for d in reversed(themes.list_theme_dirs(_root(name)))], "labs": _labs()}

    @router.post("/api/instances/{name}/skill-lab/themes")
    def create_theme(name: str, body: CreateThemeBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        _app_instance(name)
        _check_lab(body.lab)
        try:
            directory = themes.create_theme(_root(name), instance=name, title=body.title, lab=body.lab, actor=user)
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_theme_create", theme=directory.name, title=body.title)
        return _summary(directory)

    @router.get("/api/instances/{name}/skill-lab/themes/{theme_id}")
    def get_theme(name: str, theme_id: str, user: str = Depends(require_login)):
        directory = _theme(name, theme_id)
        data = themes.read(directory)
        return {
            **data,
            "summary": _summary(directory),
            "files": themes.list_files(directory),
            "cases_list": themes.list_cases(directory),
            "fixtures": themes.list_fixtures(directory),
            "tryout_problems": themes.tryout_matches(directory, data.get("tryout")) if data.get("tryout") else None,
            "labs": _labs(),
            "start_missing": themes.start_missing(directory),
        }

    @router.put("/api/instances/{name}/skill-lab/themes/{theme_id}")
    def update_theme(name: str, theme_id: str, body: UpdateThemeBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        _check_lab(body.lab)
        if body.lab is not None and themes.read(directory).get("status") in themes.ACTIVE_STATUSES:
            # 実行中に担当を替えると、元のワーカーが続けている間に新しいワーカーも同じテーマを拾ってしまう。
            raise HTTPException(status_code=409, detail="ループ・トライアウトの実行中・開始待ちの間は担当のワーカーを変えられません。先に停止してください。")

        def _apply(d: dict) -> None:
            if body.title is not None and body.title.strip():
                d["title"] = body.title.strip()
            if body.lab is not None:
                d["lab"] = body.lab or None
            themes.add_history(d, user, "update", f"title={d['title']} lab={d.get('lab')}")

        themes.update(directory, _apply)
        _audit(request, user, name, "skill_lab_theme_update", theme=theme_id)
        return _summary(directory)

    # --- 資産 ---------------------------------------------------------------

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/assets")
    def add_asset(name: str, theme_id: str, body: AddAssetBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        _guard_editable(directory)
        cfg = _cfg(name)
        try:
            if body.mode == "draft":
                if body.asset_type != "skills" or not body.origin:
                    raise themes.ThemeError("ドラフトの取り込みにはドラフトのフォルダを指定してください。")
                origin = Path(body.origin).resolve()
                draft_root = cfg.skill_draft_dir.resolve()
                if not origin.is_relative_to(draft_root) or len(origin.relative_to(draft_root).parts) != 2:
                    raise themes.ThemeError("このインスタンスのドラフトではありません。")
                owner = origin.parent.name
                themes.add_asset_copy(
                    directory, "skills", origin.name, origin, source={"kind": "draft", "origin": str(origin), "owner": owner}, actor=user
                )
                promotion.mark_imported(origin, theme_id)
                # ドラフトのケースも取り込む（テーマのケースとして直せる）。
                for case in sorted((origin / "evals").glob("*.yaml")) if (origin / "evals").is_dir() else []:
                    dest = directory / themes.CASES_DIRNAME / f"{origin.name}_{case.name}"
                    if not dest.exists():
                        text = case.read_text(encoding="utf-8")
                        try:
                            data = yaml.safe_load(text)
                            if isinstance(data, dict):
                                data["id"] = dest.stem
                                text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
                        except Exception:  # noqa: BLE001 - 読めないケースは写さない
                            continue
                        if themes.validate_case_yaml(text, dest.stem) is None:
                            dest.write_text(text, encoding="utf-8")
            elif body.mode == "official":
                origin = promotion.official_sources(cfg, body.asset_type, body.name)
                if origin is None:
                    raise themes.ThemeError(f"正式の {body.name} が見つかりません。")
                source = {"kind": "official", "origin": str(origin), "origin_label": sources.origin_label(cfg, origin)}
                themes.add_asset_copy(directory, body.asset_type, body.name, origin, source=source, actor=user)
            elif body.mode == "new":
                themes.add_asset_new(directory, body.asset_type, body.name, body.content or "", actor=user)
            elif body.mode == "ai":
                themes.asset_path(directory, body.asset_type, body.name)
                if not (body.request or "").strip():
                    raise themes.ThemeError("何をさせたいかを書いてください。")
                task_id = themes.add_task(
                    directory, "draft_asset", {"asset_type": body.asset_type, "name": body.name, "request": body.request}, actor=user
                )
                _audit(request, user, name, "skill_lab_task", theme=theme_id, task="draft_asset", asset=body.name)
                return {"task_id": task_id}
            else:
                raise themes.ThemeError(f"不明な追加方法です: {body.mode}")
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_asset_add", theme=theme_id, mode=body.mode, asset=f"{body.asset_type}/{body.name}")
        return {"success": True}

    @router.delete("/api/instances/{name}/skill-lab/themes/{theme_id}/assets/{asset_type}/{asset_name}")
    def remove_asset(
        name: str, theme_id: str, asset_type: str, asset_name: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        directory = _theme(name, theme_id)
        _guard_editable(directory)
        try:
            themes.remove_asset(directory, asset_type, asset_name, actor=user)
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_asset_remove", theme=theme_id, asset=f"{asset_type}/{asset_name}")
        return {"success": True}

    # --- ファイル -------------------------------------------------------------

    @router.get("/api/instances/{name}/skill-lab/themes/{theme_id}/files")
    def read_file(name: str, theme_id: str, path: str = Query(...), user: str = Depends(require_login)):
        directory = _theme(name, theme_id)
        try:
            return {"path": path, "content": themes.read_text(directory, path)}
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/api/instances/{name}/skill-lab/themes/{theme_id}/files")
    def write_file(name: str, theme_id: str, body: WriteFileBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        _guard_editable(directory)
        try:
            themes.write_text(directory, body.path, body.content, actor=user)
        except (themes.ThemeError, ConfigPatchError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_file_write", theme=theme_id, path=body.path)
        return {"success": True}

    @router.delete("/api/instances/{name}/skill-lab/themes/{theme_id}/files")
    def delete_file(name: str, theme_id: str, request: Request, path: str = Query(...), user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        _guard_editable(directory)
        try:
            themes.delete_path(directory, path, actor=user)
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_file_delete", theme=theme_id, path=path)
        return {"success": True}

    @router.get("/api/instances/{name}/skill-lab/themes/{theme_id}/cases/{case_id}")
    def read_case(name: str, theme_id: str, case_id: str, user: str = Depends(require_login)):
        """ケース1件をフォーム用の構造（yaml を読んだ dict）で返す。"""
        directory = _theme(name, theme_id)
        try:
            text = themes.read_text(directory, f"{themes.CASES_DIRNAME}/{case_id}.yaml")
            data = yaml.safe_load(text)
        except (themes.ThemeError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": case_id, "data": data if isinstance(data, dict) else {}}

    @router.put("/api/instances/{name}/skill-lab/themes/{theme_id}/cases/{case_id}")
    def write_case(
        name: str, theme_id: str, case_id: str, body: CaseBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        """フォームで作ったケースを yaml にして保存する（id・target はここで決める）。"""
        directory = _theme(name, theme_id)
        _guard_editable(directory)
        data = {"id": case_id, "target": "skill_lab", **{k: v for k, v in body.data.items() if k not in ("id", "target")}}
        data = {k: v for k, v in data.items() if v not in (None, "", [], {})}
        content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        try:
            themes.write_text(directory, f"{themes.CASES_DIRNAME}/{case_id}.yaml", content, actor=user)
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_case_write", theme=theme_id, case=case_id)
        return {"success": True}

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/fixtures")
    def upload_fixture(name: str, theme_id: str, body: UploadBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        _guard_editable(directory)
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="ファイルの内容を読めません。") from exc
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail=f"ファイルが大きすぎます（上限 {_MAX_UPLOAD_BYTES // 1024 // 1024}MB）。")
        rel = f"{themes.CASES_DIRNAME}/{themes.FIXTURES_DIRNAME}/{body.folder.strip()}/{Path(body.filename).name}"
        try:
            themes.write_bytes(directory, rel, content, actor=user)
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_fixture_upload", theme=theme_id, path=rel, size=len(content))
        return {"path": rel, "work_dir": f"{themes.FIXTURES_DIRNAME}/{body.folder.strip()}"}

    # --- タスク・ループ ---------------------------------------------------------

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/tasks")
    def add_task(name: str, theme_id: str, body: TaskBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        data = themes.read(directory)
        if data.get("status") in themes.CLOSED_STATUSES:
            raise HTTPException(status_code=409, detail="このテーマは終了しています。")
        if body.type != "trial" and data.get("status") in themes.ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="ループ・トライアウトの実行中は下書きを頼めません。")
        if body.type == "trial" and body.params.get("case_id") not in {c["id"] for c in themes.list_cases(directory)}:
            raise HTTPException(status_code=400, detail="試すケースを選んでください。")
        try:
            task_id = themes.add_task(directory, body.type, body.params, actor=user)
        except themes.ThemeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_task", theme=theme_id, task=body.type)
        return {"task_id": task_id, "lab_running": _lab_running(data)}

    def _lab_running(data: dict) -> bool:
        labs = _labs()
        lab = data.get("lab") or (labs[0]["name"] if len(labs) == 1 else None)
        return any(x["name"] == lab and x["state"] in ("running", "external") for x in labs)

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/start")
    def start(name: str, theme_id: str, body: StartBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        data = _guard_editable(directory)
        if body.mode not in ("loop", "tryout"):
            raise HTTPException(status_code=400, detail="mode は loop か tryout にしてください。")
        missing = themes.start_missing(directory)
        if missing:
            raise HTTPException(status_code=400, detail="開始するには次を入れてください: " + "、".join(missing))
        labs = _labs()
        if not data.get("lab") and len(labs) != 1:
            raise HTTPException(status_code=400, detail="担当のスキル研究室を選んでください。")

        def _apply(d: dict) -> None:
            d["status"] = themes.STATUS_QUEUED
            d["loop"] = {"mode": body.mode, "queued_at": themes.now_iso(), "stop_requested": False}
            themes.add_history(d, user, "start", body.mode)

        themes.update(directory, _apply)
        _audit(request, user, name, "skill_lab_start", theme=theme_id, mode=body.mode)
        return {"success": True, "lab_running": _lab_running(data)}

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/stop")
    def stop(name: str, theme_id: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)

        def _apply(d: dict) -> None:
            if d.get("status") == themes.STATUS_QUEUED:
                d["status"] = themes.STATUS_STOPPED
                if d.get("loop"):
                    d["loop"]["deadline"] = None
            elif d.get("status") == themes.STATUS_RUNNING and d.get("loop"):
                d["loop"]["stop_requested"] = True
            themes.add_history(d, user, "stop")

        themes.update(directory, _apply)
        _audit(request, user, name, "skill_lab_stop", theme=theme_id)
        return {"success": True}

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/cancel")
    def cancel(name: str, theme_id: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        _guard_editable(directory)

        def _apply(d: dict) -> None:
            d["status"] = themes.STATUS_CANCELLED
            themes.add_history(d, user, "cancel")

        themes.update(directory, _apply)
        promotion.unmark_imported(directory)
        _audit(request, user, name, "skill_lab_cancel", theme=theme_id)
        return {"success": True}

    # --- 実行結果・差分 -----------------------------------------------------------

    @router.get("/api/instances/{name}/skill-lab/themes/{theme_id}/iterations/{n}")
    def get_iteration(name: str, theme_id: str, n: int, user: str = Depends(require_login)):
        directory = _theme(name, theme_id)
        data = themes.read(directory)
        it = next((x for x in data.get("iterations") or [] if x["n"] == n), None)
        if it is None:
            raise HTTPException(status_code=404, detail="反復が見つかりません。")
        return {**it, "runs": _runs(directory, it.get("out_dir"), it.get("verdicts") or [])}

    @router.get("/api/instances/{name}/skill-lab/themes/{theme_id}/trial/{task_id}")
    def get_trial(name: str, theme_id: str, task_id: str, user: str = Depends(require_login)):
        directory = _theme(name, theme_id)
        task = next((t for t in themes.read(directory).get("tasks") or [] if t["id"] == task_id), None)
        if task is None:
            raise HTTPException(status_code=404, detail="タスクが見つかりません。")
        return task

    def _runs(directory: Path, out_dir: str | None, verdicts: list[dict]) -> list[dict]:
        if not out_dir:
            return []
        path = Path(out_dir).resolve()
        if not path.is_relative_to(directory.resolve()) or not (path / "results.json").is_file():
            return []
        from . import evaluation

        results = json.loads((path / "results.json").read_text(encoding="utf-8"))
        by_key = {(v["case_id"], v["repeat_index"]): v for v in verdicts}
        rows = []
        for r in results:
            v = by_key.get((r.get("case_id"), r.get("repeat_index", 1)), {})
            rows.append(
                {
                    "case_id": r.get("case_id"),
                    "repeat_index": r.get("repeat_index", 1),
                    "outcome": v.get("outcome", evaluation.classify(r)),
                    "ai_judge": v.get("ai_judge"),
                    "failed_rules": {k: d for k, d in (r.get("rule_results") or {}).items() if not d.get("pass")},
                    "error": r.get("error"),
                    "detail": r.get("detail"),
                    "final_answer": (r.get("final_answer") or "")[:4000],
                    "excerpt": evaluation.transcript_excerpt(r, 12000),
                }
            )
        return rows

    @router.get("/api/instances/{name}/skill-lab/themes/{theme_id}/diff")
    def get_diff(name: str, theme_id: str, user: str = Depends(require_login)):
        """取り込み・複製した時点（新規は空）からの変更。scripts/・ケースの変更には印を付ける。"""
        directory = _theme(name, theme_id)
        files = []
        current = {f["path"]: f for f in themes.list_files(directory) if f["path"].startswith(f"{themes.ASSETS_DIRNAME}/")}
        baseline_root = directory / themes.BASELINE_DIRNAME
        baseline = {}
        if baseline_root.is_dir():
            for p in baseline_root.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    baseline[p.relative_to(baseline_root).as_posix()] = p
        for rel in sorted(set(current) | set(baseline)):
            new_path = directory / rel
            old_text = _text_or_none(baseline.get(rel))
            new_text = _text_or_none(new_path if new_path.is_file() else None)
            if old_text == new_text:
                continue
            files.append(
                {
                    "path": rel,
                    "status": "added" if old_text is None else ("deleted" if new_text is None else "modified"),
                    "important": "/scripts/" in rel,
                    "diff": "".join(
                        difflib.unified_diff(
                            (old_text or "").splitlines(keepends=True),
                            (new_text or "").splitlines(keepends=True),
                            fromfile=f"元/{rel}",
                            tofile=f"今/{rel}",
                        )
                    )[:_MAX_DIFF_CHARS],
                }
            )
        data = themes.read(directory)
        case_changes = [h for h in data.get("history") or [] if h.get("action") == "change" and "cases/" in (h.get("detail") or "")]
        ai_case_changes = [h for h in case_changes if str(h.get("actor", "")).startswith("AI")]
        return {"files": files, "case_changes": case_changes[-50:], "ai_changed_cases": bool(ai_case_changes)}

    # --- 昇格・差し戻し ------------------------------------------------------------

    @router.get("/api/instances/{name}/skill-lab/themes/{theme_id}/promote-check")
    def promote_check(name: str, theme_id: str, distribute_to: list[str] = Query(default=[]), user: str = Depends(require_login)):
        directory = _theme(name, theme_id)
        result = promotion.check(instances_root, directory, distribute_to=distribute_to)
        others = [n for n in inst.list_instance_names(instances_root) if n != name and not inst.read_instance(instances_root, n).is_skill_lab]
        return {**result.to_json(), "distributable": others}

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/promote")
    def promote(name: str, theme_id: str, body: PromoteBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        try:
            record = promotion.promote(
                instances_root,
                directory,
                distribute_to=body.distribute_to,
                discard_source_changes=body.discard_source_changes,
                note=body.note,
                actor=user,
                remote_addr=client_ip(request),
                backup_keep=backup_keep,
            )
        except promotion.PromotionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_promote", theme=theme_id, instances=record["instances"], archived=record["archived_drafts"])
        restart = [n for n in record["instances"] if supervisor.status(n).state.value in ("running", "external")]
        return {**record, "restart_needed": restart}

    @router.post("/api/instances/{name}/skill-lab/themes/{theme_id}/return")
    def return_theme(name: str, theme_id: str, body: ReturnBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        directory = _theme(name, theme_id)
        try:
            result = promotion.return_to_creators(directory, reason=body.reason, apply_fixes=body.apply_fixes, reject=body.reject, actor=user)
        except promotion.PromotionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "skill_lab_reject" if body.reject else "skill_lab_return", theme=theme_id, drafts=result["drafts"])
        return result

    return router


def _text_or_none(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"（バイナリファイル {path.stat().st_size} バイト）"
