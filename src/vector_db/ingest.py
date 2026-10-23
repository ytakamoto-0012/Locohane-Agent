"""コレクションの取り込み元フォルダを走査して、差分だけをベクターDBへ取り込む。

使い方（CLI）:
    python -m src.vector_db.ingest --instance default --collection manuals [--rebuild]

コレクション自体の作成は設定ダッシュボードの「ナレッジ」タブ（admin/knowledge.py）で行う。
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import hashlib
import logging
import os
import stat
from pathlib import Path
from typing import Callable

import httpx

from ..config import Config
from .catalog import Catalog, CatalogError, FileRecord, open_catalog
from .chunking import split_text
from .embeddings import EmbeddingStopped, embed_images, embed_texts, embedding_model, open_client
from .extract import SUPPORTED_EXTENSIONS, ExtractError, extract_segments
from .keyword_index import KeywordIndex, open_keyword_index
from .render import IMAGE_EXTENSIONS, IMAGE_FILE_EXTENSIONS, PageImage, render_pages
from .store import ChunkRecord, image_collection_name, open_store

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
    # failed のうち [vector_db].max_file_mb を超えて取り込まなかったファイルの数（設定で対処できることを画面に出すため）。
    too_large: int = 0

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "chunks": self.chunks,
            "failed": [{"path": p, "reason": r} for p, r in self.failed],
            "too_large": self.too_large,
        }


def _too_large_hint(cfg: Config, count: int) -> str:
    """上限サイズを超えたファイルへの対処方法（CLI の表示用。画面は admin/static/knowledge.js が同じ内容を出す）。"""
    return (
        f"{count} 件は上限サイズ（config.ini [vector_db] max_file_mb = {cfg.vector_db_max_file_mb}MB、Locohane 側の制限）を"
        "超えたため取り込んでいません。max_file_mb を大きくしてから取り込み（差分）を実行し直すと取り込めます。"
    )


def _is_hidden(path: Path) -> bool:
    """名前が "." で始まる、または Windows の隠し属性が付いているか。"""
    if path.name.startswith("."):
        return True
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
    except OSError:
        return False


def effective_extensions(extensions: tuple[str, ...], image_input: bool) -> tuple[str, ...]:
    """include_extensions のうち、実際に取り込む拡張子。

    画像ファイル（.png・.heic 等）は文字を取り出せないため、image_input = True のときだけ対象にする。
    """
    supported = SUPPORTED_EXTENSIONS + (IMAGE_FILE_EXTENSIONS if image_input else ())
    return tuple(e for e in extensions if e in supported)


def list_source_files(folder: Path, recursive: bool, extensions: tuple[str, ...], image_input: bool = False) -> list[Path]:
    """取り込み対象のファイルをパス順に返す（Office の一時ファイル ~$*.docx 等と、隠しファイル・隠しフォルダ配下は除く）。

    取り込み元フォルダ自身が隠しフォルダでも対象にする（判定は取り込み元より下の名前だけ）。
    拡張子は effective_extensions() で絞る。
    """
    allowed = set(effective_extensions(extensions, image_input))
    files: list[Path] = []
    for root, dirs, names in os.walk(folder):
        root_path = Path(root)
        if recursive:
            dirs[:] = [d for d in dirs if not _is_hidden(root_path / d)]
        else:
            dirs[:] = []
        for name in names:
            path = root_path / name
            if path.suffix.lower() in allowed and not name.startswith("~$") and not _is_hidden(path):
                files.append(path)
    return sorted(files, key=lambda p: str(p).lower())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_mode(cfg: Config, path: Path) -> str:
    """path の埋め込み方。

    [vector_db].image_input = true で画像化に対応した形式なら "text+image"（本文のチャンクに加えて
    ページ画像も埋め込む。画像ファイルは本文が無いため画像だけ）、それ以外は "text"。
    """
    return "text+image" if cfg.vector_db_image_input and path.suffix.lower() in IMAGE_EXTENSIONS else "text"


def _page_chunk_text(cfg: Config, path: Path, page: PageImage) -> str:
    """ページ画像のチャンクの本文（検索結果の抜粋）。ページの文字を chunk_size 文字までにする。"""
    text = page.text.strip()
    if not text:
        if path.suffix.lower() in IMAGE_FILE_EXTENSIONS:
            return "（画像ファイルです。内容は出典の画像で確認してください）"
        return "（文字情報の無いページです。内容は出典のページを画像にして確認してください）"
    return text if len(text) <= cfg.vector_db_chunk_size else text[: cfg.vector_db_chunk_size] + "…"


def _chunk_file(cfg: Config, path: Path) -> list[ChunkRecord]:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return []  # 画像ファイルには本文が無い
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
            （埋め込みモデル・document_prefix を変えたときに必要）。
        progress: ファイルの処理を始めるごとに呼ぶ進捗通知（並列のときは最後に始めたファイル）。
        should_stop: True を返したら残りのファイルを処理せずに終える（埋め込みの再試行を
            待っているファイルもその場でやめ、記録せずに次回の取り込みへ回す）。
        embedding_transport / store_transport: テスト用の httpx トランスポート。

    Raises:
        IngestError: コレクションが無い、取り込み元フォルダが無い、または
            埋め込みモデル・document_prefix が前回と違うのに rebuild=False の場合。
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
        prefix = cfg.vector_db_document_prefix
        if collection.model and collection.document_prefix is not None and collection.document_prefix != prefix and not rebuild:
            raise IngestError(
                f"コレクション {name} は document_prefix {collection.document_prefix!r} で取り込まれています（今の設定は {prefix!r}）。"
                "接頭辞が混ざると検索の精度が落ちるため、再構築してください。"
            )
        store = open_store(cfg, transport=store_transport)
        keywords = open_keyword_index(cfg.vector_db_dir)
        # 埋め込みサーバーへの接続は取り込み全体で使い回す（embeddings.open_client 参照）。
        embedding_client = open_client(cfg, transport=embedding_transport)
        try:
            if rebuild:
                store.drop_collection(name)
                store.drop_collection(image_collection_name(name))
                keywords.drop_collection(name)
                catalog.clear_files(name)
                catalog.set_model(name, None, None)
                collection = catalog.get(name)
            else:
                # qdrant_preset を変えた後は、変わったファイルが無くても既存のコレクションへ反映する。
                store.apply_preset(name)
                store.apply_preset(image_collection_name(name))
            _ingest(
                cfg, catalog, store, keywords, name, collection.dim, model, folder, collection.recursive,
                result, progress, should_stop, embedding_client,
            )
        finally:
            embedding_client.close()
            keywords.close()
            store.close()
        catalog.mark_ingested(name)
    logger.info("vector_db ingest: collection=%s %s", name, result.to_dict())
    return result


@dataclass
class _Prepared:
    """1ファイル分の、DB へ書き込む前までの処理結果（ワーカースレッドで作り、メインスレッドで書き込む）。

    kind:
        "unchanged" - 埋め込み直さない（keyword_chunks があればキーワード索引だけ埋める）。
        "touch"     - 内容（SHA-256）は同じで、サイズ・更新日時の記録だけ更新する。
        "failed"    - 取り込めなかった（reason に理由。too_large なら上限サイズ超え）。
        "embed"     - chunks・pages を埋め込んだ（vectors・image_vectors）。
    """

    path: Path
    record: FileRecord | None
    mode: str
    kind: str
    stat: os.stat_result | None = None
    sha: str = ""
    reason: str = ""
    too_large: bool = False
    keyword_chunks: list[ChunkRecord] | None = None
    chunks: list[ChunkRecord] = field(default_factory=list)
    pages: list[PageImage] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    image_vectors: list[list[float]] = field(default_factory=list)


def _keyword_chunks(cfg: Config, path: Path, needs_keywords: bool) -> list[ChunkRecord] | None:
    """埋め込み直さないファイルがキーワード索引に無ければ（索引の導入前に取り込んだもの）、索引に入れる本文を返す。"""
    if not needs_keywords:
        return None
    try:
        return _chunk_file(cfg, path)
    except (OSError, ExtractError):
        return []


def _prepare(
    cfg: Config,
    path: Path,
    record: FileRecord | None,
    needs_keywords: bool,
    max_bytes: int,
    embedding_client: httpx.Client,
    should_stop: Callable[[], bool] | None = None,
) -> _Prepared:
    """path を読み、必要なら本文の抽出・ページの画像化・埋め込みまで行う（DB には触れない。ワーカースレッドで動く）。

    読めないファイルは kind="failed" で返す。埋め込みサーバーに接続できない等の全体に関わるエラーは例外のまま投げる
    （再試行を待つ間に should_stop() が True になったら EmbeddingStopped）。
    """
    # image_input を切り替えたら、内容の変わっていないファイルも新しい埋め込み方で取り込み直す。
    mode = file_mode(cfg, path)
    try:
        st = path.stat()
        same_mode = record is not None and record.mode == mode
        if same_mode and record.size == st.st_size and record.mtime == st.st_mtime:
            return _Prepared(path, record, mode, "unchanged", keyword_chunks=_keyword_chunks(cfg, path, needs_keywords))
        if st.st_size > max_bytes:
            reason = f"ファイルサイズが上限（{cfg.vector_db_max_file_mb}MB）を超えています"
            return _Prepared(path, record, mode, "failed", reason=reason, too_large=True)
        sha = _sha256(path)
        if same_mode and record.sha256 == sha:
            return _Prepared(path, record, mode, "touch", stat=st, sha=sha, keyword_chunks=_keyword_chunks(cfg, path, needs_keywords))
        chunks = _chunk_file(cfg, path)
        pages = render_pages(path, cfg.vector_db_image_dpi) if mode == "text+image" else []
    except (OSError, ExtractError) as e:
        return _Prepared(path, record, mode, "failed", reason=str(e))
    if not chunks and not pages:
        if mode == "text+image":
            reason = "本文も画像にできるページもありませんでした"
        else:
            reason = "本文を取り出せませんでした（画像だけの PDF 等。[vector_db] image_input = true なら画像として取り込めます）"
        return _Prepared(path, record, mode, "failed", reason=reason)
    vectors = embed_texts(cfg, [c.text for c in chunks], kind="document", client=embedding_client, should_stop=should_stop) if chunks else []
    image_vectors = embed_images(cfg, [p.png for p in pages], client=embedding_client, should_stop=should_stop) if pages else []
    return _Prepared(
        path, record, mode, "embed", stat=st, sha=sha, chunks=chunks, pages=pages, vectors=vectors, image_vectors=image_vectors
    )


class _Writer:
    """_Prepared をカタログ・保存先・キーワード索引へ書き込む（メインスレッドだけで使う）。"""

    def __init__(self, cfg: Config, catalog: Catalog, store, keywords: KeywordIndex, name: str, dim, model: str, result: IngestResult):
        self.cfg, self.catalog, self.store, self.keywords = cfg, catalog, store, keywords
        self.name, self.dim, self.model, self.result = name, dim, model, result
        self.image_collection_ready = False

    def write(self, prepared: _Prepared) -> None:
        catalog, store, keywords, name, result = self.catalog, self.store, self.keywords, self.name, self.result
        path, record, mode = prepared.path, prepared.record, prepared.mode
        source = str(path)
        if prepared.kind == "failed":
            result.failed.append((source, prepared.reason))
            if prepared.too_large:
                result.too_large += 1
            return
        if prepared.kind in ("unchanged", "touch"):
            if prepared.kind == "touch":
                st = prepared.stat
                catalog.upsert_file(name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, record.chunk_count, mode))
            if prepared.keyword_chunks is not None:
                keywords.replace_source(name, source, prepared.keyword_chunks)
            result.unchanged += 1
            return
        chunks, pages, vectors, image_vectors = prepared.chunks, prepared.pages, prepared.vectors, prepared.image_vectors
        if self.dim is None:
            self.dim = len((vectors or image_vectors)[0])
            store.ensure_collection(name, self.dim)
            catalog.set_model(name, self.model, self.dim, self.cfg.vector_db_document_prefix)
        # 本文と画像は別のコレクションに入れる（store.image_collection_name 参照）。
        # 画像の無いファイルも、image_input を切り替える前の画像のベクターが残らないよう消す。
        if chunks:
            store.replace_source(name, source, chunks, vectors)
        else:
            store.delete_source(name, source)
        keywords.replace_source(name, source, chunks)
        if pages:
            if not self.image_collection_ready:
                store.ensure_collection(image_collection_name(name), self.dim)
                self.image_collection_ready = True
            image_chunks = [ChunkRecord(p.locator, _page_chunk_text(self.cfg, path, p)) for p in pages]
            store.replace_source(image_collection_name(name), source, image_chunks, image_vectors)
        else:
            store.delete_source(image_collection_name(name), source)
        chunk_count = len(chunks) + len(pages)
        st = prepared.stat
        catalog.upsert_file(name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, chunk_count, mode))
        result.chunks += chunk_count
        if record is None:
            result.added += 1
        else:
            result.updated += 1


def _ingest(
    cfg, catalog: Catalog, store, keywords: KeywordIndex, name, dim, model, folder, recursive, result, progress, should_stop, embedding_client
) -> None:
    """取り込み元のファイルを [vector_db].ingest_workers 個のスレッドで並列に処理する。

    ファイルの読み取り・本文の抽出・画像化・埋め込みはワーカースレッドで行い、SQLite（カタログ・
    sqlite-vec・キーワード索引）と保存先への書き込みは接続をスレッド間で共有できないため、メインスレッドで
    1件ずつ行う。同時に処理するファイルはワーカー数までに抑え（ページ画像でメモリーを使いすぎないため・
    停止を早く効かせるため）、1つ終わるごとに次のファイルを始める。ワーカー数 1 なら従来どおりパス順に1つずつ処理する。
    """
    files = list_source_files(folder, recursive, cfg.vector_db_include_extensions, cfg.vector_db_image_input)
    known = catalog.files(name)
    max_bytes = int(cfg.vector_db_max_file_mb * 1024 * 1024)
    workers = cfg.vector_db_ingest_workers
    writer = _Writer(cfg, catalog, store, keywords, name, dim, model, result)
    seen: set[str] = set()
    pending = iter(files)
    in_flight: set[Future] = set()
    completed = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vector_db_ingest") as pool:
        try:
            while True:
                while not stopped and len(in_flight) < workers:
                    path = next(pending, None)
                    if path is None:
                        break
                    if should_stop is not None and should_stop():
                        stopped = True
                        break
                    source = str(path)
                    seen.add(source)
                    if progress is not None:
                        progress(completed, len(files), source)
                    needs_keywords = not keywords.has_source(name, source)
                    in_flight.add(
                        pool.submit(_prepare, cfg, path, known.get(source), needs_keywords, max_bytes, embedding_client, should_stop)
                    )
                if not in_flight:
                    break
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        prepared = future.result()
                    except EmbeddingStopped:
                        # 埋め込みの再試行中に停止を求められた。このファイルは記録せず、次回の取り込みで処理する。
                        stopped = True
                        continue
                    writer.write(prepared)
                    completed += 1
        except BaseException:
            # 埋め込みサーバーに接続できない等で止めるときは、まだ始まっていないファイルを始めない。
            for future in in_flight:
                future.cancel()
            raise
    # 並列だと終わった順に記録されるため、失敗一覧はパス順にそろえる。
    result.failed.sort(key=lambda item: item[0].lower())
    if stopped:
        # 未処理のファイルを「消えた」と誤って削除しないよう、ここで抜ける。
        return
    for source in sorted(set(known) - seen):
        store.delete_source(name, source)
        store.delete_source(image_collection_name(name), source)
        keywords.delete_source(name, source)
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
    if summary["too_large"]:
        print(f"  {_too_large_hint(cfg, summary['too_large'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
