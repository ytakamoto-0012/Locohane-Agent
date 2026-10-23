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
  `[vector_db] image_input = true` にすると、PDF・Word・Excel・PowerPoint は本文に加えてページごとの画像も埋め込み
  （画像入力に対応した埋め込みモデルが必要。Office ファイルには Windows と Microsoft Office が必要）、図・表やスキャンした
  PDF も検索できます。本文と画像の検索結果は順位で統合し（Reciprocal Rank Fusion）、両方で一致したページを上位にします。
  ページ画像のファイルは保存せず、出典は元のファイルとページ位置です。
- ナレッジの取り込みで、`[vector_db] image_input = true` のときは画像ファイル（.png・.jpg・.gif・.bmp・.webp・.tif・.heic・.heif）も
  取り込めるようにしました。写真やスキャン画像も検索できます。
- ナレッジの検索に、本文のキーワード検索（SQLite FTS5 の BM25。日本語は2文字ずつ区切って索引にする）を加えました。
  意味検索が苦手な型番・品番・数値の完全一致も見つかります。本文の意味検索・キーワード・ページ画像の結果は順位で統合し、
  それぞれの重みを `[vector_db] rrf_weight_text`・`rrf_weight_keyword`・`rrf_weight_image` で変えられます。
- ナレッジの取り込みを複数ファイル並列で行えるようにしました（`[vector_db] ingest_workers`、既定1）。ファイルの読み取り・
  本文の抽出・画像化・埋め込みを並列にします。埋め込みを速くするには、推論サーバー側も同時処理できるように
  （llama-server なら `--parallel` を `ingest_workers` 以上にして）起動してください。設定ダッシュボードの「ベクターDB」プリセットにも出ます。
- ナレッジの保存先を Qdrant にしたとき、チャンク件数の規模に合わせたコレクションの設定を `[vector_db] qdrant_preset`
  （`none` 既定＝Qdrant の既定のまま・`1m`・`2m`・`3m`・`4m`＝〜100万・200万・300万・400万件向け）で選べるようにしました。
  HNSW 索引の大きさ、int8 量子化、元のベクターをディスクに置くか、セグメント数、検索時の探索幅を切り替えます。
  既存のコレクションにも次の「取り込み（差分）」で反映し、埋め込み直しは要りません。設定ダッシュボードの「ベクターDB」
  プリセットにも出ます。また、`config.ini` の `backend` の説明に sqlite-vec・Qdrant それぞれが対応できるチャンク件数の目安を追記しました。
- ナレッジのコレクションの距離関数を `[vector_db] distance`（`Cosine` 既定・`Euclid`・`Manhattan`・`Dot`）で
  選べるようにしました。sqlite-vec・Qdrant のどちらでも使えます（`Dot` は Qdrant のみ）。
  変えた後は既存のコレクションの再構築が必要です。既存の sqlite-vec の DB はそのまま `Cosine` として使えます。
- ナレッジの取り込みで上限サイズ（`[vector_db] max_file_mb`）を超えたファイルがあったとき、ナレッジ画面に「`max_file_mb` を大きくして取り込み直せば取り込める」案内を出すようにしました。設定ダッシュボードの「ベクターDB」プリセットにも `max_file_mb` を加えました。
  既に取り込んだコレクションは、次の「取り込み（差分）」で埋め込み直さずにキーワード索引だけを作ります。
- ナレッジの検索結果で、同じファイル・同じページのチャンクを1件にまとめるようにしました（最も一致したチャンクを抜粋にし、
  他に一致したチャンクは数だけ表示）。
- ナレッジの取り込みで、隠しファイル・隠しフォルダ（名前が `.` で始まるもの、Windows の隠し属性付き）を対象外にしました。
- `[vector_db] query_prefix`・`document_prefix` の既定を `auto` にし、埋め込みモデルの名前（`multilingual-e5`・`ruri`・`nomic-embed` 等）から
  検索語・文書に付ける接頭辞を自動で決めるようにしました。実際に付ける値はナレッジ画面に出ます。取り込みに使った `document_prefix` を
  コレクションごとに記録し、設定と違うときは埋め込みモデルを変えたときと同じく再構築を求めます。
- ナレッジの取り込みで、埋め込みサーバーへの接続をファイルごとに作り直さず、取り込み全体で使い回すようにしました。
  検索（`search_documents`）でも、埋め込みサーバーと Qdrant への接続をプロセス内で使い回すようにしました。
  Windows で `embedding_url`・`qdrant_url` が `localhost` のとき、接続のたびに約2秒待たされて取り込み・検索が遅くなっていました。
- ナレッジのコレクションごとに、時刻（複数可）と曜日を指定して差分取り込みを自動で実行できるようにしました。
  コレクションの追加フォーム・「編集」ダイアログで設定し、一覧に次回の予定日時が出ます。設定ダッシュボード（管理ツール）が
  起動している間だけ実行します。
- 埋め込みの接続先（`[vector_db] embedding_url`）が複数あるとき、接続できない場合だけでなくエラー（HTTP 200 以外）が返った場合も
  次の接続先へ切り替えて送り直すようにしました。すべての接続先でエラーになったときは `[vector_db] embedding_retry_interval_seconds`
  （既定10）秒おきに試し直します。回数は取り込みが `embedding_max_retries`（既定3。`0` にすると成功するまで無制限で、
  ナレッジ画面の「停止」で止められます）、検索（`search_documents`）が `search_embedding_max_retries`（既定2。`0` は試し直さない）です。接続先の選び方を `[vector_db] embedding_routing_strategy`（`[llm] main_routing_strategy` と同じ
  `round_robin`・`random`・`priority_failover`、既定は従来どおり先頭を優先する `priority_failover`）で選べるようにしました。
  設定ダッシュボードの「ベクターDB」プリセットにもこれらを加えました。

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
