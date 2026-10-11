"""AI 修正（評価で失敗した回を研究室の LLM に見せ、テーマのファイルを直させる）。

直せるのはテーマ内の資産（assets/ 配下のスキル・サブエージェント）・ケース（cases/*.yaml）・
設定パッチ（config_patch.json）・仕様以外のファイル。仕様（spec.md）は人が決めた要件なので直させない。
ケースも直してよいが、変更はレビュー画面で強調表示し、人が確かめる（ケースが変われば
トライアウトは0回からやり直しになる）。

修正は全部適用できるか、全部やめるかのどちらか。パスの制限（themes.resolve_path）と形式の検証
（SKILL.md・エージェント定義の frontmatter と tools 名・ケース・設定パッチ・Python の構文）に
1つでも通らなければ元に戻し、理由を次の反復の入力にする。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from src.config import Config

from . import evaluation, themes
from .llm import LabLLMError, ask_json

_SYSTEM = """あなたは業務用AIアシスタント（ローカルLLMで動くエージェント）のスキルとサブエージェントを直す担当です。
スキルは SKILL.md（先頭に name・description の frontmatter、本文に手順）と scripts/（Python スクリプト）・references/ でできています。
サブエージェントは agents/<名前>.md（先頭に name・description・tools の frontmatter、本文がシステムプロンプト）でできています。
評価で失敗した記録を読み、原因を特定して、仕様どおりに動くようにファイルを直します。

守ること:
- 仕様（spec.md）は変えない。仕様に合わせて資産を直す。
- 目の前の失敗例だけに効く場当たりの指示（「必ず○○と答える」等）を足さない。原因を一般化して直す。
- SKILL.md・エージェント定義の frontmatter の name はフォルダ名・ファイル名と同じにする。
- スクリプトは正常時に終了コード0で1行のJSONを標準出力へ、異常時は終了コード1で標準エラーへ理由を出す。
- ケース（cases/*.yaml）を直してよいのは、ケース自体が仕様と矛盾している場合だけ。直したら analysis にその理由を書く。
  ケースの削除・追加と、ケースの入力ファイル（cases/fixtures/）の変更はできない。
- 直せるパス: 今ある研究対象の assets/skills/<名前>/... 、assets/agents/<名前>.md 、今ある cases/<ID>.yaml 、config_patch.json
  （新しいスキル・サブエージェントは作れない。SKILL.md・エージェント定義そのものは削除できない）

次の形式の JSON だけを出力してください:
{"analysis": "失敗の原因と直し方（日本語）",
 "edits": [
   {"path": "assets/skills/<名前>/SKILL.md", "old": "置き換える元の文字列（ファイル内で1か所だけ一致）", "new": "新しい文字列"},
   {"path": "assets/skills/<名前>/scripts/run.py", "content": "ファイル全体の新しい内容"},
   {"path": "assets/skills/<名前>/references/old.md", "delete": true}
 ]}"""


def _read_limited(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "（バイナリファイル）"
    if len(text) > limit:
        return text[:limit] + f"\n…（{len(text) - limit}文字省略）"
    return text


def theme_context(directory: Path, max_file_chars: int, *, include_cases: bool = True) -> str:
    """仕様・資産・設定パッチ・ケースの全文（AI に渡す材料）。"""
    parts = [f"## 仕様（spec.md）\n{_read_limited(directory / themes.SPEC_FILENAME, max_file_chars)}"]
    for item in themes.list_files(directory):
        rel = item["path"]
        if rel == themes.SPEC_FILENAME or rel.startswith(f"{themes.CASES_DIRNAME}/{themes.FIXTURES_DIRNAME}/"):
            continue
        if not include_cases and rel.startswith(f"{themes.CASES_DIRNAME}/"):
            continue
        body = _read_limited(directory / rel, max_file_chars) if item["text"] else "（バイナリファイル）"
        parts.append(f"## {rel}\n```\n{body}\n```")
    fixtures = themes.list_fixtures(directory)
    if fixtures:
        listing = []
        for f in fixtures:
            base = directory / themes.CASES_DIRNAME / f
            listing += [p.relative_to(directory / themes.CASES_DIRNAME).as_posix() for p in sorted(base.rglob("*")) if p.is_file()]
        parts.append("## ケースの入力ファイル（中身は省略）\n" + "\n".join(f"- {x}" for x in listing))
    return "\n\n".join(parts)


def failures_text(results: list[dict], verdicts: list[dict], excerpt_chars: int, per_case: int = 2) -> str:
    """失敗した回の説明（ケースごとに最大 per_case 回分）。"""
    by_key = {(v["case_id"], v["repeat_index"]): v for v in verdicts}
    shown: dict[str, int] = {}
    blocks = []
    for r in results:
        v = by_key.get((r.get("case_id"), r.get("repeat_index", 1)))
        if not v or v["outcome"] == "pass":
            continue
        cid = r.get("case_id", "?")
        if shown.get(cid, 0) >= per_case:
            continue
        shown[cid] = shown.get(cid, 0) + 1
        failed_rules = {k: d for k, d in (r.get("rule_results") or {}).items() if not d.get("pass")}
        lines = [f"### ケース {cid}（{r.get('repeat_index', 1)}回目）: {v['outcome']}"]
        if failed_rules:
            lines.append("満たさなかった判定: " + json.dumps(failed_rules, ensure_ascii=False)[:1500])
        if v.get("ai_judge"):
            lines.append(f"AI judge の不合格理由: {v['ai_judge'].get('reason', '')}")
        lines.append(evaluation.transcript_excerpt(r, excerpt_chars))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "（失敗の記録がありません）"


def history_text(iterations: list[dict], limit: int = 5) -> str:
    rows = []
    for it in iterations[-limit:]:
        fix = it.get("fix") or {}
        rows.append(
            f"- 反復{it.get('n')}（{it.get('phase')}）: 判定 {it.get('verdict')}"
            + (f"、直したファイル {fix.get('applied')}、分析: {str(fix.get('analysis', ''))[:300]}" if fix else "")
            + (f"、修正を適用できなかった理由: {fix.get('errors')}" if fix.get("errors") else "")
        )
    return "\n".join(rows) or "（まだありません）"


def propose(config: Config, directory: Path, *, failures: str, history: str, max_file_chars: int) -> dict:
    user = (
        f"{theme_context(directory, max_file_chars)}\n\n"
        f"# これまでの反復\n{history}\n\n"
        f"# 今回の評価で失敗した記録\n{failures}\n\n"
        "原因を特定し、指定の JSON 形式で修正を出力してください。"
    )
    data = ask_json(config, _SYSTEM, user)
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        raise LabLLMError(f"修正の応答の形式が不正です: {str(data)[:300]}")
    return {"analysis": str(data.get("analysis", "")), "edits": data["edits"]}


def _forbidden(directory: Path, rel: str, path: Path, *, delete: bool) -> str | None:
    """AI 修正でしてはいけない変更なら理由を返す。

    試験に通るだけの直し方（失敗したケースを消す・入力ファイルを都合よく書き換える・
    研究対象そのものを消す・別の資産を作って差し替える）を防ぐ。ケースの中身を直すのは、
    仕様と矛盾している場合に限って認め、レビューで強調表示する。
    """
    parts = rel.split("/")
    if parts[0] == themes.CASES_DIRNAME and len(parts) >= 3:
        return "ケースの入力ファイルは AI では変更できません"
    if parts[0] == themes.CASES_DIRNAME:
        if delete:
            return "ケースは AI では削除できません"
        if not path.is_file():
            return "ケースは AI では追加できません（今あるケースを直すことだけができます）"
    if parts[0] == themes.ASSETS_DIRNAME:
        asset = path if parts[1] == "agents" else directory / themes.ASSETS_DIRNAME / "skills" / parts[2]
        exists = asset.is_file() if parts[1] == "agents" else (asset / "SKILL.md").is_file()
        if not exists:
            return "研究対象に無いスキル・サブエージェントは AI では作れません"
        if delete and (parts[1] == "agents" or parts[3:] == ["SKILL.md"]):
            return "研究対象そのもの（SKILL.md・エージェント定義）は AI では削除できません"
    return None


def apply_edits(directory: Path, edits: list, *, known_tools: list[str] | None) -> tuple[list[str], list[str]]:
    """edits をすべて適用する（1つでも失敗したら全部元に戻す）。(適用したパス, エラー) を返す。"""
    errors: list[str] = []
    planned: list[tuple[str, Path, str | None]] = []  # (rel, path, 新しい内容 or None=削除)
    pending: dict[Path, str | None] = {}
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
            errors.append(f"{i + 1}件目: path がありません")
            continue
        rel = edit["path"].replace("\\", "/").strip().strip("/")
        if rel == themes.SPEC_FILENAME:
            errors.append(f"{rel}: 仕様は AI では変更できません")
            continue
        try:
            path = themes.resolve_path(directory, rel, for_write=True)
        except themes.ThemeError as e:
            errors.append(str(e))
            continue
        forbidden = _forbidden(directory, rel, path, delete=bool(edit.get("delete")))
        if forbidden:
            errors.append(f"{rel}: {forbidden}")
            continue
        try:
            current = pending[path] if path in pending else (path.read_text(encoding="utf-8") if path.is_file() else None)
        except UnicodeDecodeError:
            errors.append(f"{rel}: テキストファイルではないため AI では直せません")
            continue
        if edit.get("delete"):
            new_content = None
        elif isinstance(edit.get("content"), str):
            new_content = edit["content"]
        elif isinstance(edit.get("old"), str) and isinstance(edit.get("new"), str):
            if current is None:
                errors.append(f"{rel}: ファイルが無いため old/new では直せません（content で全体を書いてください）")
                continue
            count = current.count(edit["old"])
            if count != 1:
                errors.append(f"{rel}: old がファイル内で{count}か所一致しました（1か所だけ一致させてください）")
                continue
            new_content = current.replace(edit["old"], edit["new"])
        else:
            errors.append(f"{rel}: content・old/new・delete のどれも指定がありません")
            continue
        if new_content is not None:
            try:
                themes.validate_content(rel, new_content)
                if rel.startswith(f"{themes.ASSETS_DIRNAME}/agents/") and known_tools is not None:
                    error = themes.validate_agent_md(new_content, Path(rel).stem, known_tools)
                    if error:
                        raise themes.ThemeError(error)
            except themes.ThemeError as e:
                errors.append(str(e))
                continue
        pending[path] = new_content
        planned.append((rel, path, new_content))
    if errors:
        return [], errors
    if not planned:
        return [], ["修正が1件もありません"]

    with tempfile.TemporaryDirectory(prefix="skill_lab_fix_") as tmp:
        backups: dict[Path, Path | None] = {}
        try:
            for rel, path, content in planned:
                if path not in backups:
                    if path.is_file():
                        backup = Path(tmp) / f"{len(backups)}"
                        shutil.copy2(path, backup)
                        backups[path] = backup
                    else:
                        backups[path] = None
                if content is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
        except OSError as e:
            for path, backup in backups.items():
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, path)
            return [], [f"ファイルの書き込みに失敗しました: {e}"]
    applied = list(dict.fromkeys(rel for rel, _, _ in planned))
    themes.mark_changed(directory, "AI", "AI 修正: " + ", ".join(applied))
    return applied, []
