"""コレクションの取り込み元フォルダを走査して、差分だけをベクターDBへ取り込む。

使い方（CLI）:
    python -m src.vector_db.ingest --instance default --collection manuals [--rebuild]

コレクション自体の作成は設定ダッシュボードの「ナレッジ」タブ（admin/knowledge.py）で行う。
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
import hashlib
import heapq
import logging
import os
import shutil
import stat
from pathlib import Path
import tempfile
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
    # added・updated のうち、中身を埋め込まずにファイルパスだけを登録したファイルの数
    # （image_input = false の画像ファイルと、本文を取り出せなかったファイル）。
    path_only: int = 0

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "chunks": self.chunks,
            "path_only": self.path_only,
            "failed": [{"path": p, "reason": r} for p, r in self.failed],
            "too_large": self.too_large,
            "over_limit": self.over_limit,
            "max_files": self.max_files,
            "found_files": self.found_files,
        }


def _over_limit_hint(result: IngestResult) -> str:
    """ファイル数の上限を超えたときの説明（CLI の表示用。画面は admin/static/knowledge.js が同じ内容を出す）。"""
    return (
        f"The source has {result.found_files} files, which exceeds the per-collection limit (config.ini [vector_db] "
        f"max_files_per_collection = {result.max_files}), so {result.over_limit} files were not ingested "
        "(already ingested files take priority, and the rest are chosen from the start in path order). "
        "Increase config.ini [vector_db] max_files_per_collection and run the ingestion (diff) again to ingest them "
        "(while ingesting, the list of target files is kept in memory, so watch the memory). "
        "You can also split collections by source folder."
    )


def _too_large_hint(cfg: Config, count: int) -> str:
    """上限サイズを超えたファイルへの対処方法（CLI の表示用。画面は admin/static/knowledge.js が同じ内容を出す）。"""
    return (
        f"{count} files exceeded the size limit (config.ini [vector_db] max_file_mb = {cfg.vector_db_max_file_mb}MB, a Locohane limit) "
        "and were not ingested. Increase max_file_mb and run the ingestion (diff) again to ingest them."
    )


def _is_hidden(path: Path, st: os.stat_result | None = None) -> bool:
    """名前が "." で始まる、または Windows の隠し属性が付いているか（st があればそれの属性を見る）。"""
    if path.name.startswith("."):
        return True
    try:
        return bool(getattr(st or path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
    except OSError:
        return False


def effective_extensions(extensions: tuple[str, ...]) -> tuple[str, ...]:
    """include_extensions のうち、実際に取り込む拡張子。

    画像ファイル（.png・.heic 等）は image_input = True なら画像として埋め込み、False でもファイル名・
    フォルダ名で探せるようパスだけを登録する（file_mode() の "path"）。
    """
    supported = SUPPORTED_EXTENSIONS + IMAGE_FILE_EXTENSIONS
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


def _is_link(entry: os.DirEntry) -> bool:
    """entry がシンボリックリンクかジャンクション（Windows のマウントポイント）か。"""
    if entry.is_symlink():
        return True
    try:
        tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
    except OSError:
        return False
    return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)


def _link_target(path: Path) -> str | None:
    """リンクのフォルダの実体のパス（比べるために正規化したもの。たどれなければ None）。"""
    try:
        return os.path.normcase(os.path.realpath(path, strict=True))
    except OSError:
        return None


def _is_same_or_inside(path: str, folder: str) -> bool:
    """path が folder 自身かその中か（どちらも _link_target で正規化し、末尾の区切りを除いたもの）。"""
    return path == folder or path.startswith(folder + os.sep)


@dataclass
class _Subdirectory:
    path: Path
    # シンボリックリンク・ジャンクションなら実体のパス（_link_target）。リンクでなければ None。
    link_target: str | None = None


def _read_directory(directory: Path, recursive: bool, allowed: set[str]) -> tuple[list[tuple[Path, os.stat_result]], list[_Subdirectory]]:
    """directory 直下の取り込み対象のファイルと、たどるサブフォルダを返す（読めなければ空）。"""
    try:
        entries = list(os.scandir(directory))
    except OSError as e:
        logger.warning("vector_db ingest: cannot read folder: %s: %s", directory, e)
        return [], []
    files: list[tuple[Path, os.stat_result]] = []
    subdirectories: list[_Subdirectory] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            is_dir = entry.is_dir()
            st = entry.stat()
        except OSError:
            continue
        if is_dir:
            if recursive and not _is_hidden(path, st):
                if _is_link(entry):
                    target = _link_target(path)
                    if target is not None:
                        subdirectories.append(_Subdirectory(path, target))
                else:
                    subdirectories.append(_Subdirectory(path))
        elif path.suffix.lower() in allowed and not entry.name.startswith("~$") and not _is_hidden(path, st):
            files.append((path, st))
    return files, subdirectories


class _LinkGuard:
    """シンボリックリンク・ジャンクションのフォルダを、ループ・重複せずにたどるための判定。

    実体が取り込み元フォルダの中（リンクでないフォルダとしていずれたどる）・取り込み元フォルダを含むフォルダ
    （たどると取り込み元をもう一度たどる）か、既にたどったリンクの実体と同じなら、たどらない
    （自分の親を指すジャンクション等で同じファイルを何度も取り込まないため）。
    """

    def __init__(self, root: Path):
        root_target = _link_target(root)
        self._root = root_target.rstrip("\\/") if root_target else None
        self._visited: set[str] = set()

    def allow(self, subdirectory: _Subdirectory) -> bool:
        target = subdirectory.link_target
        if target is None:
            return True
        target = target.rstrip("\\/")
        if self._root is not None and _is_same_or_inside(target, self._root):
            logger.info("vector_db ingest: not following a link that points inside the source folder: %s", subdirectory.path)
            return False
        if self._root is not None and _is_same_or_inside(self._root, target):
            logger.info("vector_db ingest: not following a link that points to a folder containing the source folder: %s", subdirectory.path)
            return False
        if target in self._visited:
            logger.info("vector_db ingest: not following a link to an already visited folder: %s", subdirectory.path)
            return False
        self._visited.add(target)
        return True


def scan_source_files(
    folder: Path,
    recursive: bool,
    extensions: tuple[str, ...],
    *,
    workers: int = 1,
    max_files: int | None = None,
    known: Collection[str] = (),
) -> ScanResult:
    """取り込み対象のファイルとその stat を探す（Office の一時ファイル ~$*.docx 等と、隠しファイル・隠しフォルダ配下は除く）。

    取り込み元フォルダ自身が隠しフォルダでも対象にする（判定は取り込み元より下の名前だけ）。
    シンボリックリンク・ジャンクションのフォルダもたどるが、ループ・重複になるものはたどらない（_LinkGuard）。
    拡張子は effective_extensions() で絞る。stat は os.scandir() のもの（Windows ではフォルダを読むときに
    サイズ・更新日時・属性も得られるため、ファイルごとに問い合わせずに済む）。

    workers が 2 以上なら、その数のスレッドで複数のフォルダを同時に読む（ファイルサーバー上のフォルダでは
    フォルダを読む往復の待ち時間が大半を占めるため、並列にすると速い）。

    max_files を超えるファイルがあれば、取り込みのたびに選ぶファイルが入れ替わらないよう、known（取り込み済み）の
    ファイルを優先し、残りの枠を新しいファイルのパス順の先頭から埋める。メモリーに持つのは高々 max_files の
    2倍程度のファイルまで。
    """
    allowed = set(effective_extensions(extensions))
    links = _LinkGuard(folder)
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
            directories.extend(d.path for d in subdirectories if links.allow(d))
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vector_db_scan") as pool:
            running = {pool.submit(_read_directory, folder, recursive, allowed)}
            try:
                while running:
                    done, running = wait(running, return_when=FIRST_COMPLETED)
                    for future in done:
                        found, subdirectories = future.result()
                        collect(found)
                        running |= {pool.submit(_read_directory, d.path, recursive, allowed) for d in subdirectories if links.allow(d)}
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


def list_source_files(folder: Path, recursive: bool, extensions: tuple[str, ...]) -> list[Path]:
    """scan_source_files() のファイルのパスだけを返す。"""
    return [path for path, _st in scan_source_files(folder, recursive, extensions).files]


def file_mode(cfg: Config, path: Path) -> str:
    """path の埋め込み方。

    [vector_db].image_input = true で画像化に対応した形式なら "text+image"（本文のチャンクに加えて
    ページ画像も埋め込む。画像ファイルは本文が無いため画像だけ）。image_input = false の画像ファイルは
    "path"（中身は埋め込まず、ファイルパスだけを登録する）。それ以外は "text"。
    """
    return _file_mode(_LoadOptions.from_config(cfg), path)


def _page_chunk_text(cfg: Config, path: Path, page: PageImage) -> str:
    """ページ画像のチャンクの本文（検索結果の抜粋）。ページの文字を chunk_size 文字までにする。"""
    text = page.text.strip()
    if not text:
        if path.suffix.lower() in IMAGE_FILE_EXTENSIONS:
            return "(This is an image file. Check its content in the source image)"
        return "(This page has no text. Check its content by rendering the source page as an image)"
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
            raise IngestError(f"Source folder does not exist: {folder}")
        if collection.model and collection.model != model and not rebuild:
            raise IngestError(
                f"Collection {name} was built with embedding model {collection.model} (current setting: {model}). "
                "Search does not work with a different model, so rebuild it."
            )
        prefix = cfg.vector_db_document_prefix
        if collection.model and collection.document_prefix is not None and collection.document_prefix != prefix and not rebuild:
            raise IngestError(
                f"Collection {name} was ingested with document_prefix {collection.document_prefix!r} (current setting: {prefix!r}). "
                "Mixing prefixes lowers search accuracy, so rebuild it."
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
        "path_only" - 中身は埋め込まず、ファイルパスだけを登録する（image_input = false の画像ファイル、
                      本文も画像にできるページも無かったファイル。reason にその理由）。

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
    # pages の PNG を書き出した一時フォルダ（書き込みが終わったら消す。_Pipeline._finish_job）。
    page_dir: Path | None = None

    def texts_to_embed(self) -> list[str]:
        """埋め込むテキスト（本文のチャンク、続けてファイルパス）。"""
        if self.kind == "embed":
            return [c.text for c in self.chunks] + [self.path_text]
        if self.kind == "path_only":
            return [self.path_text]
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
    office_timeout: float = 0.0
    # ページ画像の PNG を書き出す一時フォルダ（取り込み1回ごとに作る。_ingest）。空ならシステムの一時フォルダ。
    pages_dir: str = ""

    @classmethod
    def from_config(cls, cfg: Config, pages_dir: str = "") -> "_LoadOptions":
        return cls(
            cfg.vector_db_chunk_size,
            cfg.vector_db_chunk_overlap,
            cfg.vector_db_image_input,
            cfg.vector_db_image_dpi,
            cfg.vector_db_max_file_mb,
            cfg.vector_db_office_timeout_seconds,
            pages_dir,
        )

    @property
    def max_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)


def _file_mode(options: _LoadOptions, path: Path) -> str:
    suffix = path.suffix.lower()
    if options.image_input and suffix in IMAGE_EXTENSIONS:
        return "text+image"
    return "path" if suffix in IMAGE_FILE_EXTENSIONS else "text"


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
    ページ画像は options.pages_dir の下のファイルごとの一時フォルダへ PNG で書き出し（_Prepared.page_dir）、
    メモリーには全ページの画像を持たない（400ページの PDF 等でもメモリーを使い切らないため）。
    """
    # image_input を切り替えたら、内容の変わっていないファイルも新しい埋め込み方で取り込み直す。
    mode = _file_mode(options, path)
    path_text = relative_path_text(folder, path)
    if _is_unchanged(record, mode, st):
        return _Prepared(path, record, mode, "unchanged", keyword_chunks=_keyword_chunks(options, path, needs_keywords), path_text=path_text)
    if mode == "path":
        # image_input = false の画像ファイルは中身を読まず、ファイル名・フォルダ名で探せるようパスだけを登録する。
        return _Prepared(path, record, mode, "path_only", stat=st, path_text=path_text, reason="image file (image_input = false)")
    if st.st_size > options.max_bytes:
        reason = f"The file size exceeds the limit ({options.max_file_mb}MB)"
        return _Prepared(path, record, mode, "failed", reason=reason, too_large=True)
    page_dir: Path | None = None
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
        pages: list[PageImage] = []
        if mode == "text+image":
            page_dir = Path(tempfile.mkdtemp(prefix="file_", dir=options.pages_dir or None))
            pages = render_pages(path, options.image_dpi, out_dir=page_dir, office_timeout=options.office_timeout or None)
    except (OSError, ExtractError) as e:
        _remove_dir(page_dir)
        return _Prepared(path, record, mode, "failed", reason=str(e))
    if not pages:
        _remove_dir(page_dir)
        page_dir = None
    if not chunks and not pages:
        # 画像だけの PDF 等でも、ファイル名・フォルダ名で探せるようパスだけを登録する。
        if mode == "text+image":
            reason = "There was neither text nor a page that could be rendered as an image"
        else:
            reason = "Could not extract text (e.g. a PDF of images only; with [vector_db] image_input = true it can be ingested as images)"
        return _Prepared(path, record, mode, "path_only", stat=st, sha=sha, path_text=path_text, reason=reason)
    return _Prepared(path, record, mode, "embed", stat=st, sha=sha, chunks=chunks, pages=pages, path_text=path_text, page_dir=page_dir)


def _remove_dir(directory: Path | None) -> None:
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)


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
                logger.exception("vector_db ingest: rollback failed")
            part._batching = False

    def _maybe_commit(self) -> None:
        self._uncommitted += 1
        if self._uncommitted >= self._COMMIT_EVERY_FILES:
            self._commit()
        else:
            self.commit_if_due()

    def _commit(self) -> None:
        for part in self._parts():
            part.commit()
        self._uncommitted = 0
        self._last_commit = time.monotonic()

    def seconds_until_commit(self) -> float | None:
        """コミットしていない書き込みを、あと何秒でコミットするか（無ければ None）。

        _Pipeline はこの秒数までしか埋め込み・準備の完了を待たない。次のファイルの書き込みを待ってから
        コミットすると、埋め込みの再試行や Office の変換で待つ間も書き込みのロックを持ち続け、同時に動く
        別の取り込みや設定ダッシュボードの書き込みが「database is locked」で失敗するため。
        """
        if not self._uncommitted:
            return None
        return max(0.0, self._COMMIT_INTERVAL_SECONDS - (time.monotonic() - self._last_commit))

    def commit_if_due(self) -> None:
        """前回のコミットから _COMMIT_INTERVAL_SECONDS 秒たっていれば、コミットしていない書き込みをコミットする。"""
        if self._uncommitted and time.monotonic() - self._last_commit >= self._COMMIT_INTERVAL_SECONDS:
            self._commit()

    def _ensure_dim(self, vector: list[float]) -> None:
        """最初の埋め込みで次元を決め、本文の保存先のコレクションを作ってモデルを記録する（作成済みなら何もしない）。"""
        if self.dim is None:
            self.dim = len(vector)
            self.store.ensure_collection(self.name, self.dim)
            self.catalog.set_model(self.name, self.model, self.dim, self.cfg.vector_db_document_prefix)

    def _write_path(self, source: str, prepared: _Prepared) -> None:
        """ファイルパスのベクターを1ファイル1件で入れ替える（store.path_collection_name 参照）。"""
        self._ensure_dim(prepared.path_vector)
        if not self.path_collection_ready:
            self.store.ensure_collection(path_collection_name(self.name), self.dim)
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
        if prepared.kind == "path_only":
            self._write_path_only(source, prepared)
            return
        chunks, pages, vectors, image_vectors = prepared.chunks, prepared.pages, prepared.vectors, prepared.image_vectors
        self._ensure_dim((vectors or image_vectors or [prepared.path_vector])[0])
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

    def _write_path_only(self, source: str, prepared: _Prepared) -> None:
        """中身は埋め込まず、ファイルパスだけを登録する（_Prepared の "path_only"）。

        以前に本文・ページ画像を埋め込んだファイル（image_input を切り替えた等）なら、それらを消す。
        """
        self._ensure_dim(prepared.path_vector)
        for storage in (self.name, image_collection_name(self.name)):
            self.store.delete_source(storage, source)
        self._write_path(source, prepared)
        # チャンクの無いファイルとして記録する（索引の導入前のファイルとして読み直さないため）。
        self.keywords.replace_source(self.name, source, [])
        self.keyword_sources.add(source)
        self._write_path_keywords(source, prepared, only_missing=False)
        st = prepared.stat
        self.catalog.upsert_file(self.name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, 0, prepared.mode, True))
        logger.info("vector_db ingest: registered the path only (%s): %s", prepared.reason, source)
        self.result.path_only += 1
        if prepared.record is None:
            self.result.added += 1
        else:
            self.result.updated += 1

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

    メモリーを使いすぎないよう、準備済みで埋め込みを待っているファイルは _max_waiting 個までにし、ページ画像は
    取り込み1回ごとの一時フォルダ（_pages_root）へ書き出して、埋め込むときに1枚ずつ読む。

    子プロセス（ingest_processes）が異常終了した（PDFium が壊れたファイルで落ちた等）ときは、そのとき準備中だった
    ファイルを1つずつ準備し直し、1つだけでも落ちるファイルを失敗一覧に載せて続ける（_pool_broken）。
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
        # 準備中のファイル: Future → (_load の引数, 1つだけで準備し直しているものか)。
        self.loads: dict[Future, tuple[tuple, bool]] = {}
        self.embeds: dict[Future, tuple[str, list[tuple[_Job, int]]]] = {}
        self.text_queue: deque[tuple[_Job, int]] = deque()
        self.image_queue: deque[tuple[_Job, int]] = deque()
        # 子プロセスの異常終了に巻き込まれ、1つずつ準備し直すファイルの _load の引数。
        self.isolate: deque[tuple] = deque()
        self.waiting = 0
        self.completed = 0
        self.stopped = False
        self.load_pool = None

    def _new_load_pool(self):
        if self.processes:
            return ProcessPoolExecutor(max_workers=self.processes, initializer=_init_load_process)
        return ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="vector_db_load")

    def run(self, files: list[tuple[Path, os.stat_result]]) -> None:
        """files を処理する（停止したら self.stopped が True）。"""
        if self.workers > 1 or self.processes > 1:
            # 大きいファイルから始め、最後に大きいファイルが1つだけ残って待つのを避ける。
            files = sorted(files, key=lambda item: item[1].st_size, reverse=True)
        self.total = len(files)
        self.pending = iter(files)
        self.exhausted = False
        pages_root = tempfile.mkdtemp(prefix="locohane_vdb_pages_")
        self.options = replace(self.options, pages_dir=pages_root)
        self.load_pool = self._new_load_pool()
        embed_pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="vector_db_embed")
        try:
            while True:
                if not self.stopped and self.should_stop is not None and self.should_stop():
                    # 全ファイルの準備を始めた後でも、残りの埋め込みを始めずに止める。
                    self.stopped = True
                self._start_loads(self.load_pool)
                self._start_embeds(embed_pool)
                if not self.loads and not self.embeds:
                    if self.stopped or (self.exhausted and not self.isolate):
                        break
                    continue
                # コミットの期限までしか待たない（_Writer.seconds_until_commit 参照）。
                done, _ = wait(
                    list(self.loads) + list(self.embeds), timeout=self.writer.seconds_until_commit(), return_when=FIRST_COMPLETED
                )
                for future in done:
                    if future in self.loads:
                        args, isolated = self.loads.pop(future)
                        try:
                            prepared = future.result()
                        except BrokenProcessPool:
                            self._pool_broken(args, isolated)
                            continue
                        self._loaded(prepared)
                    elif future in self.embeds:
                        self._embedded(future, *self.embeds.pop(future))
                    # どちらでもなければ、異常終了した子プロセスの準備で、_pool_broken で準備し直しに回したもの。
                self.writer.commit_if_due()
        except BaseException:
            # 埋め込みサーバーに接続できない等で止めるときは、まだ始まっていない処理を始めない。
            for future in list(self.loads) + list(self.embeds):
                future.cancel()
            raise
        finally:
            embed_pool.shutdown(wait=True, cancel_futures=True)
            if not self.processes and self.cfg.vector_db_image_input:
                _close_office_in_threads(self.load_pool, self.workers)
            self.load_pool.shutdown(wait=True, cancel_futures=True)
            # 書き込まずに終えたファイル（停止・エラー）のページ画像もここで消す。
            _remove_dir(Path(pages_root))

    def _pool_broken(self, args: tuple, isolated: bool) -> None:
        """子プロセスが異常終了した。巻き込まれた準備中のファイルを1つずつ準備し直す（新しいプロセスで）。

        1つだけで準備していたファイル（isolated）で落ちたなら、そのファイルが原因のため失敗一覧に載せる。
        """
        path, record = args[2], args[4]
        if isolated:
            logger.warning("vector_db ingest: skipping a file whose child process crashed: %s", path)
            reason = "The child process for text extraction/rendering crashed (broken file, etc.)"
            self._loaded(_Prepared(path, record, _file_mode(self.options, path), "failed", reason=reason))
        else:
            self.isolate.append(args)
        # 同じプロセスプールの他の準備も失敗するため、1つずつ準備し直す。
        for other_args, _isolated in self.loads.values():
            self.isolate.append(other_args)
        self.loads.clear()
        self.load_pool.shutdown(wait=False, cancel_futures=True)
        self.load_pool = self._new_load_pool()

    def _start_loads(self, pool) -> None:
        if self.isolate:
            # 異常終了に巻き込まれたファイルは、他のファイルと同時に準備せず1つずつ準備し直す。
            if not self.loads and not self.stopped:
                args = self.isolate.popleft()
                self.loads[pool.submit(_load, *args)] = (args, True)
            return
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
            self.loads[future] = (args, False)

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
                future = pool.submit(_embed_page, self.cfg, job.prepared.pages[index], self.embedding_client, self.should_stop)
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
        prepared = job.finish()
        self.writer.write(prepared)
        _remove_dir(prepared.page_dir)
        self.completed += 1


def _embed_page(cfg: Config, page: PageImage, client, should_stop) -> list[list[float]]:
    """ページ画像1枚を埋め込む（書き出した PNG は埋め込むときに読む。_Pipeline 参照）。"""
    return embed_images(cfg, [page.read_png()], client=client, should_stop=should_stop)


def _ingest(
    cfg, catalog: Catalog, store, keywords: KeywordIndex, name, dim, model, folder, recursive, result, progress, should_stop, embedding_client
) -> None:
    """取り込み元のファイルを _Pipeline で処理し、フォルダから消えたファイルを消す。

    停止したときは、埋め込みまで終わったファイルだけを記録する（途中のファイルは次回の取り込みで処理する）。
    """
    known = catalog.files(name)
    max_files = cfg.vector_db_max_files_per_collection
    scan = scan_source_files(
        folder, recursive, cfg.vector_db_include_extensions,
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
    if progress is not None:
        # 停止したときも、最後に読み始めたファイルの時点の件数のまま残らないよう、記録まで終わった件数を通知する。
        progress(pipeline.completed if pipeline.stopped else len(files), len(files), "")


def main(argv: list[str] | None = None) -> int:
    from ..config import DEFAULT_INSTANCE_NAME, load_config, resolve_instances_root

    parser = argparse.ArgumentParser(description="Ingest documents into a vector DB collection")
    parser.add_argument("--instance", default=os.getenv("LOCOHANE_INSTANCE") or None, help="Instance name (default if omitted)")
    parser.add_argument("--collection", required=True, help="Collection name (one created in the \"Knowledge\" tab of the settings dashboard)")
    parser.add_argument("--rebuild", action="store_true", help="Delete the existing vectors and ingest every file again")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    instance = args.instance or DEFAULT_INSTANCE_NAME
    # 管理ツールと同じく instances/<名前>/config_overrides.json の上書きを重ねて読む。
    cfg = load_config(overrides_path=resolve_instances_root() / instance / "config_overrides.json", instance_name=instance)
    if not cfg.vector_db_enabled:
        print("[vector_db] enabled = false. Enable it before running this.")
        return 1

    def _progress(done: int, total: int, path: str) -> None:
        if path:
            print(f"[{done + 1}/{total}] {path}")

    result = ingest_collection(cfg, args.collection, rebuild=args.rebuild, progress=_progress)
    summary = result.to_dict()
    print(
        f"Added {summary['added']} / Updated {summary['updated']} / Unchanged {summary['unchanged']} / "
        f"Removed {summary['removed']} / Chunks {summary['chunks']} / Failed {len(summary['failed'])}"
    )
    if summary["path_only"]:
        print(f"  {summary['path_only']} of them had no extractable content, so only their file and folder names were registered.")
    for item in summary["failed"]:
        print(f"  Failed: {item['path']}: {item['reason']}")
    if summary["too_large"]:
        print(f"  {_too_large_hint(cfg, summary['too_large'])}")
    if result.over_limit:
        print(f"  {_over_limit_hint(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
