---
name: excel-knowledge
description: xlsx/xlsmファイルをexcel-edit/excel-read/excel-render/excel-recalcスキルで作成・編集・確認する際のコーディング作法・定石・よくある落とし穴について、実際の作業ログから蓄積したローカルの知識ベース（references/配下のノート）を参照するスキル。ユーザーが「表を作りたい」「既存のxlsx/xlsmを修正したい」といった依頼そのものではなく、excel-edit等のスキルを呼び出す際に「引数エラーが出た」「同じエラーを繰り返している」「excel-editに渡すopenpyxlコードのAPIの書き方で詰まった」「積み上げ・複合グラフの書き方が分からない」「openpyxlで実現できない要求にどう対処すべきか」「ファイルが壊れて開けなくなった」など、ツール呼び出し自体で詰まった/迷走しかけたときに使う。excel-edit/excel-read/excel-render/excel-recalcで実際にxlsx/xlsmを読み書きする前後の下調べ・トラブルシューティング段階で使うことが多い。VBAマクロの読み書き・エラーはexcel-vba-knowledge（excel-vba-read/excel-vba-edit用）の担当でこのスキルの対象外。ユーザーの依頼でExcel操作用のPythonスクリプト（openpyxl等を使った単体スクリプト）を新規に書き起こすタスクもこのスキルの対象外。
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-knowledge

xlsx/xlsm を扱う `excel-edit`/`excel-read`/`excel-render`/`excel-recalc` スキルの
呼び出しでハマりやすい点をまとめたローカル知識ベース。
スクリプトは持たず、`read_skill_file` で
`references/` 配下のノートを読むだけのスキル。

## 使い方

1. このSKILL.mdの本文（`read_skill` で既に読んでいる）にある下記「索引」から、
   相談内容に近いトピックを選ぶ。複数該当してもよい。
2. `read_skill_file` で該当ノートを読む。**`relative_path` には必ず
   `excel-knowledge/references/<ファイル名>` のようにスキルフォルダ名を
   先頭に含めること**（`references/edit-excel-invocation-contract.md` の
   ようにスキルフォルダ名を省略すると見つからない）。
3. ノート内に `[[別のノート名]]` という参照があれば、必要に応じてそちらも読む
   （例: `[[error-message-first-retry]]` は
   `references/error-message-first-retry.md` を指す）。
4. ノートの内容はそのままコピペ提示するのではなく、実際に呼び出そうとしている
   op・引数・エラーメッセージに当てはめて具体的な修正案として提示する。
5. 索引に該当するトピックが無い、またはノートを読んでも解決しない場合は、
   その旨をユーザーに伝えたうえで各スキル自身のSKILL.mdを`read_skill`で
   読み直す・`web-search`スキルでの調査を検討する。
6. 実際にxlsx/xlsmを読む・書き換える作業は`excel-edit`/`excel-read`/
   `excel-render`/`excel-recalc`スキルの担当。このスキルは「呼び出す前に
   引数・openpyxlのAPIの正しい形を確認する」「エラーが出たときに原因の当たりを付ける」
   ための下調べに使う。

## 索引

| トピック | ファイル | 内容 |
|---|---|---|
| excel-read/excel-renderの引数・query | `excel-knowledge/references/read-render-args-and-queries.md` | `read_excel.py`/`render_excel.py`の引数はSKILL.md記載のもので全て、`--query-json`の対応opは`group_by`/`list_images`/`list_charts`/`print_area`のみ、存在しない引数・op名を当てずっぽうで発明しない |
| edit_excel.pyの呼び出し契約 | `excel-knowledge/references/edit-excel-invocation-contract.md` | `--code`/`--code-file`はどんな用途でも必須（コピー・復元ツールとして使えない）、`--new`は0シートの空ブックから始まる（Sheet1は存在しない）、`wb.save()`・`wb`の作り直しは禁止、`--query`と`--query-json`の混同 |
| openpyxlでハマりやすいAPI | `excel-knowledge/references/beyond-excel-edit-capabilities.md` | 積み上げ棒グラフ（`grouping`+`overlap`）・`Series(values=)`・複合グラフ（`bar += line`）等の実際に出た記憶違い。`execute_python_code`の生openpyxlで迂回してxlsmを破損させた実例、openpyxl自体が対応しない要求の扱い |
| フォント名変更など既存書式の一部変更 | `excel-knowledge/references/raw-openpyxl-xlsm-fallback.md` | Fontはcopy()してから.nameだけ変更する（新規Font代入だとbold/size/colorが消える）、render_excel失敗=破損とは限らない、同じ仮説を実行せず繰り返す迷走への対処 |
| エラーメッセージを読んでからの再試行 | `excel-knowledge/references/error-message-first-retry.md` | エラーメッセージの型ごとの読み方、再試行前に変更点とエラー原因が対応しているか確認する、2回連続で同種エラーが出たらread_skillでSKILL.mdを読み直す |
| ツール権限の境界 | `excel-knowledge/references/tool-permission-boundaries.md` | readonly系サブエージェント等、権限外の書き込み系スクリプト実行をコードでシミュレートしようとしない |

## ノートの追加・更新について（開発者向け）

このスキルの `references/` は、実際の作業ログから見つかった失敗パターンや
Web調査で得た知見を**開発者（人間、またはこのプロジェクトを保守するClaude Code）が
事前に**書き足していく運用を想定している。

Locohaneの実行時LLM（このスキルを使う本体）は、スクリプト実行系ツールの
書き込み先が作業ディレクトリ配下に限定されるサンドボックス制約があり、
`skills/` 配下（このスキル自身のフォルダ）へはチャット実行中に書き込めない。そのため、**チャット中に得た新しい知見をこの
知識ベースへ自動で書き戻す機能は無い**。会話の中で有用な新知見が得られた場合は、
ユーザーまたは開発者に「このノウハウを`excel-knowledge`に追記してよいか」を
確認し、後日（アプリの外から）ファイルを追加する運用にすること。

新しいノートを追加する手順:

1. `references/<トピックを表す短い英語スラッグ>.md` を作成し、既存ノートと同じ
   Markdown形式（見出し・コード例・関連ノートへの`[[名前]]`リンク）で書く。
2. 上記「索引」の表に1行追加する（トピック名・ファイルパス・内容の要約）。
3. アプリ再起動は不要（`read_skill_file` はファイルをそのつど読むため）。
   ただし索引に載っていないノートはLLMから見つけてもらえないので、
   必ず索引への追記とセットで行う。
