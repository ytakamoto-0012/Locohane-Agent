"""read_tool/glob_tool/grep_tool/json_query が共有する、ローカルファイルシステム
読込・検索の純粋ヘルパー。

src/memory.py と同じ契約: Chainlit・パスメモリー・作業ディレクトリ解決には
一切依存しない。呼び出し元（各ツールファイル）が解決済みの絶対パスを渡す
前提で、ツール本体側は ValueError を "エラー: ..." 形式の文字列へ変換する
薄いラッパーに徹する。
"""

from __future__ import annotations

import difflib
import locale
from pathlib import Path


def read_text_with_fallback(path: Path) -> str:
    """UTF-8 で読めなければ CP932（Shift-JIS系）、OS の ANSI コードページ（英語版 Windows なら
    cp1252 等）の順にフォールバックして読む。どれでも読めなければ UnicodeDecodeError を送出する。"""
    data = path.read_bytes()
    error: UnicodeDecodeError | None = None
    for encoding in dict.fromkeys(("utf-8", "cp932", locale.getencoding())):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError as e:
            error = error or e
        except LookupError:
            continue
    else:
        raise error or UnicodeDecodeError("utf-8", data, 0, len(data), "undecodable")
    # Path.read_text と同じく改行を \n にそろえる（universal newlines）。
    return text.replace("\r\n", "\n").replace("\r", "\n")


def looks_binary(path: Path) -> bool:
    """先頭1024バイトに NUL バイトが含まれるかでバイナリらしさを判定する。"""
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        return b"\x00" in chunk
    except OSError:
        return False


def suggest_similar_dir(path: Path) -> str:
    """存在しないディレクトリパスに対し、実在する近い候補をヒント文字列として返す。

    ローカルLLMがパスを記憶から手打ちで組み立て直した際にスペルミス・余分な
    空白・区切り文字の欠落を起こし、同じ失敗を繰り返すケースへの対策。
    実在する最も近い祖先ディレクトリまで遡り、そこから先の欠けている
    ディレクトリ名に似たものが兄弟ディレクトリの中にないか探す。

    Args:
        path: 存在しなかったディレクトリパス（解決済みの絶対パスを渡すこと）。

    Returns:
        候補が見つかればエラーメッセージに追記できる短いヒント文字列
        （先頭に半角スペース付き）。見つからなければ空文字列。
    """
    target = path
    ancestor = target
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or ancestor == target:
        return ""

    remainder = target.relative_to(ancestor)
    if not remainder.parts:
        return ""
    missing_name = remainder.parts[0]

    try:
        siblings = [child.name for child in ancestor.iterdir() if child.is_dir()]
    except OSError:
        return ""

    close = difflib.get_close_matches(missing_name, siblings, n=1, cutoff=0.5)
    if not close:
        return ""

    suggested = ancestor / close[0]
    return (
        f" もしかして {suggested} ではありませんか？"
        " パスは記憶や推測で再構築せず、直前のツール結果に含まれる文字列や"
        " path_memory の @N をそのままコピーして使ってください。"
    )
