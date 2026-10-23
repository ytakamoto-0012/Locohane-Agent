"""画像として取り込んだページの PNG の保存先（[vector_db].dir/page_images/<コレクション名>/）。

検索結果にページ画像のパスを載せ、LLM が analyze_image で中身を確かめられるようにするため、
埋め込みに使った画像を取り込み元ファイルごとに残しておく。ファイル名は取り込み元パスの
ハッシュ＋ページ連番にして、取り込み直し・ファイル削除のときに丸ごと消せるようにする。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

PAGE_IMAGES_DIRNAME = "page_images"


def _collection_dir(vector_db_dir: Path, collection: str) -> Path:
    return vector_db_dir / PAGE_IMAGES_DIRNAME / collection


def _source_key(source_path: str) -> str:
    return hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:16]


def save_page_images(vector_db_dir: Path, collection: str, source_path: str, pngs: list[bytes]) -> list[str]:
    """source_path のページ画像を保存し直し、保存先の絶対パスをページ順に返す。"""
    delete_page_images(vector_db_dir, collection, source_path)
    directory = _collection_dir(vector_db_dir, collection)
    directory.mkdir(parents=True, exist_ok=True)
    key = _source_key(source_path)
    paths = []
    for i, png in enumerate(pngs, start=1):
        path = directory / f"{key}_{i}.png"
        path.write_bytes(png)
        paths.append(str(path.resolve()))
    return paths


def delete_page_images(vector_db_dir: Path, collection: str, source_path: str) -> None:
    """source_path のページ画像を消す（無ければ何もしない）。"""
    directory = _collection_dir(vector_db_dir, collection)
    if not directory.is_dir():
        return
    for path in directory.glob(f"{_source_key(source_path)}_*.png"):
        path.unlink(missing_ok=True)


def drop_collection_images(vector_db_dir: Path, collection: str) -> None:
    """コレクションのページ画像をすべて消す（再構築・コレクション削除時）。"""
    shutil.rmtree(_collection_dir(vector_db_dir, collection), ignore_errors=True)
