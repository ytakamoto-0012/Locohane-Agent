# excel-read/excel-renderの引数・queryは当てずっぽうで発明しない

`excel-read`（`read_excel.py`）と`excel-render`（`render_excel.py`）は、それぞれの
`SKILL.md`に書かれている引数・queryの種類が**全て**であり、それ以外のフラグ・op名は
存在しない。実際のログでは、この事実を確認せず「こういう機能があるはずだ」という
思い込みで引数・op名を当てずっぽうに作り、同じエラーを何度も繰り返す事故が
最も頻発していた。

## read_excel.pyの引数はこれで全て

```
read_excel.py <file_path> [--sheet <名前 or 0始まり index>]
              [--offset <N>] [--limit <N>] [--data-only] [--style]
              [--query-json '<...>'] [--columns 'A,C,E']
```

- `--sheet`省略時はシート一覧のみ返す（値は読めない）。
- 行範囲を絞って読みたいだけなら`--offset`/`--limit`で足りる。
  **`--mode`のような追加フラグは存在しない。**
- シート名やモード名を**位置引数**として並べて渡す間違いも起きた
  （例: `script_args: ["book.xlsm", "取引明細表", "all"]`）。`read_excel.py`の
  位置引数は`file_path`1個だけで、シート指定は必ず`--sheet`。
- 既定は軽量モード（value/number_format/merged_cells/tablesのみ）。太字・背景色・
  罫線等のフルstyle情報が要るときだけ`--style`を付ける（正確な引数一覧・出力形式は
  `read_excel`のSKILL.md参照。ここは要点のみ）。
- 列を絞り込みたいときは`--columns "A,C,E"`（列アルファベット/1始まり列番号の
  カンマ区切り、`--sheet`必須）。返り値の`columns`キーが今回`rows`に含まれる
  列アルファベットの配列で、`rows`の各行の`j`番目は必ず`columns[j]`列の値。
  列数が多い表を一度に全列読まず、必要な列だけに絞る方が安全。

## `--query-json`が対応するopは`group_by`/`list_images`/`list_charts`/`print_area`の4つだけ

```
_QUERY_HANDLERS = {"group_by": ..., "list_images": ..., "list_charts": ..., "print_area": ...}
```

セル範囲をまとめて読みたい・列幅を知りたい・特定条件で絞り込みたい、といった
自然な発想から`get_rows`/`get_column_width`/`range`のような**存在しないop名**を
当てずっぽうで送り、下記のエラーを受け取った実例が複数セッションで独立に発生した。

```
未対応のqueryです: 'get_rows'（対応op: ['group_by', 'list_charts', 'list_images', 'print_area']）
```

このエラーメッセージ自体に対応op一覧が含まれているにもかかわらず、次の試行でも
別の架空のop名（さらには`--mode rows --start-row 130 --end-row 142`という
架空のフラグ体系）に切り替えて再度失敗し、3回目でようやく`read_skill`し直して
気づいた事例がある。**「対応opエラー」を受け取ったら次のopをまた推測せず、
即座に`read_skill`でSKILL.mdのquery節を再読すること。** セル範囲の値そのものを
知りたいだけなら、`--query-json`ではなく通常モードの`--offset`/`--limit`で
代替できる（excel-editでグループの前後へ行を挿入する前の
グループ構成確認に使う`group_by`、グラフ確認用途の`list_charts`、印刷範囲確認用途の
`print_area`以外は、基本的に通常モードで足りる）。

## render_excel.pyの引数は`excel_path`と`--print-as-is`だけ

```
render_excel.py <excel_path> [--print-as-is]
```

シート指定・ページ指定・部分出力のオプションは無い（全シートを常に丸ごと
画像化する単純な仕様）。`--print-as-is`は印刷設定の強制フィットを止める
オプションであり、シートや出力範囲を選ぶものではない。それにもかかわらず
`--sheet`/`--pages`を付与して以下のエラーになった実例がある。

```
usage: render_excel.py [-h] [--print-as-is] excel_path
```

`read_excel.py`が`--sheet`を持つことに引きずられ、別スクリプトにも同じ引数体系が
あるはずだと類推したのが原因とみられる。**あるスキルの引数体系を、別スキルに
そのまま類推適用しない。** 特定シートだけ見た目を確認したい場合でも、
`render_excel.py`は全シート出力しかできない前提で運用する。

## エラーの`usage:`行や「対応op」一覧はそのまま正解

argparseの`unrecognized arguments`エラーに付随する`usage:`行、および
`--query-json`の「対応op」一覧は、いずれもその場で確認できる**唯一の正解**。
再試行の前に一度立ち止まってこれらを読み、正しい引数・op名で組み立て直すこと。
（一般原則は[[error-message-first-retry]]参照）
