# openpyxlで書くときにハマりやすいAPI（グラフ等）と、edit_excel.pyを迂回しない理由

`excel-edit`はopenpyxlのコードをそのまま実行するため、openpyxlでできることは
基本的に何でもできる。その分、openpyxlのAPIを記憶違いで書いて迷走しやすい。
旧ops方式の時代に、opに無い要求（積み上げ棒グラフ＋折れ線）を
`execute_python_code`の生openpyxlで実現しようとして20ターン超迷走し、
`.xlsm`を壊した実例がある。そこで実際に出た誤りを残す。

## 実際に出たAPIの誤り

- `bar_chart.type = "stacked"` → openpyxlに存在しない。積み上げは
  `chart.grouping = "stacked"`（100%は`"percentStacked"`）と
  **`chart.overlap = 100`のセット**で指定する（overlapが無いと積み上がって見えない）。
  `chart.type`は`"col"`（縦棒）/`"bar"`（横棒）の向き指定。
- `Series(value=...)` → 正しくは`Series(values=...)`。そもそも`Series`を手組み
  せず、`chart.add_data(Reference(...), titles_from_data=True)`を使う。
- `chart.series[0]`を`add_data`前に参照して`IndexError`。系列は`add_data`後に
  できる。系列色を付ける`style_chart(chart)`も`add_data`の後に呼ぶ。
- 棒＋折れ線の複合グラフは`bar += line`（`BarChart`へ`LineChart`を`+=`で合成し、
  `bar`を`add_chart`する）。`bar + line`は`TypeError: Cannot combine instances of
  different types`になる。第2軸が必要なら合成前に`line.y_axis.axId = 200`、
  `line.y_axis.crosses = "max"`。

## `execute_python_code`で生openpyxlを書いて迂回しない

`edit_excel.py`は`.xlsm`読込時に自動で`keep_vba=True`を付け、保存前に
バックアップを取る。`execute_python_code`で`load_workbook`→`save`を自前で
書くとこれが保証されず、**マクロが消える、またはファイル形式が壊れて
「Excel でファイル '...' を開くことができません。ファイル形式または
ファイル拡張子が正しくありません。」で開けなくなる**。openpyxlで書けることは
すべて`edit_excel.py --code`で書けるので、迂回する理由は無い。

## それでも実現できない要求

ピボットテーブル・画像のトリミング・Excelでの再計算結果の取得など、
openpyxl自体が対応していないものは、コードを工夫しても実現できない。
その時点で試行錯誤をやめ、制約をユーザーに伝えて代替案
（ピボットは`excel-vba-edit`、再計算は`excel-recalc`等）を提示する。
**同じエラーで2回続けて失敗したら、それ以上APIを推測で書き換えず報告する**
（迷走の抜け出し方は[[raw-openpyxl-xlsm-fallback]]参照）。
