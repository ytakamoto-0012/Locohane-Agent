"""研究テーマへ取り込める資産の一覧（対象インスタンスの中に限る）。

- 利用者のドラフト: そのインスタンスの [skill_creator] draft_dir（<ユーザー名>/<スキル名>/）
- 正式スキル: そのインスタンスが走査するもの（同梱 skills/・共有の project_locohane_dir・専用の instance_locohane_dir）
- 正式エージェント: 同じく agents/
- サブエージェントに割り当てられるツール名（src/tools/registry.py の _SUBAGENT_TOOLS）
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from pathlib import Path

from src.config import PROJECT_ROOT, Config

from . import themes

# ドラフトの状態のうち、取り込みの候補にしないもの（昇格済み・不採用）。
_CLOSED_DRAFT_STATUSES = frozenset({"promoted", "rejected"})


def origin_label(cfg: Config, path: Path) -> str:
    """正式資産の置き場の種類: builtin（同梱）/ instance（このインスタンス専用）/ shared（共有の project_locohane_dir）。"""
    resolved = path.resolve()
    if resolved.is_relative_to(cfg.instance_locohane_dir.resolve()):
        return "instance"
    if resolved.is_relative_to(cfg.skills_dir.resolve()) or resolved.is_relative_to(cfg.agents_dir.resolve()):
        return "builtin"
    return "shared"


def list_drafts(cfg: Config) -> list[dict]:
    root = cfg.skill_draft_dir
    if not root.is_dir():
        return []
    result = []
    for owner in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for skill in sorted(p for p in owner.iterdir() if p.is_dir() and not p.name.startswith("_")):
            if not (skill / "SKILL.md").is_file():
                continue
            meta_path = skill / "_draft_meta.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            except (OSError, ValueError):
                meta = {}
            status = meta.get("status", "draft")
            if status in _CLOSED_DRAFT_STATUSES:
                continue
            result.append(
                {
                    "owner": owner.name,
                    "name": skill.name,
                    "path": str(skill),
                    "status": status,
                    "kind": meta.get("kind"),
                    "updated_at": meta.get("updated_at"),
                    "returned_reason": meta.get("returned_reason"),
                    "lab_theme": (meta.get("lab") or {}).get("theme_id"),
                }
            )
    return result


def list_official_skills(cfg: Config) -> list[dict]:
    from src.skills import scan_skills

    return [
        {"name": s.name, "description": s.description, "path": str(s.dir_path), "origin": origin_label(cfg, s.dir_path)}
        for s in scan_skills([cfg.skills_dir, *cfg.locohane_skills_dirs])
    ]


def find_official_agent_file(cfg: Config, name: str) -> Path | None:
    """正式エージェント name の定義ファイル（後方のディレクトリが優先）。"""
    for root in reversed([cfg.agents_dir, *cfg.locohane_agents_dirs]):
        if (root / f"{name}.md").is_file():
            return root / f"{name}.md"
    return None


def list_official_agents(cfg: Config) -> list[dict]:
    from src.agent_types import scan_agent_types

    result = []
    for a in scan_agent_types([cfg.agents_dir, *cfg.locohane_agents_dirs]):
        path = find_official_agent_file(cfg, a.name)
        result.append(
            {
                "name": a.name,
                "description": a.description,
                "tools": a.tool_names,
                "path": str(path) if path else None,
                "origin": origin_label(cfg, path) if path else None,
            }
        )
    return result


def find_official_skill_dir(cfg: Config, name: str) -> Path | None:
    for root in reversed([cfg.skills_dir, *cfg.locohane_skills_dirs]):
        if (root / name / "SKILL.md").is_file():
            return root / name
    return None


@functools.lru_cache(maxsize=1)
def subagent_tool_names() -> tuple[str, ...]:
    """サブエージェントに割り当てられるツール名（frontmatter の tools に書けるもの）。

    src.tools は chainlit 等を読み込むため、管理ツールのプロセスには持ち込まず別プロセスで求める。
    """
    code = "import json; from src.tools import registry; print(json.dumps([t.name for t in registry._SUBAGENT_TOOLS]))"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    if out.returncode != 0:
        raise RuntimeError(f"Cannot get the list of tool names: {out.stderr.strip()[-500:]}")
    return tuple(json.loads(out.stdout.strip().splitlines()[-1]))


def imported_drafts(root: Path) -> dict[str, str]:
    """このインスタンスのテーマに取り込まれているドラフト（パス → テーマ ID、終わったテーマは除く）。"""
    result = {}
    for directory in themes.list_theme_dirs(root):
        data = themes.read_or_none(directory)
        if data is None:
            continue
        if data.get("status") in themes.CLOSED_STATUSES:
            continue
        for src in (data.get("sources") or {}).get("skills", {}).values():
            if src.get("kind") == "draft" and src.get("origin"):
                result[str(Path(src["origin"]).resolve())] = data["id"]
    return result
