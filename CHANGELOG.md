# 変更履歴

このファイルには、Locohane の利用者・運用者に関係する主な変更を記録します。
書式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に、番号の付け方は
[セマンティックバージョニング](https://semver.org/lang/ja/) に従います。

- 1.0.0 になるまでは、MINOR（0.**x**.0）が上がるリリースに、`config.ini` のキーの移動・改名や
  データ配置の変更など、互換性のない変更が含まれることがあります。その場合は各版の
  「移行時の注意」に手順を書きます。
- PATCH（0.x.**y**）のリリースは、不具合修正と互換性を保った小さな改善だけです。
- リリース手順は README_DETAIL.md の「バージョン管理とリリース手順」節を参照してください。

## [Unreleased]

### 追加

- **ベクターDB（社内文書の意味検索）**: 規程・手順書等の文書（PDF・Word・Excel・PowerPoint・Markdown・テキスト・CSV）を
  インスタンスごとに取り込み、AI が `search_documents` ツールで意味の近い箇所を探して出典付きで回答できるようにしました。
  保存先は sqlite-vec（既定、サーバー不要）か Qdrant サーバーを選べます。埋め込みは別に起動した llama-server の
  `/v1/embeddings` で計算します。文書の取り込み（差分取り込み・再構築・停止）は設定ダッシュボードのインスタンスの
  「ナレッジ」画面で管理者が行います。新しい設定は `config.ini` の `[vector_db]` で、既定は無効です。
  `[vector_db] image_input = true` にすると、PDF・Word・Excel・PowerPoint をページごとの画像にして埋め込み
  （画像入力に対応した埋め込みモデルが必要。Office ファイルには Windows と Microsoft Office が必要）、図・表やスキャンした
  PDF も検索できます。検索結果にはページ画像のパスも付き、AI が画像で中身を確かめられます。

## [0.2.0] - 2026-10-23

v0.1.1 から約 590 コミット分の変更をまとめた最初の正式な番号付きリリースです。

### 移行時の注意

v0.1.1 の `config.ini`・データをそのまま使う場合は、以下を確認してください。
迷ったら同梱の `config.ini` を基に設定し直すのが確実です。

- **プロジェクト名**: リポジトリ名を `Locohane-Agent` に改名しました。
- **Python 環境の指定**: 基本の Python 環境パスは `python_env.bat` の 1 か所で指定します。
- **複数インスタンス化**: 設定ダッシュボード（`admin.bat`）から複数のインスタンスを起動する構成になり、
  ログ・スレッド・永続メモリー等のデータは `data/<インスタンス名>/` 配下に置かれます。
  インスタンス専用の設定・スキルは `instances/<インスタンス名>/` に置きます
  （場所は `[admin].instances_dir` で変更可能）。
- **`agents/`・`skills/` の場所**: `.locohane/` 直下からリポジトリ直下へ移しました。
- **設定キーの移動・改名**
  - `[paths]` を `uploads`・`log`・`default_workdir` 系に整理しました。
  - `[llm]` の接続先は `main_url`・`sub_url` に分かれ、それぞれ複数の接続先と使用時間帯（`start`・`end`）を指定できます。
    `sticky` 戦略は廃止し、`round_robin`（llama-server の `GET /slots` で空きを確認）・`priority_failover` を使います。
  - `main_agent_glob_guard` は `[main_agent_tool_guard]` に統合し、許可リスト方式・3 値の `mode`・`visibility_mode` に変わりました。
  - `plan_approval_exempt_scripts` は `[plan]` へ、`agent_type_run_script_allowlist` は `[subagent]` へ移しました。
  - `[paths].instance_locohane_dir` は `[skill_lab].instance_locohane_dir` へ移し、既定値を `${instance_dir}` にしました。
  - スキル調整ワーカー専用のループ上限は `[skill_lab_worker]` に分けました。
- **廃止・統合したもの**
  - `planner` サブエージェントは廃止しました。`explore-docs`（のちの `analyze-docs`）・`explore-websearch` は `explore` に統合しました。
  - `excel-tools`・`docx-tools`・`pptx-tools` は、機能別のスキル（`*-read`・`*-edit`・`*-render` 等）に分割しました。
  - officecli への依存は削除しました。
  - ツール `list_path_memory` は `search_path_memory` に、`show_image` は `analyze_image(show_in_chat)` に置き換えました。
  - `run_script_readonly` は廃止しました（`explore` は許可リスト制限つきの `run_script` を使います）。

### 追加

- **設定ダッシュボード（管理ツール）**: 複数インスタンスの起動・停止・設定編集、`config.ini` のプリセット絞り込み、
  変更履歴、API リファレンス、接続中ユーザー・生成中スレッド・トークン推移のモニター。
- **スキル研究室**: 利用者のドラフトスキル（skill-creator で作成）を、スキル調整ワーカーが評価・AI 修正し、
  安定化トライアウトを経てインスタンス専用スキルへ昇格させる仕組み。
- **会話スレッド**: 左サイドバーの会話一覧・再開、生成中スレッドのライブ中継と別セッションからの停止、
  他セッションの生成中・ユーザー操作待ちの表示、添付ファイル・画像の永続化。
- **スレッド共有ノート（thread notes）**と**永続メモリーの類似検索**。
- **サブエージェント**: `explore`・`worker`・`verifier`、並列一括委譲の `dispatch_agent_batch`、
  `agents/*.md` の `model` 指定と `[llm].sub_default_model`、実行中の動作表示。
- **長時間・大量処理への対策**: コンテキストのトリム・圧縮（メイン・サブエージェント別に設定可）、
  トークンガード、思考ループ検知（`[thinking_loop_guard]`）、同一応答ループ検知（`[tool_loop_guard]`）、
  思考レベルの自動切り替え（`[thinking_control]`）、LLM 接続断時の接続先切り替えと自動リトライ。
- **バックグラウンド実行**: `run_script_background`・`execute_python_code_background` と進捗表示。
- **計画承認**: `create_plan` の詳細計画 Markdown、`approve_plan` の自動承認（`[plan].auto_approve`）。
- **同梱スキル**: Office 文書（Excel・Word・PowerPoint）の読込・生成・編集・画像化、VBA 読み書き、
  PDF、HEIC/HEIF 画像、Tavily による Web 検索。
- **MCP サーバー**（`mcp_server/`）: Locohane のスキルを Claude Code 等へ配布。
- **プロンプト自動チューニング**（`evals/`）と、Claude Code 用スキル `tune-prompt`・`create-eval-case`・
  `monitor-app-log`・`consolidate-memory`・`apply-memory-to-skills`・`promote-skill`・`setup-locohane`。
- **画面**: 定型文ボタン、長文貼り付けの添付化、トークン使用量・実行計画パネル、スマホ幅表示、
  ブラウザのタブタイトル・ヘッダーの差し替え。
- **HTTPS での待ち受け**（`[admin].ssl_certfile`・`ssl_keyfile`）。
- **reasoning 設定**（`reasoning_effort`・`reasoning_preserve`）による llama-server・vLLM の差の吸収。
- **バージョン表示**: `VERSION` ファイルを新設し、チャット画面のヘッダー（ツールチップ）、
  設定ダッシュボードの上部バー、MCP サーバーの `serverInfo`、起動ログにバージョン番号を出します。

### 変更

- システムプロンプトを 3 層構成に再編し、ローカルの低パラメータモデル向けに継続的にチューニングしました。
- `run_script`・`execute_python_code` の書き込み先をサンドボックスに限定し、
  `git`・`npm`・`pip` の実行を禁止しました（追加の許可は `allow_sandbox_dir`）。
- 二重起動による SQLite 破損を防ぐため、インスタンス単位の排他ロックを追加しました。
- アップロードしたファイルの保存先をユーザー名ごとのサブフォルダにし、画像も LLM へ直接渡さずパスで渡すようにしました。
- `app_*.log` を行数でローテーションし、全行に会話の `thread_id` を付けるようにしました。
- 依存パッケージを更新しました（chainlit 2.12.0 で MCP エンドポイントの脆弱性に対応、mcp 1.30.0、pypdf 6.19.0 等）。

### 修正

- WebSocket 再接続時の Step 再同期漏れ、停止ボタンが生成を止めない問題、
  孤立した tool_call による会話破損など、長時間運用で見つかった多数の不具合を修正しました。

## [0.1.1] - 2026-07-30

- 初期設定まわりを更新しました。

## [0.1.0] - 2026-07-30

- 最初の公開版です。

[Unreleased]: https://github.com/ytakamoto-0012/Locohane-Agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ytakamoto-0012/Locohane-Agent/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/ytakamoto-0012/Locohane-Agent/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ytakamoto-0012/Locohane-Agent/releases/tag/v0.1.0
