"""ツールの表示名（UIのStepバッジとサブエージェントの進捗表示で共有する）。

UIのStepバッジは「ツール: <表示名>」（frontend/src/components/StepItem.tsx が
"ツール: " を前置する）、dispatch_agent の進捗表示（src/subagent.py の
on_activity）も同じ「ツール: <表示名>」を出す。両方がこの対応表を引くことで、
進捗表示とStepバッジの表記が食い違わないようにする。対応表に無いツール
（Read/Glob/Grep 等）はツール名そのものを表示名にする。
"""

from __future__ import annotations

TOOL_LABELS: dict[str, str] = {
    "read_skill": "スキル読み込み",
    "read_skill_file": "ファイル読み込み",
    "run_script": "スクリプト実行",
    "analyze_image": "画像解析",
    "dispatch_agent": "サブエージェント実行",
    "dispatch_agent_batch": "サブエージェント一括実行",
    "AskUserQuestion": "ユーザーへの質問（自由記述）",
    "ask_user_choice": "ユーザーへの質問（選択式）",
    "create_plan": "実行計画作成",
    "update_task_progress": "進捗更新",
    "create_memory": "メモリー保存",
    "update_memory": "メモリー更新",
    "delete_memory": "メモリー削除",
    "read_memory": "メモリー読み込み",
    "search_memory": "メモリー検索",
    "list_memories": "メモリー一覧",
    "help": "ヘルプ表示",
}


def tool_label(name: str) -> str:
    """ツール名から表示名を返す（対応表に無ければツール名そのもの）。"""
    return TOOL_LABELS.get(name, name)
