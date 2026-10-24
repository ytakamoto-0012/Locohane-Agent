"""設定ダッシュボード（admin/）のサーバー側の表示言語（日本語 / 英語）。

画面に出るエラーメッセージ（HTTPException の detail・入力検証の例外）だけを訳す。
フロントエンド（admin/static/i18n.js）と同じく日本語の原文をキーにし、可変部分は
{0}・{1}… で書く（t("インスタンス {0!r} が見つかりません。", name)）。

言語はリクエストの X-Locohane-Lang ヘッダー（admin.js の api() が付ける）で決まり、
LanguageMiddleware が contextvar に入れる。リクエストの外（ワーカー・CLI・テスト）や
ヘッダーが無いときは日本語。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

HEADER = "X-Locohane-Lang"
_LANG: ContextVar[str] = ContextVar("locohane_admin_lang", default="ja")


def normalize(value: object) -> str:
    """言語コードを "ja" / "en" へ正規化する（不明なら "ja"）。"""
    if not isinstance(value, str):
        return "ja"
    code = value.strip().lower().split("-", 1)[0].split("_", 1)[0]
    return "en" if code == "en" else "ja"


def current_lang() -> str:
    """このリクエストの表示言語。"""
    return _LANG.get()


def set_request_lang(value: object) -> Token:
    """このリクエストの表示言語を設定する（戻り値は reset_request_lang に渡す）。"""
    return _LANG.set(normalize(value))


def reset_request_lang(token: Token) -> None:
    _LANG.reset(token)


def t(text: str, *args: object) -> str:
    """表示文言を引く。英語なら EN の訳（無ければ原文）、日本語なら原文。args は str.format で埋める。"""
    translated = EN.get(text) if _LANG.get() == "en" else None
    result = translated if translated is not None else text
    return result.format(*args) if args else result


def response_language_instruction() -> str:
    """管理アシスタントのシステムプロンプト末尾に足す応答言語の指示（日本語なら空文字）。"""
    if _LANG.get() != "en":
        return ""
    return (
        "\n\n## Response language\n"
        "The administrator is using the dashboard in English. Always answer in English, even though the "
        "instructions and reference material above are in Japanese. Keep config keys, section names, screen "
        "names in brackets and file paths exactly as they are."
    )


class LanguageMiddleware:
    """X-Locohane-Lang ヘッダーをリクエストの表示言語として contextvar に入れる ASGI ミドルウェア。

    BaseHTTPMiddleware ではなく素の ASGI にするのは、管理アシスタントのストリーミング応答
    （NDJSON）の生成中も同じコンテキストで値を読めるようにするため。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = HEADER.lower().encode("latin-1")
        value = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == header), "")
        token = set_request_lang(value)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_lang(token)


# 英訳（キーは日本語の原文）。画面向けのメッセージを足したら、ここにも英訳を足す（無ければ日本語のまま）。
EN: dict[str, str] = {
    "スキル研究室のインスタンスには研究テーマを置けません。": "Research themes cannot be placed on a skill lab instance.",
    "テーマ {0} の theme.json を読めません（手で直すか、テーマのフォルダを退避してください）。": "Cannot read theme.json of theme {0} (fix it by hand, or move the theme folder aside).",
    "このインスタンスのテーマではありません。": "This theme does not belong to this instance.",
    "スキル研究室 {0} が見つかりません。": "Skill lab {0} not found.",
    "このテーマは終了しています（{0}）。": "This theme has ended ({0}).",
    "ループ・トライアウトの実行中は編集できません。先に停止してください。": "Cannot edit while a loop or tryout is running. Stop it first.",
    "このテーマは終了しています。": "This theme has ended.",
    "ループ・トライアウトの実行中・開始待ちの間は担当のワーカーを変えられません。先に停止してください。": "The assigned worker cannot be changed while a loop or tryout is running or waiting to start. Stop it first.",
    "同じ名前の試験問題（{0}）が既にあります。別の名前にしてください。": "A test question with the same name ({0}) already exists. Use a different name.",
    "ファイルが大きすぎます（上限 {0}MB）。": "The file is too large (limit {0}MB).",
    "フォルダ名とファイル名を指定してください。": "Specify a folder name and a file name.",
    "ループ・トライアウトの実行中は下書きを頼めません。": "Cannot request a draft while a loop or tryout is running.",
    "試すケースを選んでください。": "Choose the cases to try.",
    "mode は loop か tryout にしてください。": "mode must be loop or tryout.",
    "開始するには次を入れてください: {0}": "Fill in the following before starting: {0}",
    "担当のスキル調整ワーカー {0} が見つかりません。「担当・名前を変更」で選び直してください。": "The assigned skill tuning worker {0} was not found. Choose again with \"Change worker/name\".",
    "担当のスキル調整ワーカーを選んでください（「担当・名前を変更」）。": "Choose the assigned skill tuning worker (\"Change worker/name\").",
    "スキル調整ワーカーがありません。先にインスタンス一覧で作成してください。": "There is no skill tuning worker. Create one in the instance list first.",
    "AI への依頼（下書き）が終わるまで待ってから開始してください。": "Wait until the request to the AI (draft) finishes before starting.",
    "テーマの状態が変わったため開始できませんでした。画面を更新してください。": "Could not start because the theme's state changed. Refresh the page.",
    "テーマの状態が変わったため取りやめられませんでした。画面を更新してください。": "Could not cancel because the theme's state changed. Refresh the page.",
    "反復が見つかりません。": "Iteration not found.",
    "タスクが見つかりません。": "Task not found.",
    "インスタンス {0} の設定を読めません: {1}": "Cannot read the settings of instance {0}: {1}",
    "ファイルの内容を読めません。": "Cannot read the file contents.",
    "同じ名前の研究対象が既にあります: {0}": "A research target with the same name already exists: {0}",
    "何をさせたいかを書いてください。": "Describe what it should do.",
    "ドラフトの取り込みにはドラフトのフォルダを指定してください。": "Specify the draft folder to import a draft.",
    "このインスタンスのドラフトではありません。": "This draft does not belong to this instance.",
    "このドラフトは別のテーマ（{0}）に取り込み済みです。そのテーマで続けるか、取りやめてから取り込んでください。": "This draft is already imported into another theme ({0}). Continue in that theme, or cancel it before importing.",
    "正式の {0} が見つかりません。": "Official {0} not found.",
    "不明な追加方法です: {0}": "Unknown way to add: {0}",
    "管理アシスタントは無効です（[admin] assistant_enabled）。": "The admin assistant is disabled ([admin] assistant_enabled).",
    "最後のメッセージは相談内容（role=user）にしてください。": "The last message must be your question (role=user).",
    "{0} は専用のエンドポイントから操作してください。": "Use the dedicated endpoint for {0}.",
    "環境変数名として不正です: {0!r}": "Invalid environment variable name: {0!r}",
    "{0} はインスタンスの .env では設定できません（管理ツールの重複チェック・稼働判定と食い違うため）。データ保存先は設定画面の [paths] から変更してください。": "{0} cannot be set in an instance's .env (it would conflict with the admin tool's duplicate check and status detection). Change the data location from [paths] on the settings screen.",
    "インスタンス名は半角英数字・アンダースコア・ハイフンのみ、1〜32文字にしてください: {0!r}": "Instance names may contain only letters, digits, underscores and hyphens, 1 to 32 characters: {0!r}",
    "インスタンス名は{0}文字以内にしてください（データ保存先のパスが長くなりすぎるため）: {1!r}": "Instance names must be at most {0} characters (otherwise the data paths become too long): {1!r}",
    "インスタンス {0!r} が見つかりません。": "Instance {0!r} not found.",
    "インスタンスの種類が不正です: {0!r}（{1} のいずれか）": "Invalid instance type: {0!r} (one of {1})",
    "インスタンス {0!r} は既に存在します。": "Instance {0!r} already exists.",
    "複製先のインスタンス専用ディレクトリに既にあります: {0}": "Already present in the target instance's dedicated directory: {0}",
    "既定インスタンス(default)は削除できません。": "The default instance (default) cannot be deleted.",
    "空いているポートが見つかりませんでした。": "No free port was found.",
    "データ保存先がインスタンス {0!r} と重複しています（{1}）。[paths].common_data_dir を分けてください。": "The data location overlaps with instance {0!r} ({1}). Use separate [paths].common_data_dir values.",
    "ポート {0}:{1} は既にインスタンス {2!r} が使用しています。": "Port {0}:{1} is already used by instance {2!r}.",
    "複製元のインスタンス {0!r} が見つかりません。": "Source instance {0!r} not found.",
    "インスタンス {0!r} の設定を読み込めないため、データの保存先を特定できません: {1}": "Cannot read the settings of instance {0!r}, so its data location cannot be determined: {1}",
    "データを削除するには config.ini のパスが必要です。": "The config.ini path is required to delete data.",
    "不明なデータ項目です: {0!r}": "Unknown data item: {0!r}",
    "{0} は削除できません（{1}）: {2}": "{0} cannot be deleted ({1}): {2}",
    "データの削除に失敗しました（インスタンス自体は削除していません）: {0}: {1}": "Failed to delete data (the instance itself was not deleted): {0}: {1}",
    "取り込み元フォルダは絶対パスで指定してください。": "Specify the source folder as an absolute path.",
    "取り込み元フォルダがありません（管理ツールのPCから見えるパスを指定してください）: {0}": "The source folder does not exist (specify a path visible from the admin tool's PC): {0}",
    "スキル研究室のインスタンスにはナレッジを置けません。": "Knowledge cannot be placed on a skill lab instance.",
    "取り込み中のコレクションは削除できません。先に停止してください。": "A collection being ingested cannot be deleted. Stop it first.",
    "このインスタンスの [vector_db] enabled が false です。config.ini タブで有効にしてください。": "[vector_db] enabled is false for this instance. Enable it in the config.ini tab.",
    "このコレクションは取り込み中です。": "This collection is being ingested.",
    "取り込み中ではありません。": "Not ingesting.",
    "ベクターを削除できませんでした: {0}": "Could not delete the vectors: {0}",
    "{0}: トップレベルはオブジェクトである必要があります。": "{0}: the top level must be an object.",
    "バックアップが見つかりません: {0}": "Backup not found: {0}",
    "[admin] セクションは管理ツールからは変更できません（config.ini を直接編集してください）。": "The [admin] section cannot be changed from the admin tool (edit config.ini directly).",
    "[{0}].{1} は config.ini に存在しないキーです。": "[{0}].{1} is not a key in config.ini.",
    "他のセッション/ブラウザタブで既に保存されています。画面を再読み込みしてから編集し直してください。": "Someone already saved in another session/browser tab. Reload the page and edit again.",
    "設定の検証に失敗しました: {0}": "Settings validation failed: {0}",
    "バックアップの検証に失敗しました: {0}": "Backup validation failed: {0}",
    "config.ini に [{0}].{1} がありません。": "[{0}].{1} is missing from config.ini.",
    "ほかの昇格・差し戻しを処理中です。終わってからやり直してください。": "Another promotion or return is in progress. Try again after it finishes.",
    "昇格できません: {0}": "Cannot promote: {0}",
    "確認が必要です: {0}": "Confirmation required: {0}",
    "差し戻しの理由を書いてください（作成者の skill-creator に表示されます）。": "Write the reason for returning it (shown in the author's skill-creator).",
    "このテーマは既に終了しています（{0}）": "This theme has already ended ({0})",
    "ループ・トライアウトの実行中は差し戻せません（先に停止してください）": "Cannot return it while a loop or tryout is running (stop it first)",
    "差し戻せる取り込み元のドラフトがありません（テーマを終えるには「取りやめ」を使ってください）。": "There is no source draft to return it to (use \"Cancel\" to end the theme).",
    "昇格に失敗したため、置いたファイルを元に戻しました（設定を保存済みのインスタンス: {0}）: {1}": "Promotion failed, so the placed files were restored (instances whose settings were saved: {0}): {1}",
    "ドラフト {0}/{1} へ修正を写せませんでした（ドラフトは元のままです。作成者がファイルを開いていないか確かめてください。差し戻し済み: {2}）: {3}": "Could not copy the fixes to draft {0}/{1} (the draft is unchanged; check that the author does not have the files open. Already returned: {2}): {3}",
    "ログインが必要です。": "Sign-in required.",
    "CSRFヘッダ（X-Locohane-Admin: 1）がありません。": "The CSRF header (X-Locohane-Admin: 1) is missing.",
    "ログイン試行回数が上限を超えました。しばらく待ってから再試行してください。": "Too many sign-in attempts. Wait a while and try again.",
    "ユーザー名またはパスワードが違います。": "Incorrect username or password.",
    "稼働中のインスタンスは削除できません。先に停止してください。": "A running instance cannot be deleted. Stop it first.",
    "{0}では効かない設定のため変更できません: {1}": "These settings have no effect on {0}, so they cannot be changed: {1}",
    "不正なファイル名です。": "Invalid file name.",
    "既に存在するユーザー名です。": "That username already exists.",
    "プロジェクト直下 .env のユーザー（管理者設定）はこの画面から変更・削除できません。": "Users in the project's .env (administrator setting) cannot be changed or deleted here.",
    "ユーザーが見つかりません（継承中の場合は先にユーザーを追加してください）。": "User not found (if users are inherited, add a user first).",
    "ユーザーが見つかりません。": "User not found.",
    "不正な種別です: {0}": "Invalid kind: {0}",
    "スレッドが見つかりません。": "Thread not found.",
    "resets の要素は [section, key] の2要素にしてください: {0!r}": "Each element of resets must be a two-element [section, key]: {0!r}",
    "base64のデコードに失敗しました: {0}": "Failed to decode base64: {0}",
    "{0} が見つかりません。": "{0} not found.",
    "{0} は編集できません。": "{0} cannot be edited.",
    "許可されていない拡張子です（{0} のいずれか）: {1!r}": "Extension not allowed (must be one of {0}): {1!r}",
    "ファイルサイズが大きすぎます（上限 {0}MB）。": "The file is too large (limit {0}MB).",
    "{0} は削除できません。": "{0} cannot be deleted.",
    "テーマ ID が不正です: {0!r}": "Invalid theme ID: {0!r}",
    "テーマが見つかりません: {0}": "Theme not found: {0}",
    "テーマ名を入力してください。": "Enter a theme name.",
    "パスに使えない名前が含まれています: {0!r}": "The path contains a name that cannot be used: {0!r}",
    "パスが不正です: {0!r}": "Invalid path: {0!r}",
    "このパスは扱えません: {0}": "This path cannot be handled: {0}",
    "テーマの外を指すパスです: {0}": "The path points outside the theme: {0}",
    "スキルのフォルダそのものではなく、その中のファイルを指定してください。": "Specify a file inside the skill folder, not the folder itself.",
    "入力ファイルは cases/fixtures/<フォルダ名>/ の中に置いてください。": "Put input files inside cases/fixtures/<folder name>/.",
    "同じ名前のフォルダがあります: {0}": "A folder with the same name exists: {0}",
    "ファイルが見つかりません: {0}": "File not found: {0}",
    "ファイルのアップロードは cases/fixtures/ 配下にだけできます。": "Files can only be uploaded under cases/fixtures/.",
    "{0} は削除できません（中身を空にして保存してください）。": "{0} cannot be deleted (empty its contents and save instead).",
    "研究対象そのものを除くには、研究対象の「外す」を使ってください。": "To remove a research target itself, use \"Remove\" on the research target.",
    "見つかりません: {0}": "Not found: {0}",
    "研究対象の種類が不正です: {0!r}": "Invalid research target type: {0!r}",
    "名前は小文字英数字・ハイフン・アンダースコアのみにしてください: {0!r}": "Names may contain only lowercase letters, digits, hyphens and underscores: {0!r}",
    "研究対象が見つかりません: {0}": "Research target not found: {0}",
    "不明なタスクです: {0}": "Unknown task: {0}",
    "テキストファイルではありません: {0}": "Not a text file: {0}",
    "スキルが見つかりません: {0}": "Skill not found: {0}",
    "エージェント定義が見つかりません: {0}": "Agent definition not found: {0}",
    "テーマのロックを取得できません（他の処理が使用中）: {0}": "Cannot get the theme lock (in use by another process): {0}",
    "関係する設定・説明を探しています…": "Looking for related settings and docs…",
    "関係する設定・説明を検索できませんでした: {0}": "Could not search related settings and docs: {0}",
    "回答を考えています…": "Thinking about the answer…",
    "LLM（インスタンス {0!r} の [llm] 接続先）から応答を得られませんでした: {1}": "Got no response from the LLM (the [llm] endpoints of instance {0!r}): {1}",
    "LLM の応答が空でした。": "The LLM returned an empty response.",
}
