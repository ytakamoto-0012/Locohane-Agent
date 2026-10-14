---
name: excel-edit
description: xlsx/xlsmファイルの新規作成・既存編集を行うスキル。openpyxlのPythonコードを書いて実行する方式で、セル値・書式（フォント/背景色/罫線/列幅/行高/結合）の設定、行列の挿入削除、グラフ追加・調整、画像の追加・位置調整、条件付き書式、データ検証、構造化テーブルなどopenpyxlでできることは何でも行える（テーマ配色の表仕上げ・グラフ配色・結合セルを保つ行挿入などのヘルパー付き）。Excel本体は不要。数式文字列は書き込めるが計算はしない（計算結果の確認・エラーセル検出はexcel-recalcスキルを使う）。ユーザーが表データを新規作成/編集したいとき、書式付きのレポートやグラフ・画像入りのExcelを出力したいとき、既存のxlsx/xlsmにデータや装飾を追記・修正したいときに使う。読み込み専用ならexcel-read、VBAマクロの読み書きはexcel-vba-read/excel-vba-edit、見た目の画像確認はexcel-renderを使う。
license: MIT
metadata:
  author: ytakamoto
  version: "2.0-openpyxl-experiment"
---

# excel-edit

xlsx/xlsm の新規作成・既存編集を行うスキル。**openpyxl のPythonコード**を `edit_excel.py` に渡して実行する。

## 呼び出し

```bash
python edit_excel.py "C:\Users\me\book.xlsx" --code 'ws = wb["Sheet1"]
ws["A1"] = "合計"'
```

- コードには読み込み済みの openpyxl Workbook が**変数 `wb`** として渡される。`wb` を書き換えるだけでよい。
- **`wb.save()` を書かない・`load_workbook()`/`Workbook()` で `wb` を作り直さない**（どちらもエラー）。読み込み・保存・上書き前のバックアップはスクリプトが行う。
- 新規作成: 末尾に`--new`を追加（既存ファイルがあれば`--overwrite`も必須、無いとエラー）。`--new`の`wb`は**シートが0枚**（`Sheet1`は無い）なので、必ず`wb.create_sheet("名前")`から始める。
- `--new`なし: 対象パスを読み込んで編集（不在ならエラー）。触れなかった既存セル・書式・シートはそのまま保持。
- `--output`省略で対象パスへ上書き保存。別ファイル保存時のみ`--output <絶対パス>`を追加。
- `.xlsm`はマクロを保持したまま編集可（`--new`なし時のみ。スクリプトが`keep_vba=True`で読み込むため）。マクロ自体の読み書きはexcel-vba-read/excel-vba-editスキル。
- **`--new`かつ`.xlsm`は「マクロ有効ブック」にならない**（拡張子が`.xlsm`でも中身はxlsx相当になり、Excelで開けない/マクロを追加できないファイルができる）。表データ・書式とVBAマクロの両方を持つ新規`.xlsm`は、**先にexcel-vba-editスキルの`--new`で空のマクロ有効ブックを作り、その後このスキルを`--new`なしで使って同じパスにシート・データを追記する**（逆順不可）。

## 入出力の型とコードの渡し方

成功=終了コード0＋JSON1行を標準出力（`{"path":...,"backup_path":...,"sheets":[...]}`。コード内で`print`した内容は`code_output`に入る）。失敗=終了コード非0＋エラーメッセージを標準エラー（**コードの何行目で何の例外が出たか**が載る。シート名の誤りならブックのシート一覧も載る）。失敗時は**ファイルを一切保存しない**ので、エラー箇所だけ直して同じコードを再実行すればよい。未導入ライブラリは`ImportError`終了コード1（原因: `openpyxl`）→`pip install openpyxl`をユーザーに促す。

コードは通常`--code '<コード>'`で渡す（改行を含んでよい）。コードが80行を超える場合、または`--code`でクォートの崩れによる構文エラーが1回でも出たら、コードを`.py`ファイルへ書き出して`--code-file <絶対パス>`で渡す。

```bash
python edit_excel.py "C:\Users\me\book.xlsx" --code-file "C:\Users\me\edit.py"
```

## コード内でimportなしに使える名前

- `wb`（編集対象のWorkbook）、`openpyxl`
- `Font`, `PatternFill`, `Border`, `Side`, `Alignment`, `get_column_letter`, `column_index_from_string`
- `BarChart`, `LineChart`, `PieChart`, `ScatterChart`, `Reference`
- `Table`, `TableStyleInfo`, `DataValidation`, `CellIsRule`, `ColorScaleRule`, `DataBarRule`, `FormulaRule`, `IconSetRule`
- 下記ヘルパー関数と定数`THEMES`/`ROLE_COLORS`/`CHART_PALETTE`

それ以外のopenpyxlの機能は普通に`from openpyxl... import ...`して使ってよい。

## ヘルパー関数（openpyxlだけだと崩れやすい処理）

| 関数 | 説明 |
|---|---|
| `write_table(ws, "A1", rows, theme=None, header=True, row_styles=None, style=None, banded=True, freeze_header=True)` | `rows`（見出し行を含む2次元リスト）を書き込み、`header=True`なら見出し配色・罫線・縞模様・見出し行固定・列幅調整まで仕上げる。戻り値は書いた範囲（例`"A1:C4"`） |
| `format_table(ws, "A1:C10", theme=None, header_fill=None, header_font_color=None, banded=True, border="thin", freeze_header=True, autofit=True)` | 書き込み済みの表（1行目=見出し）を後から仕上げる。本体行の文字色・太字は変えない |
| `apply_style(ws, "A1:C3", style)` | 範囲へstyle辞書（下記）を適用 |
| `autofit_columns(ws, "A1:C10")` | 値から列幅を調整（全角=2文字換算、既存幅より縮めない。範囲省略でシート全体） |
| `insert_rows(ws, idx, n)` / `delete_rows(ws, idx, n)` / `insert_cols(ws, idx, n)` / `delete_cols(ws, idx, n)` | 行列の挿入・削除（1始まり）。**`ws.insert_rows`等を直接使わずこちらを使う**（openpyxl本体は既存の結合セルを追従させず表が壊れるため） |
| `find_group(ws, "A", "7月")` | A列の値が`"7月"`の連続区間の`(開始行, 終了行)`を返す（結合セル・空欄は直前の値の区間に含む）。値は型も含め厳密一致（数値セルなら`7`で探す） |
| `list_groups(ws, "A")` | A列の連続区間を`[{"value","start_row","end_row","row_count"}, ...]`で返す |
| `style_chart(chart)` | グラフへ色覚多様性に配慮した系列色とデータラベル（値。円グラフは%）を付ける。`add_data`の後に呼ぶ |
| `place_image(ws, path, left_cm, top_cm, width_cm=None, height_cm=None)` | シート左上原点からのcm座標へ画像を追加（幅・高さ片方のみ指定でアスペクト比維持、両方省略で原寸） |
| `move_image(ws, image_index, left_cm=..., top_cm=..., width_cm=..., height_cm=...)` | 既存画像（シート内0始まり、追加順）の位置・サイズのうち指定したものだけ変更 |

## style辞書（`write_table`/`apply_style`で共通）

```python
{"bold": True, "italic": False, "font_color": "0000FF", "font_size": 11,
 "fill_color": "FFFF00", "number_format": "#,##0.00",
 "align": "center", "valign": "center", "wrap_text": False,
 "border": "thin", "role": "input"}
```
全キー省略可。色はRRGGBB16進数。`border`は`"thin"`/`"medium"`/`"thick"`（4辺一括）または`{"top":"thin","bottom":"thin"}`。openpyxlの`Font`等を直接セルへ代入してもよい。

`role`によるセル色分け（推奨）— `font_color`の代わりに`role`を指定すると規約色になる: `input`=青`0000FF`（ハードコードした入力値）、`formula`=黒`000000`（同一シート内の数式）、`link`=緑`008000`（他シートを参照する数式）。

## テーマ配色（theme）

`write_table`/`format_table`の`theme`に以下から選ぶと見出し配色を一括指定できる（`pptx-create`スキルと同じ名前・配色）: `charcoal`（既定相当）/`navy`/`forest`/`coral`/`terracotta`/`ocean`/`teal`/`berry`。色の値は`THEMES["navy"]["primary"]`等で参照できる。グラフの系列色はテーマによらず`style_chart`の固定8色パレット（`CHART_PALETTE`）を使う。

## 美しい表を作る基本レシピ（必須ルール）

**表形式データ（レポート・一覧・予定表等）は`write_table`で書く**。見出し配色・罫線・縞模様・見出し行固定・列幅調整が自動で付く。`fill_color`/`font_color`/`border`を1セルずつ自前で組み立てるのは避ける（コードが長くなり見た目もバラつく）。見出しなしの生データだけ`header=False`。**見出し行とデータ行は同じ`rows`にまとめる**（別々に書かない）。

```python
ws = wb.create_sheet("年間予定")
write_table(ws, "A1", [["月","行事","担当"], ["4月","入学式","田中"], ["5月","遠足","佐藤"]], theme="navy")
```

## 行ごとに異なる背景色を付けたいとき（必須ルール）

**書き込み後に行番号を数えて塗り直さない**（見出し行を含めるかの数え間違いで1行ズレる）。`rows`と**同じ要素数**の`row_styles`（各要素は1行分のstyle辞書、色なしは`None`）を`rows`と同時に組み立てて渡す。1要素目は見出し行に対応。

```python
data = [("1月","役員選考"), ("1月","新年会"), ("2月","ひな祭り")]
colors = {"1月": "DAEEF3", "2月": "F8CBAD"}
rows = [["月","行事名"]] + [list(r) for r in data]
row_styles = [None] + [{"fill_color": colors[r[0]]} for r in data]
write_table(ws, "A2", rows, row_styles=row_styles)
```

## 同じ値が続く列はセルを結合する（月・区分・期間などのグルーピング列）

同じ値が2行以上連続する列（例:「月」列）は結合する。表を書いた後に`list_groups`で区間を求めて`merge_cells`すれば、行番号を手計算しなくてよい。

```python
write_table(ws, "A1", [["月","週"], ["1月","第1週"], ["1月","第2週"], ["2月","第1週"]])
for g in list_groups(ws, "A"):
    if g["start_row"] > 1 and g["row_count"] > 1:
        ws.merge_cells(start_row=g["start_row"], start_column=1, end_row=g["end_row"], end_column=1)
        ws.cell(row=g["start_row"], column=1).alignment = Alignment(vertical="center")
```

## グルーピングされた表の、既存グループの前後へ行を挿入したいとき（必須ルール）

**挿入位置の行番号を目で数えて決めない**。`find_group`で挿入位置を求め、`insert_rows`（ヘルパー版）で空け、そこへ書く。複数グループを挿入するときは、**挿入のたびに`find_group`を呼び直す**（前の挿入で後ろの行番号がずれるため）。

```python
ws = wb["月間予定表"]
start, end = find_group(ws, "A", "7月")      # 7月の前に挿入（後ろなら idx = end + 1）
new_rows = [["6月","ふれあい祭り出店","06/14"], ["6月","夏休み前最後の行事","06/28"]]
insert_rows(ws, start, len(new_rows))
for i, row in enumerate(new_rows):
    for j, v in enumerate(row):
        cell = ws.cell(row=start + i, column=1 + j, value=v)
        cell._style = ws.cell(row=start + len(new_rows), column=1 + j)._style  # 下の行の書式に揃える
```

## 既存の表の一部列だけを直したいとき（必須ルール）

**複数列ある既存の表のうち一部の列だけを直すために、行を削除してその列分だけ書き直す、ということはしない**（書かなかった列の値・罫線・背景色が消える）。直したいセルだけ`ws["B5"] = 新しい値`のように個別に代入する（値だけ代入すれば既存の書式は残る）。

## グラフ

```python
ws = wb["売上"]
ch = BarChart()
ch.title = "月次実績"
ch.add_data(Reference(ws, min_col=2, min_row=1, max_col=3, max_row=13), titles_from_data=True)  # 見出し行込み
ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=13))
style_chart(ch)
ch.width, ch.height = 18, 9   # cm
ws.add_chart(ch, "E2")
```

- 積み上げ棒グラフ: `ch.grouping = "stacked"`（100%は`"percentStacked"`）と**`ch.overlap = 100`をセットで**指定する（overlapが無いと積み上がって見えない）。
- 既存グラフは`ws._charts`（シート内0始まり、追加順）で取り出して`title`/`width`/`height`/`anchor`を変更、削除は`del ws._charts[i]`。**テンプレートに元から入っていたグラフ**はopenpyxlがサイズを正しく復元しないため、触る場合は`width`/`height`を明示する。
- **1系列は単一シート内の連続範囲しか指定できない。** 複数シートに散らばった値（「1月〜12月シートそれぞれのB11」等）をグラフにするときは、集計用シートを作って各セルに`"='1月'!B11"`のような参照数式を書き、その集計シートを範囲にする（`Series`を手組みしない）。

## 画像

`place_image`/`move_image`を使う（openpyxlの`Image`を直接`add_image(img, "A1")`してもよいが、cm単位の位置・サイズ指定はヘルパーの方が確実）。既存画像の位置・番号は`excel-read`の`--query-json '[{"op": "list_images"}]'`で確認できる。**テンプレートに元から入っていた画像**を初めて動かすと絶対座標配置に置き換わり、指定しなかった軸は0cm起点になるので、`left_cm`/`top_cm`は両方明示する。**画像のトリミング（切り抜き）は非対応**（openpyxlに安定したAPIが無くファイル破損のリスクがある）。頼まれたら「excel-editでは画像のトリミングに対応していません」と伝え、トリミング済み画像を別途用意してもらう。

## 条件付き書式・データ検証

```python
ws.conditional_formatting.add("C2:C100", ColorScaleRule(start_type="min", start_color="FFFFFF", end_type="max", end_color="FF0000"))
ws.conditional_formatting.add("D2:D100", CellIsRule(operator="greaterThan", formula=["100"], fill=PatternFill("solid", fgColor="FFC7CE")))
dv = DataValidation(type="list", formula1='"OK,NG,保留"', allow_blank=True)
dv.prompt, dv.showInputMessage = "OK/NG/保留のいずれかを選択", True
ws.add_data_validation(dv)
dv.add("E2:E100")
```

## その他のよく使う操作

- シート: `wb.create_sheet("名前", index)`（0始まり。同名を作ると`名前1`に自動改名されるので、既存なら`wb["名前"]`を使う）、`wb.remove(wb["名前"])`、`ws.title = "新名"`（31文字まで）、`ws.sheet_properties.tabColor = "FF0000"`、開いたときの表示シートは`wb.active = wb.sheetnames.index("名前")`。
- 列幅・行高: `ws.column_dimensions["A"].width = 20`、`ws.row_dimensions[1].height = 30`。ウィンドウ枠固定: `ws.freeze_panes = "A2"`。
- 構造化テーブル: `t = Table(displayName="売上表", ref="A1:C10"); t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True); ws.add_table(t)`（`displayName`はブック内一意、空白不可）。
- 数式は`"=SUM(B2:B10)"`のように文字列で代入する（このスクリプトは計算しない。計算値確認はexcel-recalcスキル）。

## 手順とエッジケース

1. 対象ブックの構造（シート名・表の位置）が分からなければ、先にexcel-readスキルで確認する。
2. コードを書いて上記コマンドを実行する。
3. `--new`使用時に対象が既存で上書き可否不明なら、`--overwrite`なしで一度実行しエラーからユーザーに確認する。
4. 成功時の`path`をユーザーに伝える。失敗時はエラーに出た行だけ直して再実行する（ファイルは保存されていないので、前回の途中結果を気にしなくてよい）。

## デザイン確認（必須ルール）

デザイン・レイアウト（配色・罫線・列幅・グラフ配置・印刷時の見え方等）の調整を行う前後には、必ずexcel-renderスキル（`render_excel.py`＋`analyze_image`）でシートを画像として確認する。excel-readのセル値・style情報だけで見た目を判断して完了報告しない。

## 禁止事項

- `--new`かつ`--overwrite`なしで既存ファイルへ強制上書きする（エラーになった場合はユーザーに上書き可否を確認してから再実行する）。
- コード内での`wb.save()`、`load_workbook()`/`Workbook()`による`wb`の作り直し、対象ブック以外のファイルの書き換え。
- `.xls`への編集・生成（拡張子の対応外操作）。
