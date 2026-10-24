"""コレクションの取り込み元フォルダを走査して、差分だけをベクターDBへ取り込む。

使い方（CLI）:
    python -m src.vector_db.ingest --instance default --collection manuals [--rebuild]

コレクション自体の作成は設定ダッシュボードの「ナレッジ」タブ（admin/knowledge.py）で行う。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
from typing import Callable

import httpx

from ..config import Config
from .catalog import Catalog, CatalogError, FileRecord, open_catalog
from .chunking import split_text
from .embeddings import embed_texts, embedding_model
from .extract import SUPPORTED_EXTENSIONS, ExtractError, extract_segments
from .store import ChunkRecord, open_store

logger = logging.getLogger(__name__)

# 進捗の通知先: (処理済みファイル数, 全ファイル数, 今のファイルパス)
ProgressCallback = Callable[[int, int, str], None]


class IngestError(Exception):
    """取り込みを始められない（フォルダが無い・埋め込みモデルが変わった等）。"""


@dataclass
class IngestResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "chunks": self.chunks,
            "failed": [{"path": p, "reason": r} for p, r in self.failed],
        }


def list_source_files(folder: Path, recursive: bool, extensions: tuple[str, ...]) -> list[Path]:
    """取り込み対象のファイルをパス順に返す（Office の一時ファイル ~$*.docx 等は除く）。"""
    allowed = {e for e in extensions if e in SUPPORTED_EXTENSIONS}
    candidates = folder.rglob("*") if recursive else folder.iterdir()
    files = [p for p in candidates if p.is_file() and p.suffix.lower() in allowed and not p.name.startswith("~$")]
    return sorted(files, key=lambda p: str(p).lower())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_file(cfg: Config, path: Path) -> list[ChunkRecord]:
    chunks = []
    for segment in extract_segments(path):
        for text in split_text(segment.text, cfg.vector_db_chunk_size, cfg.vector_db_chunk_overlap):
            chunks.append(ChunkRecord(locator=segment.locator, text=text))
    return chunks


def ingest_collection(
    cfg: Config,
    name: str,
    *,
    rebuild: bool = False,
    progress: ProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
    embedding_transport: httpx.BaseTransport | None = None,
    store_transport: httpx.BaseTransport | None = None,
) -> IngestResult:
    """コレクション name の取り込み元フォルダを差分で取り込む。

    サイズ・更新日時が前回と同じファイルは読み直さず、変わっていても内容（SHA-256）が
    同じなら記録だけ更新する。フォルダから消えたファイルのチャンクは削除する。
    読めなかったファイルは失敗一覧に載せて続行するが、埋め込みサーバー・保存先へ
    接続できない等の全体に関わるエラーは例外のまま止める。

    Args:
        cfg: 対象インスタンスの設定。
        name: コレクション名。
        rebuild: True なら既存のベクター・記録を消して全ファイルを取り込み直す
            （埋め込みモデルを変えたときに必要）。
        progress: 1ファイル処理するごとに呼ぶ進捗通知。
        should_stop: True を返したら残りのファイルを処理せずに終える。
        embedding_transport / store_transport: テスト用の httpx トランスポート。

    Raises:
        IngestError: コレクションが無い、取り込み元フォルダが無い、または
            埋め込みモデルが前回と違うのに rebuild=False の場合。
        EmbeddingError / StoreError: 埋め込みサーバー・保存先を使えない場合。
    """
    model = embedding_model(cfg)
    result = IngestResult()
    with open_catalog(cfg.vector_db_dir) as catalog:
        try:
            collection = catalog.get(name)
        except CatalogError as e:
            raise IngestError(str(e)) from e
        folder = Path(collection.source_folder)
        if not folder.is_dir():
            raise IngestError(f"取り込み元フォルダがありません: {folder}")
        if collection.model and collection.model != model and not rebuild:
            raise IngestError(
                f"コレクション {name} は埋め込みモデル {collection.model} で作られています（今の設定は {model}）。"
                "モデルが違うと検索できないため、再構築してください。"
            )
        store = open_store(cfg, transport=store_transport)
        try:
            if rebuild:
                store.drop_collection(name)
                catalog.clear_files(name)
                catalog.set_model(name, None, None)
                collection = catalog.get(name)
            _ingest(
                cfg, catalog, store, name, collection.dim, model, folder, collection.recursive,
                result, progress, should_stop, embedding_transport,
            )
        finally:
            store.close()
        catalog.mark_ingested(name)
    logger.info("vector_db ingest: collection=%s %s", name, result.to_dict())
    return result


def _ingest(cfg, catalog: Catalog, store, name, dim, model, folder, recursive, result, progress, should_stop, embedding_transport) -> None:
    files = list_source_files(folder, recursive, cfg.vector_db_include_extensions)
    known = catalog.files(name)
    max_bytes = int(cfg.vector_db_max_file_mb * 1024 * 1024)
    seen: set[str] = set()
    for done, path in enumerate(files, start=1):
        if should_stop is not None and should_stop():
            # 未処理のファイルを「消えた」と誤って削除しないよう、ここで抜ける。
            # 進捗は直前に処理を始めたファイルの手前で止まっているため、処理済みの件数に揃える。
            if progress is not None:
                progress(done - 1, len(files), "")
            return
        source = str(path)
        seen.add(source)
        if progress is not None:
            progress(done - 1, len(files), source)
        try:
            stat = path.stat()
            record = known.get(source)
            if record is not None and record.size == stat.st_size and record.mtime == stat.st_mtime:
                result.unchanged += 1
                continue
            if stat.st_size > max_bytes:
                result.failed.append((source, f"ファイルサイズが上限（{cfg.vector_db_max_file_mb}MB）を超えています"))
                continue
            sha = _sha256(path)
            if record is not None and record.sha256 == sha:
                catalog.upsert_file(name, FileRecord(source, stat.st_size, stat.st_mtime, sha, record.chunk_count))
                result.unchanged += 1
                continue
            chunks = _chunk_file(cfg, path)
        except (OSError, ExtractError) as e:
            result.failed.append((source, str(e)))
            continue
        if not chunks:
            result.failed.append((source, "本文を取り出せませんでした（画像だけの PDF 等）"))
            continue
        vectors = embed_texts(cfg, [c.text for c in chunks], kind="document", transport=embedding_transport)
        if dim is None:
            dim = len(vectors[0])
            store.ensure_collection(name, dim)
            catalog.set_model(name, model, dim)
        store.replace_source(name, source, chunks, vectors)
        catalog.upsert_file(name, FileRecord(source, stat.st_size, stat.st_mtime, sha, len(chunks)))
        result.chunks += len(chunks)
        if record is None:
            result.added += 1
        else:
            result.updated += 1
    for source in sorted(set(known) - seen):
        store.delete_source(name, source)
        catalog.delete_file(name, source)
        result.removed += 1
    if progress is not None:
        progress(len(files), len(files), "")


def main(argv: list[str] | None = None) -> int:
    from ..config import DEFAULT_INSTANCE_NAME, load_config, resolve_instances_root

    parser = argparse.ArgumentParser(description="ベクターDBのコレクションへ文書を取り込む")
    parser.add_argument("--instance", default=os.getenv("LOCOHANE_INSTANCE") or None, help="インスタンス名（省略時は default）")
    parser.add_argument("--collection", required=True, help="コレクション名（設定ダッシュボードの「ナレッジ」タブで作成したもの）")
    parser.add_argument("--rebuild", action="store_true", help="既存のベクターを消して全ファイルを取り込み直す")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    instance = args.instance or DEFAULT_INSTANCE_NAME
    # 管理ツールと同じく instances/<名前>/config_overrides.json の上書きを重ねて読む。
    cfg = load_config(overrides_path=resolve_instances_root() / instance / "config_overrides.json", instance_name=instance)
    if not cfg.vector_db_enabled:
        print("[vector_db] enabled = false です。設定を有効にしてから実行してください。")
        return 1

    def _progress(done: int, total: int, path: str) -> None:
        if path:
            print(f"[{done + 1}/{total}] {path}")

    result = ingest_collection(cfg, args.collection, rebuild=args.rebuild, progress=_progress)
    summary = result.to_dict()
    print(
        f"追加 {summary['added']} / 更新 {summary['updated']} / 変更なし {summary['unchanged']} / "
        f"削除 {summary['removed']} / チャンク {summary['chunks']} / 失敗 {len(summary['failed'])}"
    )
    for item in summary["failed"]:
        print(f"  失敗: {item['path']}: {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
