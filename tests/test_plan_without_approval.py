"""計画承認（Plan Mode / approve_plan）廃止後の回帰テスト。

create_plan は承認を待たないチェックリスト表示だけになり、書き込み系ツール
（run_script / execute_python_code 系）は計画の有無に関わらず実行できる。
承認系ツール（approve_plan / get_plan_status / lock_plan_mode）は LLM へ公開しない。
"""

import json

import pytest

from src import tools
from src.tools._plan_render import PLAN_PREFIX


class _FakeUserSession:
    def __init__(self, data: dict | None = None):
        self._data: dict = {"thread_id": "thread-1", **(data or {})}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeMessage:
    sent: list = []

    def __init__(self, content: str = "", **kwargs) -> None:
        self.content = content

    async def send(self) -> None:
        _FakeMessage.sent.append(self.content)

    async def update(self) -> None:
        pass


def test_approval_tools_are_not_exposed() -> None:
    names = {t.name for t in tools.get_all_tools()}
    assert {"create_plan", "update_task_progress"} <= names
    assert not names & {"approve_plan", "get_plan_status", "lock_plan_mode"}


@pytest.mark.asyncio
async def test_create_plan_shows_checklist_without_waiting_for_approval(monkeypatch) -> None:
    session = _FakeUserSession()
    monkeypatch.setattr(tools.cl, "user_session", session)
    monkeypatch.setattr(tools.cl, "Message", _FakeMessage)
    monkeypatch.setattr(_FakeMessage, "sent", [])

    result = await tools.create_plan.ainvoke({"steps": [{"content": "表を作る", "activeForm": "表を作成中"}]})

    assert "approve_plan" not in result
    assert session.get("plan")[0]["status"] == "pending"
    assert session.get("awaiting_approve_plan_call") is None
    payload = json.loads(_FakeMessage.sent[0][len(PLAN_PREFIX) :])
    assert payload == {"steps": session.get("plan"), "finished": False}


def test_run_script_is_prepared_without_plan(monkeypatch, tmp_path) -> None:
    skill = tmp_path / "skills" / "my-tool"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    (skill / "SKILL.md").write_text("---\nname: my-tool\ndescription: x\n---\n", encoding="utf-8")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    monkeypatch.setattr(tools._state, "_SKILLS_ROOTS", [(tmp_path / "skills").resolve()])
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)

    prepared = tools._script_job._prepare_script_execution("my-tool", "run.py")

    assert not isinstance(prepared, str), prepared
    cmd, _workdir = prepared
    assert cmd[-1].endswith("run.py")
