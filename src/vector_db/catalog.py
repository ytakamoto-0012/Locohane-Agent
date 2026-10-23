"""コレクションの定義と取り込み済みファイルの記録（[vector_db].dir/catalog.sqlite）。

バックエンド（sqlite_vec / qdrant）によらずここに持つ。ベクター本体は store.py 側。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import sqlite3

CATALOG_FILENAME = "catalog.sqlite"

# コレクション名は Qdrant のコレクション名・ログ・URL にそのまま使うため ASCII に限る。
# 日本語の表示名は description に書く。
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    source_folder TEXT NOT NULL,
    recursive INTEGER NOT NULL DEFAULT 1,
    model TEXT,
    dim INTEGER,
    created_at TEXT NOT NULL,
    last_ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
    collection TEXT NOT NULL,
    source_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'text',
    PRIMARY KEY (collection, source_path)
);
"""


class CatalogError(Exception):
    """コレクション名が不正・存在しない・重複している等。"""


@dataclass(frozen=True)
class Collection:
    name: str
    description: str
    source_folder: str
    recursive: bool
    model: str | None
    dim: int | None
    created_at: str
    last_ingested_at: str | None
    file_count: int = 0
    chunk_count: int = 0


@dataclass(frozen=True)
class FileRecord:
    """取り込み済みファイルの記録。mode は埋め込み方（"text" または "image"、ingest.file_mode()）。"""

    source_path: str
    size: int
    mtime: float
    sha256: str
    chunk_count: int
    mode: str = "text"


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise CatalogError(f"コレクション名は半角英数字・ハイフン・アンダースコアの1〜48文字にしてください: {name!r}")
    return name


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Catalog:
    """catalog.sqlite への読み書き。1インスタンスを1スレッドで使う。"""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / CATALOG_FILENAME
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # mode 列の無い旧版の catalog.sqlite に列を足す（既存の記録はテキストとして取り込んだもの）。
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(files)")}
        if "mode" not in columns:
            with self._conn:
                self._conn.execute("ALTER TABLE files ADD COLUMN mode TEXT NOT NULL DEFAULT 'text'")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- コレクション ---

    def list_collections(self) -> list[Collection]:
        rows = self._conn.execute(
            """
            SELECT c.*, COUNT(f.source_path) AS file_count, COALESCE(SUM(f.chunk_count), 0) AS chunk_count
            FROM collections c LEFT JOIN files f ON f.collection = c.name
            GROUP BY c.name ORDER BY c.name
            """
        ).fetchall()
        return [_to_collection(r) for r in rows]

    def get(self, name: str) -> Collection:
        for c in self.list_collections():
            if c.name == name:
                return c
        raise CatalogError(f"コレクション {name} はありません。")

    def create(self, name: str, source_folder: str, description: str = "", recursive: bool = True) -> Collection:
        validate_name(name)
        if not source_folder.strip():
            raise CatalogError("取り込み元フォルダを指定してください。")
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO collections (name, description, source_folder, recursive, created_at) VALUES (?, ?, ?, ?, ?)",
                    (name, description.strip(), source_folder.strip(), int(recursive), _now()),
                )
        except sqlite3.IntegrityError as e:
            raise CatalogError(f"コレクション {name} は既にあります。") from e
        return self.get(name)

    def update(self, name: str, *, source_folder: str | None = None, description: str | None = None, recursive: bool | None = None) -> Collection:
        self.get(name)
        with self._conn:
            if source_folder is not None:
                if not source_folder.strip():
                    raise CatalogError("取り込み元フォルダを指定してください。")
                self._conn.execute("UPDATE collections SET source_folder = ? WHERE name = ?", (source_folder.strip(), name))
            if description is not None:
                self._conn.execute("UPDATE collections SET description = ? WHERE name = ?", (description.strip(), name))
            if recursive is not None:
                self._conn.execute("UPDATE collections SET recursive = ? WHERE name = ?", (int(recursive), name))
        return self.get(name)

    def delete(self, name: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM files WHERE collection = ?", (name,))
            self._conn.execute("DELETE FROM collections WHERE name = ?", (name,))

    def set_model(self, name: str, model: str | None, dim: int | None) -> None:
        with self._conn:
            self._conn.execute("UPDATE collections SET model = ?, dim = ? WHERE name = ?", (model, dim, name))

    def mark_ingested(self, name: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE collections SET last_ingested_at = ? WHERE name = ?", (_now(), name))

    # --- 取り込み済みファイル ---

    def files(self, name: str) -> dict[str, FileRecord]:
        rows = self._conn.execute(
            "SELECT source_path, size, mtime, sha256, chunk_count, mode FROM files WHERE collection = ?", (name,)
        ).fetchall()
        return {r["source_path"]: FileRecord(**dict(r)) for r in rows}

    def upsert_file(self, name: str, record: FileRecord) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO files (collection, source_path, size, mtime, sha256, chunk_count, mode) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (collection, source_path) DO UPDATE SET
                    size = excluded.size, mtime = excluded.mtime, sha256 = excluded.sha256, chunk_count = excluded.chunk_count,
                    mode = excluded.mode
                """,
                (name, record.source_path, record.size, record.mtime, record.sha256, record.chunk_count, record.mode),
            )

    def delete_file(self, name: str, source_path: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM files WHERE collection = ? AND source_path = ?", (name, source_path))

    def clear_files(self, name: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM files WHERE collection = ?", (name,))


def _to_collection(row: sqlite3.Row) -> Collection:
    return Collection(
        name=row["name"],
        description=row["description"],
        source_folder=row["source_folder"],
        recursive=bool(row["recursive"]),
        model=row["model"],
        dim=row["dim"],
        created_at=row["created_at"],
        last_ingested_at=row["last_ingested_at"],
        file_count=row["file_count"],
        chunk_count=row["chunk_count"],
    )


def open_catalog(directory: Path) -> Catalog:
    return Catalog(directory)


def catalog_exists(directory: Path) -> bool:
    """catalog.sqlite が既にあるか（無ければ作らずに「コレクション無し」と扱いたい読み取り側用）。"""
    return (directory / CATALOG_FILENAME).is_file()


__all__ = ["Catalog", "CatalogError", "Collection", "FileRecord", "catalog_exists", "open_catalog", "validate_name"]
