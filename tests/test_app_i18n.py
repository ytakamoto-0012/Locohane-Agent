"""app.py の表示言語まわり（接続時の言語・言語別の表示設定ファイル・set_language）のテスト。"""

import types

import pytest

import app
from src import i18n


class _UserSession:
    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value


@pytest.mark.parametrize(
    ("env", "expected"),
    [({"LOCOHANE_LANG": "en"}, "en"), ({"LOCOHANE_LANG": "en-US"}, "en"), ({"LOCOHANE_LANG": "xx"}, "ja"), ({}, "ja"), (None, "ja")],
)
def test_init_session_lang_reads_user_env(monkeypatch, env, expected) -> None:
    session = _UserSession({"env": env})
    monkeypatch.setattr(app.cl, "user_session", session)
    app._init_session_lang()
    assert session.get("lang") == expected


def test_localized_settings_text_prefers_language_file(tmp_path, monkeypatch) -> None:
    (tmp_path / "welcome.md").write_text("ようこそ", encoding="utf-8")
    (tmp_path / "welcome.en.md").write_text("Welcome", encoding="utf-8")
    monkeypatch.setattr(app, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "INSTANCE_SETTINGS_DIR", tmp_path / "instance")

    monkeypatch.setattr(app.cl, "user_session", _UserSession({"lang": "ja"}))
    assert app._load_localized_settings_text("welcome.md", "default") == "ようこそ"
    monkeypatch.setattr(app.cl, "user_session", _UserSession({"lang": "en"}))
    assert app._load_localized_settings_text("welcome.md", "default") == "Welcome"


def test_localized_settings_text_does_not_fall_back_to_japanese_file(tmp_path, monkeypatch) -> None:
    # 英語画面に日本語の welcome.md を出さず、組み込みの英語文言を使う。
    (tmp_path / "welcome.md").write_text("ようこそ", encoding="utf-8")
    monkeypatch.setattr(app, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(app, "INSTANCE_SETTINGS_DIR", tmp_path / "instance")
    monkeypatch.setattr(app.cl, "user_session", _UserSession({"lang": "en"}))
    assert app._load_localized_settings_text("welcome.md", i18n.t("welcome.default")).startswith("Locohane is ready.")


@pytest.mark.asyncio
async def test_set_language_updates_session_and_resends_localized_ui(monkeypatch) -> None:
    session = _UserSession({"lang": "ja", "work_dir": None})
    monkeypatch.setattr(app.cl, "user_session", session)
    sent: list[str] = []
    settings: list[str] = []

    class _Message:
        def __init__(self, content: str, **_kwargs):
            self.content = content

        async def send(self):
            sent.append(self.content)

    async def _fake_settings(initial: str) -> None:
        settings.append(initial)

    monkeypatch.setattr(app.cl, "Message", _Message)
    monkeypatch.setattr(app, "_send_work_dir_settings", _fake_settings)

    result = await app.on_set_language(types.SimpleNamespace(payload={"lang": "en"}))

    assert result == {"ok": True, "lang": "en"}
    assert session.get("lang") == "en"
    # グラフは次の on_message で組み直す（graph_lang と食い違うことで検知する）。
    assert session.get("graph_lang") is None
    assert sent[0].startswith(app.WORK_DIR_PREFIX)
    assert "Not set" in sent[0]
    assert settings == [""]


def test_set_language_is_not_a_chat_interaction() -> None:
    # 送信前に言語を切り替えただけで "set_language" という名前の会話が作られないようにする。
    assert "set_language" in app._NON_CHAT_ACTION_NAMES


@pytest.mark.asyncio
async def test_set_language_rejects_unknown_language(monkeypatch) -> None:
    session = _UserSession({"lang": "ja"})
    monkeypatch.setattr(app.cl, "user_session", session)
    result = await app.on_set_language(types.SimpleNamespace(payload={"lang": "fr"}))
    assert result == {"ok": False, "lang": "ja"}
    assert session.get("lang") == "ja"


@pytest.mark.asyncio
@pytest.mark.parametrize(("lang", "has_directive"), [("ja", False), ("en", True)])
async def test_rebuild_graph_appends_response_language_only_for_english(monkeypatch, lang, has_directive) -> None:
    session = _UserSession({"lang": lang})
    monkeypatch.setattr(app.cl, "user_session", session)
    monkeypatch.setattr(app.cl, "context", types.SimpleNamespace(session=types.SimpleNamespace(id="tab")))
    monkeypatch.setattr(app, "_apply_session_draft_skills", lambda: "BASE PROMPT")
    captured: list[str] = []

    async def _fake_build_graph(_config, system_prompt, _checkpointer, *, wait_when_busy):
        captured.append(system_prompt)
        return "graph"

    monkeypatch.setattr(app, "build_graph", _fake_build_graph)

    await app._rebuild_graph("t1", wait_when_busy=False)

    # 日本語のときはプロンプトを一切変えない（evals のチューニング結果を守るため）。
    assert (captured[0] != "BASE PROMPT") is has_directive
    assert ("## Response language" in captured[0]) is has_directive
    assert session.get("graph_lang") == lang
