"""コレクションの取り込み元フォルダを走査して、差分だけをベクターDBへ取り込む。

使い方（CLI）:
    python -m src.vector_db.ingest --instance default --collection manuals [--rebuild]

コレクション自体の作成は設定ダッシュボードの「ナレッジ」タブ（admin/knowledge.py）で行う。
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import hashlib
import heapq
import logging
import os
import stat
from pathlib import Path
import threading
import time
from typing import Callable, Collection

import httpx

from ..config import Config
from .catalog import Catalog, CatalogError, FileRecord, open_catalog
from .chunking import split_text
from .embeddings import EmbeddingStopped, embed_images, embed_texts, embedding_model, open_client
from .extract import SUPPORTED_EXTENSIONS, ExtractError, extract_segments
from .keyword_index import KeywordIndex, open_keyword_index
from .render import IMAGE_EXTENSIONS, IMAGE_FILE_EXTENSIONS, PageImage, close_office_apps, keep_office_apps, render_pages
from .store import ChunkRecord, image_collection_name, open_store, path_collection_name, storage_collection_names

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
    # [vector_db].max_files_per_collection を超えたため取り込まなかったファイルの数と、そのときの上限・見つかったファイル数。
    over_limit: int = 0
    max_files: int = 0
    found_files: int = 0

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "chunks": self.chunks,
            "failed": [{"path": p, "reason": r} for p, r in self.failed],
            "too_large": self.too_large,
            "over_limit": self.over_limit,
            "max_files": self.max_files,
            "found_files": self.found_files,
        }


def _over_limit_hint(result: IngestResult) -> str:
    """ファイル数の上限を超えたときの説明（CLI の表示用。画面は admin/static/knowledge.js が同じ内容を出す）。"""
    return (
        f"取り込み元に {result.found_files} 件のファイルがあり、1コレクションの上限（config.ini [vector_db] "
        f"max_files_per_collection = {result.max_files}）を超えたため、{result.over_limit} 件は取り込んでいません"
        "（取り込み済みのファイルを優先し、残りはパス順の先頭から選んでいます）。"
        "config.ini [vector_db] max_files_per_collection を大きくしてから取り込み（差分）を実行し直すと取り込めます"
        "（取り込み中は対象のファイルの一覧をメモリーに持つため、メモリーに注意してください）。"
        "コレクションを取り込み元のフォルダごとに分けることもできます。"
    )


def _too_large_hint(cfg: Config, count: int) -> str:
    """上限サイズを超えたファイルへの対処方法（CLI の表示用。画面は admin/static/knowledge.js が同じ内容を出す）。"""
    return (
        f"{count} 件は上限サイズ（config.ini [vector_db] max_file_mb = {cfg.vector_db_max_file_mb}MB、Locohane 側の制限）を"
        "超えたため取り込んでいません。max_file_mb を大きくしてから取り込み（差分）を実行し直すと取り込めます。"
    )


def _is_hidden(path: Path, st: os.stat_result | None = None) -> bool:
    """名前が "." で始まる、または Windows の隠し属性が付いているか（st があればそれの属性を見る）。"""
    if path.name.startswith("."):
        return True
    try:
        return bool(getattr(st or path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
    except OSError:
        return False


def effective_extensions(extensions: tuple[str, ...], image_input: bool) -> tuple[str, ...]:
    """include_extensions のうち、実際に取り込む拡張子。

    画像ファイル（.png・.heic 等）は文字を取り出せないため、image_input = True のときだけ対象にする。
    """
    supported = SUPPORTED_EXTENSIONS + (IMAGE_FILE_EXTENSIONS if image_input else ())
    return tuple(e for e in extensions if e in supported)


@dataclass
class ScanResult:
    """scan_source_files() の結果。

    files: 取り込むファイルとその stat（パス順。max_files を超えたら選んだ分だけ）。
    total: 見つかった取り込み対象のファイルの数（max_files を超えた分も数える）。
    found_known: 見つかったファイルのうち、known に含まれていたもののパス（選ばれなかったものも含む。
        取り込み済みのファイルが消えたかの判定に使う）。
    """

    files: list[tuple[Path, os.stat_result]]
    total: int
    found_known: set[str]

    @property
    def over_limit(self) -> int:
        """max_files を超えたため取り込まないファイルの数。"""
        return self.total - len(self.files)


class _Descending:
    """heapq（小さい順）で「パス順で後ろのもの」を先頭にするためのキー（パス順で先頭の N 件を残すのに使う）。"""

    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key

    def __lt__(self, other: "_Descending") -> bool:
        return self.key > other.key


def _read_directory(directory: Path, recursive: bool, allowed: set[str]) -> tuple[list[tuple[Path, os.stat_result]], list[Path]]:
    """directory 直下の取り込み対象のファイルと、たどるサブフォルダを返す（読めなければ空）。"""
    try:
        entries = list(os.scandir(directory))
    except OSError as e:
        logger.warning("vector_db ingest: フォルダを読めません: %s: %s", directory, e)
        return [], []
    files: list[tuple[Path, os.stat_result]] = []
    subdirectories: list[Path] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            is_dir = entry.is_dir()
            st = entry.stat()
        except OSError:
            continue
        if is_dir:
            if recursive and not _is_hidden(path, st):
                subdirectories.append(path)
        elif path.suffix.lower() in allowed and not entry.name.startswith("~$") and not _is_hidden(path, st):
            files.append((path, st))
    return files, subdirectories


def scan_source_files(
    folder: Path,
    recursive: bool,
    extensions: tuple[str, ...],
    image_input: bool = False,
    *,
    workers: int = 1,
    max_files: int | None = None,
    known: Collection[str] = (),
) -> ScanResult:
    """取り込み対象のファイルとその stat を探す（Office の一時ファイル ~$*.docx 等と、隠しファイル・隠しフォルダ配下は除く）。

    取り込み元フォルダ自身が隠しフォルダでも対象にする（判定は取り込み元より下の名前だけ）。
    拡張子は effective_extensions() で絞る。stat は os.scandir() のもの（Windows ではフォルダを読むときに
    サイズ・更新日時・属性も得られるため、ファイルごとに問い合わせずに済む）。

    workers が 2 以上なら、その数のスレッドで複数のフォルダを同時に読む（ファイルサーバー上のフォルダでは
    フォルダを読む往復の待ち時間が大半を占めるため、並列にすると速い）。

    max_files を超えるファイルがあれば、取り込みのたびに選ぶファイルが入れ替わらないよう、known（取り込み済み）の
    ファイルを優先し、残りの枠を新しいファイルのパス順の先頭から埋める。メモリーに持つのは高々 max_files の
    2倍程度のファイルまで。
    """
    allowed = set(effective_extensions(extensions, image_input))
    known_files: list[tuple[str, Path, os.stat_result]] = []
    found_known: set[str] = set()
    # known に無いファイル。max_files があればパス順で先頭の max_files 件だけを残す（ヒープの先頭がパス順で最後のもの）。
    new_files: list[tuple[_Descending, Path, os.stat_result]] = []
    total = 0

    def collect(found: list[tuple[Path, os.stat_result]]) -> None:
        nonlocal total
        for path, st in found:
            total += 1
            source = str(path)
            if source in known:
                found_known.add(source)
                known_files.append((source.lower(), path, st))
                continue
            item = (_Descending(source.lower()), path, st)
            if max_files is None or len(new_files) < max_files:
                heapq.heappush(new_files, item)
            elif new_files[0][0] < item[0]:
                # パス順で今残しているものの最後より前なら入れ替える。
                heapq.heapreplace(new_files, item)

    if workers <= 1:
        directories = [folder]
        while directories:
            found, subdirectories = _read_directory(directories.pop(), recursive, allowed)
            collect(found)
            directories.extend(subdirectories)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vector_db_scan") as pool:
            running = {pool.submit(_read_directory, folder, recursive, allowed)}
            try:
                while running:
                    done, running = wait(running, return_when=FIRST_COMPLETED)
                    for future in done:
                        found, subdirectories = future.result()
                        collect(found)
                        running |= {pool.submit(_read_directory, d, recursive, allowed) for d in subdirectories}
            except BaseException:
                for future in running:
                    future.cancel()
                raise

    known_files.sort(key=lambda item: item[0])
    selected = [(path, st) for _key, path, st in known_files]
    new_sorted = sorted(((item[0].key, item[1], item[2]) for item in new_files), key=lambda item: item[0])
    if max_files is not None:
        selected = selected[:max_files]
        new_sorted = new_sorted[: max_files - len(selected)]
    selected += [(path, st) for _key, path, st in new_sorted]
    selected.sort(key=lambda item: str(item[0]).lower())
    return ScanResult(selected, total, found_known)


def list_source_files(folder: Path, recursive: bool, extensions: tuple[str, ...], image_input: bool = False) -> list[Path]:
    """scan_source_files() のファイルのパスだけを返す。"""
    return [path for path, _st in scan_source_files(folder, recursive, extensions, image_input).files]


def file_mode(cfg: Config, path: Path) -> str:
    """path の埋め込み方。

    [vector_db].image_input = true で画像化に対応した形式なら "text+image"（本文のチャンクに加えて
    ページ画像も埋め込む。画像ファイルは本文が無いため画像だけ）、それ以外は "text"。
    """
    return _file_mode(_LoadOptions.from_config(cfg), path)


def _page_chunk_text(cfg: Config, path: Path, page: PageImage) -> str:
    """ページ画像のチャンクの本文（検索結果の抜粋）。ページの文字を chunk_size 文字までにする。"""
    text = page.text.strip()
    if not text:
        if path.suffix.lower() in IMAGE_FILE_EXTENSIONS:
            return "（画像ファイルです。内容は出典の画像で確認してください）"
        return "（文字情報の無いページです。内容は出典のページを画像にして確認してください）"
    return text if len(text) <= cfg.vector_db_chunk_size else text[: cfg.vector_db_chunk_size] + "…"


def relative_path_text(folder: Path, path: Path) -> str:
    """ファイルパスのベクターに埋め込む文字列（取り込み元フォルダからの相対パス。区切りは "/"）。

    取り込み元フォルダより上の部分は全ファイルで同じで検索の手がかりにならないため含めない。
    """
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return path.as_posix()


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
                for storage in storage_collection_names(name):
                    store.drop_collection(storage)
                keywords.drop_collection(name)
                keywords.drop_collection(path_collection_name(name))
                catalog.clear_files(name)
                catalog.set_model(name, None, None)
                collection = catalog.get(name)
            else:
                # qdrant_preset を変えた後は、変わったファイルが無くても既存のコレクションへ反映する。
                for storage in storage_collection_names(name):
                    store.apply_preset(storage)
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
    """1ファイル分の、DB へ書き込む前までの処理結果（_load() で作り、埋め込みを足してメインスレッドで書き込む）。

    kind:
        "unchanged" - 埋め込み直さない（keyword_chunks があればキーワード索引だけ埋める）。
        "touch"     - 内容（SHA-256）は同じで、サイズ・更新日時の記録だけ更新する。
        "failed"    - 取り込めなかった（reason に理由。too_large なら上限サイズ超え）。
        "embed"     - chunks・pages を埋め込む（vectors・image_vectors）。

    path_vector はファイルパス（path_text）のベクター。"embed" では常に、"unchanged"・"touch" では
    パスのベクターがまだ無いファイル（FileRecord.path_indexed = False）だけ埋め込む。
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
    path_text: str = ""
    path_vector: list[float] | None = None

    def texts_to_embed(self) -> list[str]:
        """埋め込むテキスト（本文のチャンク、続けてファイルパス）。"""
        if self.kind == "embed":
            return [c.text for c in self.chunks] + [self.path_text]
        if self.kind in ("unchanged", "touch") and not (self.record is not None and self.record.path_indexed):
            # パスのベクターの導入前に取り込んだファイルは、内容を埋め込み直さずにパスだけを埋め込む。
            return [self.path_text]
        return []

    def set_vectors(self, text_vectors: list[list[float]], image_vectors: list[list[float]]) -> None:
        """texts_to_embed()・pages の順の埋め込みを受け取る。"""
        if self.kind == "embed":
            self.vectors, self.path_vector = text_vectors[:-1], text_vectors[-1]
            self.image_vectors = image_vectors
        elif text_vectors:
            self.path_vector = text_vectors[0]


@dataclass(frozen=True)
class _LoadOptions:
    """_load() に渡す設定（子プロセス（[vector_db].ingest_processes）へ送るため、Config ではなく必要な値だけを持つ）。"""

    chunk_size: int
    chunk_overlap: int
    image_input: bool
    image_dpi: int
    max_file_mb: float

    @classmethod
    def from_config(cls, cfg: Config) -> "_LoadOptions":
        return cls(cfg.vector_db_chunk_size, cfg.vector_db_chunk_overlap, cfg.vector_db_image_input, cfg.vector_db_image_dpi, cfg.vector_db_max_file_mb)

    @property
    def max_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)


def _file_mode(options: _LoadOptions, path: Path) -> str:
    return "text+image" if options.image_input and path.suffix.lower() in IMAGE_EXTENSIONS else "text"


def _chunk_segments(options: _LoadOptions, path: Path, data: bytes | None = None) -> list[ChunkRecord]:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return []  # 画像ファイルには本文が無い
    chunks = []
    for segment in extract_segments(path, data):
        for text in split_text(segment.text, options.chunk_size, options.chunk_overlap):
            chunks.append(ChunkRecord(locator=segment.locator, text=text))
    return chunks


def _keyword_chunks(options: _LoadOptions, path: Path, needs_keywords: bool) -> list[ChunkRecord] | None:
    """埋め込み直さないファイルがキーワード索引に無ければ（索引の導入前に取り込んだもの）、索引に入れる本文を返す。"""
    if not needs_keywords:
        return None
    try:
        return _chunk_segments(options, path)
    except (OSError, ExtractError):
        return []


def _is_unchanged(record: FileRecord | None, mode: str, st: os.stat_result) -> bool:
    return record is not None and record.mode == mode and record.size == st.st_size and record.mtime == st.st_mtime


def _load(
    options: _LoadOptions, folder: Path, path: Path, st: os.stat_result, record: FileRecord | None, needs_keywords: bool
) -> _Prepared:
    """path を読み、必要なら本文の抽出・ページの画像化まで行う（埋め込みと DB への書き込みはしない）。

    ワーカースレッドか子プロセス（[vector_db].ingest_processes）で動く。読めないファイルは kind="failed" で返す。
    """
    # image_input を切り替えたら、内容の変わっていないファイルも新しい埋め込み方で取り込み直す。
    mode = _file_mode(options, path)
    path_text = relative_path_text(folder, path)
    if _is_unchanged(record, mode, st):
        return _Prepared(path, record, mode, "unchanged", keyword_chunks=_keyword_chunks(options, path, needs_keywords), path_text=path_text)
    if st.st_size > options.max_bytes:
        reason = f"ファイルサイズが上限（{options.max_file_mb}MB）を超えています"
        return _Prepared(path, record, mode, "failed", reason=reason, too_large=True)
    try:
        # SHA-256 の計算と本文の抽出で同じファイルを2回読まないよう、中身を1回だけ読む（上限サイズ以下のため）。
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if record is not None and record.mode == mode and record.sha256 == sha:
            return _Prepared(
                path, record, mode, "touch", stat=st, sha=sha, keyword_chunks=_keyword_chunks(options, path, needs_keywords),
                path_text=path_text,
            )
        chunks = _chunk_segments(options, path, data)
        del data
        pages = render_pages(path, options.image_dpi) if mode == "text+image" else []
    except (OSError, ExtractError) as e:
        return _Prepared(path, record, mode, "failed", reason=str(e))
    if not chunks and not pages:
        if mode == "text+image":
            reason = "本文も画像にできるページもありませんでした"
        else:
            reason = "本文を取り出せませんでした（画像だけの PDF 等。[vector_db] image_input = true なら画像として取り込めます）"
        return _Prepared(path, record, mode, "failed", reason=reason)
    return _Prepared(path, record, mode, "embed", stat=st, sha=sha, chunks=chunks, pages=pages, path_text=path_text)


def _load_in_thread(*args) -> _Prepared:
    # 取り込みの間、このスレッドで起動した Office は終了せずに使い回す（_close_office_in_threads で終了する）。
    keep_office_apps()
    return _load(*args)


def _init_load_process() -> None:
    """子プロセス（[vector_db].ingest_processes）の初期化。Office を使い回し、プロセスの終了時に終了する。"""
    import multiprocessing.util

    keep_office_apps()
    multiprocessing.util.Finalize(None, close_office_apps, exitpriority=10)


def _close_office_in_threads(pool: ThreadPoolExecutor, workers: int) -> None:
    """pool の各スレッドで使い回していた Office を終了する（COM のオブジェクトは作ったスレッドでしか扱えない）。

    全スレッドがそろうまで待つ（Barrier）ことで、各スレッドに1つずつ終了の処理を割り当てる。
    """
    barrier = threading.Barrier(workers)

    def close() -> None:
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        close_office_apps()

    wait([pool.submit(close) for _ in range(workers)])


class _Writer:
    """_Prepared をカタログ・保存先・キーワード索引へ書き込む（メインスレッドだけで使う）。

    書き込みは begin() から finish() まで、_COMMIT_INTERVAL_SECONDS 秒・_COMMIT_EVERY_FILES ファイルごとに
    まとめてコミットする。保存先 → キーワード索引 → カタログの順にコミットするため、途中で止まっても
    カタログに記録されたファイルのベクターは必ず保存されている（記録されていないファイルは次回取り込み直す）。
    """

    _COMMIT_INTERVAL_SECONDS = 2.0
    _COMMIT_EVERY_FILES = 500

    def __init__(self, cfg: Config, catalog: Catalog, store, keywords: KeywordIndex, name: str, dim, model: str, result: IngestResult):
        self.cfg, self.catalog, self.store, self.keywords = cfg, catalog, store, keywords
        self.name, self.dim, self.model, self.result = name, dim, model, result
        self.image_collection_ready = False
        self.path_collection_ready = False
        # キーワード索引に入っているファイル（has_source() をファイルごとに呼ばないよう、最初にまとめて読む）。
        self.keyword_sources = keywords.sources(name)
        self.path_keyword_sources = keywords.sources(path_collection_name(name))
        self._uncommitted = 0
        self._last_commit = time.monotonic()

    def _parts(self):
        # コミットの順（保存先 → キーワード索引 → カタログ）。
        return (self.store, self.keywords, self.catalog)

    def begin(self) -> None:
        for part in self._parts():
            part.begin_batch()

    def finish(self) -> None:
        for part in self._parts():
            part.end_batch()

    def abort(self) -> None:
        """コミットしていない書き込みを捨てる（エラーで止めるとき）。"""
        for part in self._parts():
            try:
                part.rollback()
            except Exception:  # noqa: BLE001 - 元のエラーを優先する
                logger.exception("vector_db ingest: ロールバックに失敗しました")
            part._batching = False

    def _maybe_commit(self) -> None:
        self._uncommitted += 1
        if self._uncommitted >= self._COMMIT_EVERY_FILES or time.monotonic() - self._last_commit >= self._COMMIT_INTERVAL_SECONDS:
            for part in self._parts():
                part.commit()
            self._uncommitted = 0
            self._last_commit = time.monotonic()

    def _write_path(self, source: str, prepared: _Prepared) -> None:
        """ファイルパスのベクターを1ファイル1件で入れ替える（store.path_collection_name 参照）。"""
        if not self.path_collection_ready:
            self.store.ensure_collection(path_collection_name(self.name), self.dim or len(prepared.path_vector))
            self.path_collection_ready = True
        self.store.replace_source(
            path_collection_name(self.name), source, [ChunkRecord("", prepared.path_text)], [prepared.path_vector]
        )

    def _write_path_keywords(self, source: str, prepared: _Prepared, *, only_missing: bool) -> None:
        """ファイルパスをキーワード索引にも入れる（索引の中のコレクション名は store.path_collection_name と同じ）。

        拡張子は索引の語に含めない（"pdf"・"jpg" 等が同じ形式の全ファイルに一致してしまうため）。
        検索結果に出す相対パスは拡張子付きのまま。
        only_missing なら索引に無いときだけ入れる（パスのキーワード検索の導入前に取り込んだファイル用）。
        """
        if only_missing and source in self.path_keyword_sources:
            return
        index_text = prepared.path_text.removesuffix(prepared.path.suffix)
        self.keywords.replace_source(
            path_collection_name(self.name), source, [ChunkRecord("", prepared.path_text)], index_texts=[index_text]
        )
        self.path_keyword_sources.add(source)

    def write(self, prepared: _Prepared) -> None:
        self._write(prepared)
        self._maybe_commit()

    def _write(self, prepared: _Prepared) -> None:
        catalog, store, keywords, name, result = self.catalog, self.store, self.keywords, self.name, self.result
        path, record, mode = prepared.path, prepared.record, prepared.mode
        source = str(path)
        if prepared.kind == "failed":
            result.failed.append((source, prepared.reason))
            if prepared.too_large:
                result.too_large += 1
            return
        if prepared.kind in ("unchanged", "touch"):
            # 保存先 → キーワード索引 → カタログの順に書く（同時に動く別の取り込みとロックを取る順をそろえ、待ち合いで止まらないため）。
            if prepared.path_vector is not None:
                self._write_path(source, prepared)
            if prepared.keyword_chunks is not None:
                keywords.replace_source(name, source, prepared.keyword_chunks)
                self.keyword_sources.add(source)
            self._write_path_keywords(source, prepared, only_missing=True)
            path_indexed = record.path_indexed or prepared.path_vector is not None
            if prepared.kind == "touch":
                st = prepared.stat
                catalog.upsert_file(
                    name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, record.chunk_count, mode, path_indexed)
                )
            elif prepared.path_vector is not None:
                catalog.mark_path_indexed(name, source)
            result.unchanged += 1
            return
        chunks, pages, vectors, image_vectors = prepared.chunks, prepared.pages, prepared.vectors, prepared.image_vectors
        if self.dim is None:
            self.dim = len((vectors or image_vectors or [prepared.path_vector])[0])
            store.ensure_collection(name, self.dim)
            catalog.set_model(name, self.model, self.dim, self.cfg.vector_db_document_prefix)
        # 本文と画像は別のコレクションに入れる（store.image_collection_name 参照）。
        # 画像の無いファイルも、image_input を切り替える前の画像のベクターが残らないよう消す。
        if chunks:
            store.replace_source(name, source, chunks, vectors)
        else:
            store.delete_source(name, source)
        if pages:
            if not self.image_collection_ready:
                store.ensure_collection(image_collection_name(name), self.dim)
                self.image_collection_ready = True
            image_chunks = [ChunkRecord(p.locator, _page_chunk_text(self.cfg, path, p)) for p in pages]
            store.replace_source(image_collection_name(name), source, image_chunks, image_vectors)
        else:
            store.delete_source(image_collection_name(name), source)
        self._write_path(source, prepared)
        keywords.replace_source(name, source, chunks)
        self.keyword_sources.add(source)
        self._write_path_keywords(source, prepared, only_missing=False)
        chunk_count = len(chunks) + len(pages)
        st = prepared.stat
        catalog.upsert_file(name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, chunk_count, mode, True))
        result.chunks += chunk_count
        if record is None:
            result.added += 1
        else:
            result.updated += 1

    def remove(self, source: str) -> None:
        """フォルダから消えたファイルのチャンク・記録を消す。"""
        for storage in storage_collection_names(self.name):
            self.store.delete_source(storage, source)
        self.keywords.delete_source(self.name, source)
        self.keywords.delete_source(path_collection_name(self.name), source)
        self.catalog.delete_file(self.name, source)
        self.result.removed += 1
        self._maybe_commit()


class _Job:
    """埋め込みを待っている1ファイル。texts_to_embed()・pages の各埋め込みが返るたびに埋め、全部そろったら書き込む。"""

    def __init__(self, prepared: _Prepared):
        self.prepared = prepared
        self.texts = prepared.texts_to_embed()
        self.text_vectors: list[list[float] | None] = [None] * len(self.texts)
        self.image_vectors: list[list[float] | None] = [None] * len(prepared.pages)
        self.remaining = len(self.texts) + len(prepared.pages)

    def finish(self) -> _Prepared:
        self.prepared.set_vectors(self.text_vectors, self.image_vectors)
        return self.prepared


class _Pipeline:
    """取り込み元のファイルを、準備（読み取り・抽出・画像化）→ 埋め込み → 書き込みの流れで処理する。

    - 準備: [vector_db].ingest_workers 個のスレッド（ingest_processes が 1 以上なら、その数の子プロセス）で並列に行う。
    - 埋め込み: 複数のファイルのテキスト（本文のチャンクとファイルパス）を embedding_batch_size 件ずつにまとめ、
      ingest_workers 個まで同時に送る。大きいファイルのチャンクも複数のバッチに分けて同時に送る。画像は1枚ずつ送る。
      埋め込みのリクエストが1つも動いていないとき、または準備中のファイルが無いときは、件数が足りなくても送る
      （埋め込みサーバーを遊ばせないため。埋め込みが追いつかないときほど、まとまったバッチになる）。
    - 書き込み: SQLite（カタログ・sqlite-vec・キーワード索引）の接続はスレッド間で共有できないため、メインスレッドで
      1ファイルずつ書き、まとめてコミットする（_Writer）。

    メモリーを使いすぎないよう、準備済みで埋め込みを待っているファイルは _max_waiting 個までにする。
    """

    def __init__(self, cfg: Config, folder: Path, known: dict[str, FileRecord], writer: _Writer, embedding_client, should_stop, progress):
        self.cfg, self.folder, self.known, self.writer = cfg, folder, known, writer
        self.embedding_client, self.should_stop, self.progress = embedding_client, should_stop, progress
        self.options = _LoadOptions.from_config(cfg)
        self.workers = cfg.vector_db_ingest_workers
        self.processes = cfg.vector_db_ingest_processes
        self.batch_size = cfg.vector_db_embedding_batch_size
        self.load_limit = self.processes or self.workers
        self._max_waiting = max(self.batch_size, 2 * max(self.workers, self.load_limit))
        self.loads: dict[Future, None] = {}
        self.embeds: dict[Future, tuple[str, list[tuple[_Job, int]]]] = {}
        self.text_queue: deque[tuple[_Job, int]] = deque()
        self.image_queue: deque[tuple[_Job, int]] = deque()
        self.waiting = 0
        self.completed = 0
        self.stopped = False

    def run(self, files: list[tuple[Path, os.stat_result]]) -> None:
        """files を処理する（停止したら self.stopped が True）。"""
        if self.workers > 1 or self.processes > 1:
            # 大きいファイルから始め、最後に大きいファイルが1つだけ残って待つのを避ける。
            files = sorted(files, key=lambda item: item[1].st_size, reverse=True)
        self.total = len(files)
        self.pending = iter(files)
        self.exhausted = False
        if self.processes:
            load_pool = ProcessPoolExecutor(max_workers=self.processes, initializer=_init_load_process)
        else:
            load_pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="vector_db_load")
        embed_pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="vector_db_embed")
        try:
            while True:
                self._start_loads(load_pool)
                self._start_embeds(embed_pool)
                if not self.loads and not self.embeds:
                    if self.stopped or self.exhausted:
                        break
                    continue
                done, _ = wait(list(self.loads) + list(self.embeds), return_when=FIRST_COMPLETED)
                for future in done:
                    if future in self.loads:
                        del self.loads[future]
                        self._loaded(future.result())
                    else:
                        self._embedded(future, *self.embeds.pop(future))
        except BaseException:
            # 埋め込みサーバーに接続できない等で止めるときは、まだ始まっていない処理を始めない。
            for future in list(self.loads) + list(self.embeds):
                future.cancel()
            raise
        finally:
            embed_pool.shutdown(wait=True, cancel_futures=True)
            if not self.processes and self.cfg.vector_db_image_input:
                _close_office_in_threads(load_pool, self.workers)
            load_pool.shutdown(wait=True, cancel_futures=True)

    def _start_loads(self, pool) -> None:
        while not self.stopped and not self.exhausted and len(self.loads) < self.load_limit and self.waiting < self._max_waiting:
            item = next(self.pending, None)
            if item is None:
                self.exhausted = True
                return
            if self.should_stop is not None and self.should_stop():
                self.stopped = True
                return
            path, st = item
            source = str(path)
            if self.progress is not None:
                self.progress(self.completed, self.total, source)
            record = self.known.get(source)
            needs_keywords = source not in self.writer.keyword_sources
            if not needs_keywords and _is_unchanged(record, _file_mode(self.options, path), st):
                # 変わっていないファイルは読まないため、スレッド・子プロセスに渡さずにここで済ませる。
                mode = _file_mode(self.options, path)
                self._loaded(_Prepared(path, record, mode, "unchanged", path_text=relative_path_text(self.folder, path)))
                continue
            args = (self.options, self.folder, path, st, record, needs_keywords)
            future = pool.submit(_load, *args) if self.processes else pool.submit(_load_in_thread, *args)
            self.loads[future] = None

    def _loaded(self, prepared: _Prepared) -> None:
        job = _Job(prepared)
        if not job.remaining:
            self._write(job)
            return
        self.waiting += 1
        self.text_queue.extend((job, i) for i in range(len(job.texts)))
        self.image_queue.extend((job, i) for i in range(len(prepared.pages)))

    def _start_embeds(self, pool) -> None:
        while not self.stopped and len(self.embeds) < self.workers:
            if len(self.text_queue) >= self.batch_size:
                self._submit_texts(pool, self.batch_size)
            elif self.image_queue:
                job, index = item = self.image_queue.popleft()
                future = pool.submit(
                    embed_images, self.cfg, [job.prepared.pages[index].png], client=self.embedding_client, should_stop=self.should_stop
                )
                self.embeds[future] = ("image", [item])
            elif self.text_queue and (not self.embeds or not self.loads):
                self._submit_texts(pool, len(self.text_queue))
            else:
                return

    def _submit_texts(self, pool, count: int) -> None:
        items = [self.text_queue.popleft() for _ in range(count)]
        future = pool.submit(
            embed_texts,
            self.cfg,
            [job.texts[i] for job, i in items],
            kind="document",
            client=self.embedding_client,
            should_stop=self.should_stop,
        )
        self.embeds[future] = ("text", items)

    def _embedded(self, future: Future, kind: str, items: list[tuple[_Job, int]]) -> None:
        try:
            vectors = future.result()
        except EmbeddingStopped:
            # 埋め込みの再試行中に停止を求められた。このファイルは記録せず、次回の取り込みで処理する。
            self.stopped = True
            return
        for (job, index), vector in zip(items, vectors, strict=True):
            (job.text_vectors if kind == "text" else job.image_vectors)[index] = vector
            job.remaining -= 1
            if not job.remaining:
                self.waiting -= 1
                self._write(job)

    def _write(self, job: _Job) -> None:
        self.writer.write(job.finish())
        self.completed += 1


def _ingest(
    cfg, catalog: Catalog, store, keywords: KeywordIndex, name, dim, model, folder, recursive, result, progress, should_stop, embedding_client
) -> None:
    """取り込み元のファイルを _Pipeline で処理し、フォルダから消えたファイルを消す。

    停止したときは、埋め込みまで終わったファイルだけを記録する（途中のファイルは次回の取り込みで処理する）。
    """
    known = catalog.files(name)
    max_files = cfg.vector_db_max_files_per_collection
    scan = scan_source_files(
        folder, recursive, cfg.vector_db_include_extensions, cfg.vector_db_image_input,
        workers=cfg.vector_db_scan_workers, max_files=max_files, known=known,
    )
    files = scan.files
    result.found_files, result.max_files, result.over_limit = scan.total, max_files, scan.over_limit
    if scan.over_limit:
        logger.warning("vector_db ingest: collection=%s %s", name, _over_limit_hint(result))
    writer = _Writer(cfg, catalog, store, keywords, name, dim, model, result)
    writer.begin()
    try:
        pipeline = _Pipeline(cfg, folder, known, writer, embedding_client, should_stop, progress)
        pipeline.run(files)
        if not pipeline.stopped:
            # 停止したときは、未処理のファイルを「消えた」と誤って削除しないよう消さない。
            # 上限を超えて選ばれなかったファイルも、フォルダにある限り消さない（found_known で判定する）。
            for source in sorted(set(known) - scan.found_known):
                writer.remove(source)
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    # 並列だと終わった順に記録されるため、失敗一覧はパス順にそろえる。
    result.failed.sort(key=lambda item: item[0].lower())
    if progress is not None and not pipeline.stopped:
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
    if result.over_limit:
        print(f"  {_over_limit_hint(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
