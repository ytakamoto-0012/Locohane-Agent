---
name: skill-creator
description: 自分専用のドラフトスキル（未検証のスキル）を作る・既存スキルの改善案を作る・本文やスクリプトを書き直す・実際のローカルLLMで繰り返し評価する（スキル安定化トライアウト）・descriptionのトリガー精度を上げる、ためのメタスキル。「新しいスキルを作りたい」「スキルを作って」「このスキルを直したい」「スキルがちゃんと動くか試したい」「スキルのトリガー精度を上げたい」「自分のドラフトを見せて」など、スキル自体の作成・改善・検証の依頼があれば、ユーザーが「skill-creator」と言わなくても必ず使う。
license: MIT
metadata:
  author: ytakamoto
  version: "2.0"
---

# skill-creator

ドラフトスキルを作り、ローカルLLMで評価して直す、を繰り返すためのスキル。

## 必ず守ること

- 作るのは**ドラフト**だけ。正式スキル（skills/ 等）は書き換えない。既存スキルを直したいときは `fork_skill.py` で改善案のドラフトを作る。
- ドラフトは作った人の会話でだけ、スキル名 `ユーザー名/スキル名` として一覧に出る（作成・削除の次のメッセージから）。他の人には見えない。
- 正式スキルにするのはスキル開発者の仕事。完了したら「正式化はスキル開発者に依頼してください」と伝える（開発者は管理ツールのスキル研究室で仕上げてから正式化する。正式化されたドラフトは一覧から消える）。
- 評価（`run_isolated_eval.py` `run_trigger_eval.py`）は1件ずつ。`status` が `finished` になるまで次を `start` しない。`running` の間は1分ほど待ってから `status` を呼ぶ。
- このスキルのスクリプトは自分で直接実行できる（委譲しない）。
- ドラフトを1か所でも直すと、トライアウトの合格回数は0に戻る。直した後は `--repeat 10` から評価し直す。

## 流れ

1. 何をするスキルか・どんな発話で使うか・出力の形を確かめる。
2. ドラフトを作る（新規は `scaffold_skill.py`、既存スキルの改善は `fork_skill.py`）。
3. `write_draft_file.py` で SKILL.md・scripts/・references/ を書く。
4. `validate_skill.py` で確認する。
5. `make_eval_case.py` で評価ケースを2〜3件作る。
6. `run_isolated_eval.py` で評価し、結果を見て 3 に戻る。
7. 安定したら `--repeat 10` で評価する（スキル安定化トライアウト）。
8. 結果をユーザーに報告する。

## 1. ドラフトを作る

新規（スクリプトも使うなら `--with-script` を付ける）:
```
python scaffold_skill.py --name my-skill --description "何をするか。どんな発話で使うか。" --with-script
```

既存スキルの改善案（正式スキルはそのまま残る。正式スキルの評価ケースも写される）:
```
python fork_skill.py --name excel-read
```

自分のドラフトの一覧:
```
python list_drafts.py
```

## 2. 中身を書く

`--name` は自分のドラフトならスキル名だけでよい。

ファイル全体を書く:
```
python write_draft_file.py --name my-skill --path SKILL.md --content "---
name: my-skill
description: ...
---

# my-skill
..."
```

一部だけ置き換える（`--old` はファイル内で1か所だけ一致させる）:
```
python write_draft_file.py --name my-skill --path SKILL.md --old "古い文" --new "新しい文"
```

スクリプトを書く（長い内容は作業フォルダのファイルから写す）:
```
python write_draft_file.py --name my-skill --path scripts/run.py --content-file <作業フォルダのファイル>
```

ファイルやドラフトを消す:
```
python delete_draft.py --name my-skill --path references/old.md
python delete_draft.py --name my-skill
```

書き方:
- description が唯一のトリガー手がかり。「何をするか」と「どんな発話で使うか」を具体的に書く。
- SKILL.md 本文は500行以内。長くなるなら references/ に分ける。
- スクリプトの呼び出し例は `python <script>.py <args...>` の形で書き、出力キーの意味も書く。
- スクリプトは正常時に終了コード0で1行のJSON、異常時は終了コード1で標準エラーに理由を出す。

確認:
```
python validate_skill.py --name my-skill
```

## 3. 評価ケースを作る

```
python make_eval_case.py --name my-skill --case-id 001_basic --turns "[\"ユーザーが実際に打ちそうな発話\"]" --expect "{\"tool_call_args_contains\": {\"read_skill\": {\"skill_name\": \"my-skill\"}}}"
```

judge（自由記述の判定観点）も付ける:
```
python make_eval_case.py --name my-skill --case-id 002_output --turns "[\"発話\"]" --expect "{\"tool_called_any\": [\"run_script\"]}" --judge "出力に〇〇が含まれ、捏造が無いか"
```

- 評価中のスキル名は `my-skill`（ユーザー名なし）。expect にもスキル名だけを書く。
- `--expect` の主なキーは `references/schemas.md` を参照。
- 結果が客観的に決まらない（文章のトーン等）なら judge を使う。

## 4. 評価する

全ケースを1回ずつ:
```
python run_isolated_eval.py start --name my-skill
```

比較用にドラフト無しで（新規ならスキル無し、改善案なら正式スキルのまま）:
```
python run_isolated_eval.py start --name my-skill --mode without_skill
```

一部のケースだけ、3回ずつ:
```
python run_isolated_eval.py start --name my-skill --case 001_basic --case 002_output --repeat 3
```

スキル安定化トライアウト（各ケース10回。全回合格で合格）:
```
python run_isolated_eval.py start --name my-skill --repeat 10
```

結果:
```
python run_isolated_eval.py status --name my-skill --job-id <job_id>
```

- `verdict` が `pass` なら全回合格、`fail` なら不合格あり、`needs_judge` なら `runs` の `judge` と `final_answer` を読んで自分で判定する。判定の根拠は報告に書く。
- `stale: true` なら評価中にドラフトが変わっている。結果は使わず、もう一度 `start` する。
- with_skill と without_skill の比較表: `python aggregate_results.py --name my-skill --input with_skill=<results_path> --input without_skill=<results_path>`

## 5. 直し方

- フィードバックは一般化して書く。目の前の例だけに効く「必ず〜」を並べない。
- 冗長な手順は削る。
- 同じ処理を何度も書くなら scripts/ に1つまとめる。
- 直したら同じケースで評価し直す。
- スキル開発者に差し戻されたドラフトは、`list_drafts.py` の `returned_reason` を読んで直す。直すと自動で再提出の状態に戻る。

## 6.（任意）トリガー精度を上げる

1. 使うべき発話と、紛らわしいが使うべきでない発話を合わせて10〜20件、JSONファイルにする（形式は `references/schemas.md`）。
2. `python run_trigger_eval.py start --name my-skill --eval-set <JSONファイル> --repeats 3` → `status` で `accuracy` と `per_query` を見る。
3. `matched: false` の項目をJSONファイルにして `python propose_description.py start --name my-skill --failed-queries <JSONファイル>` → `status` で改善案を受け取る。
4. 改善案を `write_draft_file.py` で description に反映し、もう一度 2 を回す。

## 参考資料

- `references/schemas.md`: 各スクリプトの引数と出力JSON。
- `skills/SKILLS_README.md`: SKILL.md の書式と scripts/ の規約。
