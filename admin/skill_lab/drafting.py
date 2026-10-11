"""研究室の LLM による下書き（仕様・ケース・新しいスキル／サブエージェント）。

下書きは人が画面で確かめて直す前提。書き込みはテーマのファイル操作（themes.py）を通し、
形式の検証に通らないものは保存しない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from src.config import PROJECT_ROOT, Config

from . import themes
from .fixer import theme_context
from .llm import LabLLMError, ask_json, ask_text

_SPEC_SYSTEM = """あなたは業務用AIアシスタントのスキル・サブエージェントの仕様をまとめる担当です。
仕様は、研究対象（このテーマで直す・作るスキルとサブエージェント）が何をすべきかを決める採点の基準になります。
研究対象の一覧と中身（assets/ 配下の SKILL.md・scripts/・references/・サブエージェント定義）を読み、
それが何のためのもので、どんな業務・状況で使い、何を出力し、何をしてはいけないかを読み取って書きます。
- 研究対象が今使っている正式なものの複製なら、今の動きを基本にし、直したい点は依頼者の補足に従う。
- AI が作成中の研究対象は、その注文を作りたいものとして扱う。
- 依頼者の補足があれば、研究対象から読み取った内容より優先する。
- 今の仕様に人が書いた内容（「要確認:」以外）があれば、それを残し、空いている所だけを埋める。
次の見出しを持つ Markdown の仕様書だけを出力してください（前置き不要）:
# <テーマ名>
## 目的
## 使う場面
## 期待する出力
## やってはいけないこと
「使う場面」にはどんな業務・状況で使うかを書き、利用者が打つ具体的な指示文は書かない（それは試験問題に書く）。
推測で機能を足さず、資料から読み取れることだけを書く。分からない点は「要確認:」として書く。"""

_CASES_SYSTEM = """あなたは業務用AIアシスタントの動作検証ケースを作る担当です。
アシスタントはローカルの小さめのLLMで動くため、ユーザーの指示（turns）は実際の利用者が打つような短く端的な日本語にします。
ケースは、研究対象（assets/ 配下のスキル・サブエージェント）が仕様（spec.md）のとおりに動くかを確かめる試験問題です。
- 仕様の「目的」「使う場面」から、利用者が実際に打ちそうな指示を作る。
- 仕様の「期待する出力」を合格の条件（response_contains・judge）に、「やってはいけないこと」を不合格の条件
  （response_not_contains・tool_not_called・judge）にする。
- スキル名・サブエージェント名・ツール名は、研究対象の中身に実際にあるものだけを使う。
- 仕様に無い機能や「要確認:」の点は確かめない。既にあるケースと同じことを確かめるケースは作らない。
1件のケースで複数のスキル・サブエージェントを使う流れを確かめてもかまいません。
次の形式の JSON 配列だけを出力してください:
[{"id": "001_basic", "turns": ["ユーザーの指示"],
  "expect": {"tool_called_any": [...], "tool_not_called": [...], "tool_call_args_contains": {"ツール名": {"引数": "値"}}, "response_contains": [...], "response_not_contains": [...]},
  "judge": "結果を判定する観点（任意。客観的に決まらない点だけ）",
  "work_dir": "fixtures/<入力ファイルのフォルダ>（入力ファイルが要るときだけ）",
  "notes": "このケースで確かめること"}]
expect のキーは使うものだけ書く。スキルを読んだかは {"tool_call_args_contains": {"read_skill": {"skill_name": "<スキル名>"}}}、
サブエージェントへの委譲は {"tool_call_args_contains": {"dispatch_agent": {"agent_type": "<名前>"}}} で確かめられる。
expect と judge のどちらか一方は必ず書く。id は英数字・ハイフン・アンダースコアのみ。"""

_SKILL_SYSTEM = """あなたは業務用AIアシスタントのスキル（SKILL.md）を書く担当です。
SKILL.md は先頭に frontmatter（name・description）を持ち、本文に手順を書きます。
- description が唯一のトリガー手がかり。「何をするか」と「どんな指示のときに使うか」を具体的に書く（1024文字以内）。
- 本文は500行以内。スクリプトを使う場合は呼び出し例を `python <script>.py <args...>` の形で書き、出力の意味も書く。
- ツール名（run_script 等）は本文に書かない。
SKILL.md の全文だけを出力してください（前置き・コードフェンス不要）。"""

_AGENT_SYSTEM = """あなたは業務用AIアシスタントのサブエージェント（agents/<名前>.md）を書く担当です。
サブエージェントは、メインのアシスタントから dispatch_agent で仕事を委譲されて動く別のエージェントです。
ファイルは先頭に frontmatter（name・description・tools）を持ち、本文がそのサブエージェントのシステムプロンプトになります。
- description はメインのアシスタントが委譲先を選ぶ手がかり。何を任せるエージェントかを具体的に書く。
- tools は与えられたツール名の一覧からだけ、必要最小限をカンマ区切りで選ぶ。
- 本文には役割・手順・最終回答の形・禁止事項を書き、スキル一覧を差し込む位置に {{skills}}、
  run_script で実行できるスキルの一覧を差し込む位置に {{run_script_allowlist}} を書く。
次の形式の JSON だけを出力してください:
{"content": "ファイルの全文", "allowlist": [["<この名前>", "<run_scriptで呼んでよいスキル名>"], ...]}
allowlist は run_script で呼ばせたいスキルがある場合だけ書く（無ければ空配列）。"""


def targets_brief(directory: Path) -> str:
    """研究対象の一覧（入手元と、新規に作ったものの注文。AI が作成中のものも含む）。"""
    data = themes.read(directory)
    sources = data.get("sources") or {}
    labels = {"skills": "スキル", "agents": "サブエージェント"}
    lines = []
    for asset_type, names in themes.list_assets(directory).items():
        for name in names:
            src = (sources.get(asset_type) or {}).get(name) or {}
            kind = src.get("kind")
            if kind == "draft":
                origin = f"利用者 {src.get('owner') or ''} が試作したドラフトを仕上げるもの"
            elif kind == "official":
                origin = "今使っている正式なものを改善するために複製したもの"
            else:
                origin = "このテーマで新しく作ったもの"
            if src.get("description"):
                origin += f"（注文: {src['description']}）"
            lines.append(f"- {labels[asset_type]} {name}: {origin}")
    for task in data.get("tasks") or []:
        params = task.get("params") or {}
        if task.get("type") == "draft_asset" and task.get("status") in ("queued", "running"):
            lines.append(f"- {labels.get(params.get('asset_type'), '')} {params.get('name')}: AI が作成中（注文: {params.get('request', '')}）")
    return "\n".join(lines) or "（まだありません）"


def draft_spec(config: Config, directory: Path, *, max_file_chars: int, request: str = "") -> None:
    data = themes.read(directory)
    user = (
        f"テーマ名: {data.get('title')}\n\n## 研究対象の一覧\n{targets_brief(directory)}\n\n"
        f"{theme_context(directory, max_file_chars, include_cases=False)}"
    )
    if request:
        user += f"\n\n## 依頼者からの補足\n{request}"
    text = ask_text(config, _SPEC_SYSTEM, user)
    themes.write_text(directory, themes.SPEC_FILENAME, text.strip() + "\n", actor="AI（下書き）")


def draft_cases(config: Config, directory: Path, *, max_file_chars: int, count: int, request: str = "") -> list[str]:
    existing = {c["id"] for c in themes.list_cases(directory)}
    user = (
        f"## 研究対象の一覧\n{targets_brief(directory)}\n\n"
        f"{theme_context(directory, max_file_chars)}\n\n"
        f"既にあるケース ID: {sorted(existing) or 'なし'}\n"
        f"使える入力ファイルのフォルダ（work_dir に書ける値）: {themes.list_fixtures(directory) or 'なし'}\n"
        f"ケースを {count} 件作ってください。"
    )
    if request:
        user += f"\n\n## 依頼者からの補足\n{request}"
    items = ask_json(config, _CASES_SYSTEM, user)
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise LabLLMError(f"ケースの応答の形式が不正です: {str(items)[:300]}")
    created, errors = [], []
    for item in items[: max(1, count)]:
        if not isinstance(item, dict):
            continue
        case_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(item.get("id") or "")).strip("_-")[:48]
        n = 1
        base_id = case_id or "case"
        while not case_id or case_id in existing:
            case_id = f"{base_id}_{n}"
            n += 1
        turns = item.get("turns") or []
        if isinstance(turns, str):
            turns = [turns]
        case = {"id": case_id, "target": "skill_lab", "turns": turns}
        for key in ("expect", "judge", "work_dir", "notes"):
            if item.get(key):
                case[key] = item[key]
        content = yaml.safe_dump(case, allow_unicode=True, sort_keys=False)
        try:
            themes.write_text(directory, f"{themes.CASES_DIRNAME}/{case_id}.yaml", content, actor="AI（下書き）")
        except themes.ThemeError as e:
            errors.append(str(e))
            continue
        existing.add(case_id)
        created.append(case_id)
    if not created:
        raise LabLLMError("保存できるケースがありませんでした: " + " / ".join(errors))
    return created


def draft_asset(
    config: Config,
    directory: Path,
    *,
    asset_type: str,
    name: str,
    request: str,
    known_tools: list[str],
    skills: list[dict],
    max_file_chars: int,
) -> None:
    """新しいスキル・サブエージェントを下書きして資産に加える。"""
    themes.asset_path(directory, asset_type, name)  # 名前の検証
    spec = (directory / themes.SPEC_FILENAME).read_text(encoding="utf-8")[:max_file_chars]
    if asset_type == "skills":
        user = f"スキル名（name とフォルダ名）: {name}\n\n## 作りたいもの\n{request}\n\n## テーマの仕様\n{spec}"
        content = _strip_fence(ask_text(config, _SKILL_SYSTEM, user))
        themes.add_asset_new(directory, "skills", name, content, actor="AI（下書き）", description=request)
        return
    example = _agent_example()
    skill_lines = "\n".join(f"- {s['name']}: {s['description'][:200]}" for s in skills)
    user = (
        f"エージェント名（name とファイル名）: {name}\n\n## 作りたいもの\n{request}\n\n## テーマの仕様\n{spec}\n\n"
        f"## tools に書けるツール名\n{', '.join(known_tools)}\n\n## 使えるスキル\n{skill_lines}\n\n"
        f"## 書き方の例（既存のサブエージェント。構成の参考にする）\n{example}"
    )
    data = ask_json(config, _AGENT_SYSTEM, user)
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise LabLLMError(f"エージェント定義の応答の形式が不正です: {str(data)[:300]}")
    content = _strip_fence(data["content"])
    error = themes.validate_agent_md(content, name, known_tools)
    if error:
        raise LabLLMError(error)
    themes.add_asset_new(directory, "agents", name, content, actor="AI（下書き）", description=request)
    allowlist = [e for e in data.get("allowlist") or [] if isinstance(e, list) and len(e) == 2 and e[0] == name]
    if allowlist:
        try:
            merge_config_patch(directory, ("subagent", "agent_type_run_script_allowlist"), allowlist, actor="AI（下書き）")
        except (themes.ThemeError, ValueError):
            # 形の崩れた許可はあきらめる（エージェントは作れているので、許可は「フォームで編集」で足せる）。
            pass


def merge_config_patch(directory: Path, key: tuple[str, str], items: list, *, actor: str) -> None:
    """config_patch.json のリストへ項目を足す（重複は足さない）。"""
    path = directory / themes.CONFIG_PATCH_FILENAME
    data = json.loads(path.read_text(encoding="utf-8") or "{}") if path.is_file() else {}
    current = data.setdefault(key[0], {}).setdefault(key[1], [])
    for item in items:
        if item not in current:
            current.append(item)
    themes.write_text(directory, themes.CONFIG_PATCH_FILENAME, json.dumps(data, ensure_ascii=False, indent=2) + "\n", actor=actor)


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip() + "\n"


def _agent_example() -> str:
    path = PROJECT_ROOT / "agents" / "explore.md"
    if not path.is_file():
        return "（例なし）"
    return path.read_text(encoding="utf-8")[:3000]
