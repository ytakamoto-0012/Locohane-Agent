# edit_excel.pyの基本契約（コード必須・--newの挙動・wbの扱い）

`excel-edit`（`edit_excel.py`）は「openpyxlのコードを受け取り、渡した`wb`を
編集させてから保存する」スクリプトであって、ファイルのコピー・復元ツールではない。

## `--code`/`--code-file`はどんな用途でも必須

`--code`と`--code-file`は`argparse`の`mutually_exclusive_group(required=True)`
で定義されており、**どちらか一方を必ず渡さないと即エラーになる**。

旧ops方式の時代に、バックアップファイルを本番ファイルへ復元しようとして
入力ファイルと`--output`だけを渡して失敗し、さらに**`--overwrite`を足しただけの
同じコマンドを再実行して同じエラーで再度失敗**した事例がある
（`--overwrite`は「`--new`時の上書き許可」フラグで、引数不足のエラーとは無関係。
再試行前にエラー原因と変更点が対応しているか確認する一般原則は
[[error-message-first-retry]]参照）。

**ファイルのコピー・バックアップ復元自体は`execute_python_code`の
`shutil.copy2`等で行い、その後に`excel-edit`でコードを適用する、
という2段階に分けること。**

## `--new`は0シートの空ブックから始まる

`--new`指定時の実装は次の通り：

```python
wb = openpyxl.Workbook()
wb.remove(wb.active)
```

つまり**既定の「Sheet1」は最初から存在しない**（openpyxlの一般知識として
「新規ブックには既定シートがある」と思い込みがちだが、`--new`は生成直後に
それを削除している）。`wb.active`や`wb["Sheet1"]`をいきなり触ると
`AttributeError`/`KeyError`になる。**`--new`のコードは必ず
`ws = wb.create_sheet("名前")`から始める。**

## `wb`は作り直さない・保存しない

- `wb.save()`はエラーになる（保存は`edit_excel.py`が、上書き前のバックアップを
  取ってから行う）。別名で保存したいなら`--output`。
- `wb = openpyxl.load_workbook(...)`/`wb = openpyxl.Workbook()`で差し替えると
  エラーになる（`.xlsm`のマクロ保持〔`keep_vba=True`〕と`--new`の上書きガードが
  効かなくなるため）。
- コードが例外で止まった場合、ファイルは一切保存されない。直して同じコードを
  丸ごと再実行すればよい（前回の途中まで反映された状態を気にする必要は無い）。

## `--query-json`（excel-read）と`--code`（excel-edit）を混同しない

`excel-read`のフラグは`--query-json`であって`--query`ではない。フラグ名の
打ち間違いは`unrecognized arguments`にしかならず気づきにくいので、
フラグ名は都度SKILL.mdの表記をそのままコピーする。
