"""provide_download ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import json
import logging

from .. import skill_drafts
from . import _state
from ._workdir import _foreign_tmp_dir_error, _resolve_workdir

logger = logging.getLogger(__name__)


@tool
def provide_download(file_paths: list[str]) -> str:
    """既存のファイルをチャット画面にダウンロードボタンとして提示する。

    アップロード済みファイルや、Read/Glob 等で見つけた既存ファイル、以前の
    作業で生成済みのファイルなどを、あらためてユーザーがダウンロードできる
    ようにしたいときに使う。Read 等と同様にパスの制限は行わない
    （ローカルファイルシステム上の任意の絶対パスを指定できる。ただし
    `_tmp_<作成時刻>_<短縮ID>` の他セッション分だけは例外で提供できない）。複数指定
    した場合、1つのメッセージにダウンロードボタンがまとめて並んで表示
    される（1件だけの場合はリストに1件だけ入れて渡す）。

    Args:
        file_paths: ダウンロードさせたいファイルの絶対パスのリスト
            （相対パスの場合はセッションの作業ディレクトリ基準で解決する）。

    Returns:
        成功時: {"output_paths": ["...", ...]} 形式のJSON文字列
            （自動的にチャットへダウンロードボタンがまとめて表示される）。
        1件でも見つからないファイルがあれば「エラー: ...」形式の文字列を
            返す（部分的な成功はしない）。
    """
    resolved: list[Path] = []
    for file_path in file_paths:
        path = Path(file_path)
        if not path.is_absolute():
            path = _resolve_workdir() / path
        path = path.resolve()
        tmp_error = _foreign_tmp_dir_error(path) or skill_drafts.access_error(
            path, "read", _state._LLM_CONFIG, skill_drafts.current_draft_user()
        )
        if tmp_error:
            return tmp_error
        if not path.is_file():
            return f"エラー: ファイルが見つかりません: {file_path}"
        resolved.append(path)
    logger.info("provide_download: %s", resolved)
    return json.dumps({"output_paths": [str(p) for p in resolved]}, ensure_ascii=False)
