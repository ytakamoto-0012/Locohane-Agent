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
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# 自動取り込みの時刻は1コレクションあたりこの数まで（毎時でも24で足りる）。
MAX_SCHEDULE_TIMES = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    source_folder TEXT NOT NULL,
    recursive INTEGER NOT NULL DEFAULT 1,
    model TEXT,
    dim INTEGER,
    document_prefix TEXT,
    created_at TEXT NOT NULL,
    last_ingested_at TEXT,
    schedule_enabled INTEGER NOT NULL DEFAULT 0,
    schedule_times TEXT NOT NULL DEFAULT '',
    schedule_weekdays TEXT NOT NULL DEFAULT ''
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
    # 取り込みに使った [vector_db].document_prefix（None は記録の無い旧版で作ったもの）。
    document_prefix: str | None = None
    # 時間指定の自動取り込み（差分）。時刻は "HH:MM"、曜日は 0=月〜6=日で空なら毎日（admin/knowledge.py の IngestScheduler）。
    schedule_enabled: bool = False
    schedule_times: tuple[str, ...] = ()
    schedule_weekdays: tuple[int, ...] = ()


@dataclass(frozen=True)
class FileRecord:
    """取り込み済みファイルの記録。mode は埋め込み方（"text" または "text+image"、ingest.file_mode()）。"""

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


def normalize_schedule_times(times: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """自動取り込みの時刻（"H:MM"・"HH:MM"）を検証し、"HH:MM" に揃えて重複を除き昇順にする。"""
    result: set[str] = set()
    for raw in times:
        value = raw.strip()
        if not value:
            continue
        m = _TIME_RE.match(value)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise CatalogError(f"自動取り込みの時刻は 00:00〜23:59 の形式で指定してください: {value!r}")
        result.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    if len(result) > MAX_SCHEDULE_TIMES:
        raise CatalogError(f"自動取り込みの時刻は {MAX_SCHEDULE_TIMES} 個までです。")
    return tuple(sorted(result))


def normalize_schedule_weekdays(weekdays: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    """自動取り込みの曜日（0=月〜6=日）を検証し、重複を除き昇順にする。空は毎日。"""
    for d in weekdays:
        if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6:
            raise CatalogError(f"自動取り込みの曜日は 0（月）〜6（日）で指定してください: {d!r}")
    return tuple(sorted(set(weekdays)))


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
        # document_prefix 列の無い旧版の catalog.sqlite に列を足す（既存のコレクションは NULL ＝ 不明）。
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(collections)")}
        if "document_prefix" not in columns:
            with self._conn:
                self._conn.execute("ALTER TABLE collections ADD COLUMN document_prefix TEXT")
        # 自動取り込みの列の無い旧版の catalog.sqlite に列を足す（既存のコレクションは自動取り込みなし）。
        for column, ddl in (
            ("schedule_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("schedule_times", "TEXT NOT NULL DEFAULT ''"),
            ("schedule_weekdays", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                with self._conn:
                    self._conn.execute(f"ALTER TABLE collections ADD COLUMN {column} {ddl}")

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

    def update(
        self,
        name: str,
        *,
        source_folder: str | None = None,
        description: str | None = None,
        recursive: bool | None = None,
        schedule_enabled: bool | None = None,
        schedule_times: list[str] | tuple[str, ...] | None = None,
        schedule_weekdays: list[int] | tuple[int, ...] | None = None,
    ) -> Collection:
        current = self.get(name)
        times = normalize_schedule_times(schedule_times) if schedule_times is not None else current.schedule_times
        weekdays = normalize_schedule_weekdays(schedule_weekdays) if schedule_weekdays is not None else current.schedule_weekdays
        enabled = schedule_enabled if schedule_enabled is not None else current.schedule_enabled
        if enabled and not times:
            raise CatalogError("自動取り込みを有効にするときは時刻を1つ以上指定してください。")
        with self._conn:
            if schedule_enabled is not None or schedule_times is not None or schedule_weekdays is not None:
                self._conn.execute(
                    "UPDATE collections SET schedule_enabled = ?, schedule_times = ?, schedule_weekdays = ? WHERE name = ?",
                    (int(enabled), ",".join(times), ",".join(str(d) for d in weekdays), name),
                )
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

    def set_model(self, name: str, model: str | None, dim: int | None, document_prefix: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE collections SET model = ?, dim = ?, document_prefix = ? WHERE name = ?", (model, dim, document_prefix, name)
            )

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
        document_prefix=row["document_prefix"],
        schedule_enabled=bool(row["schedule_enabled"]),
        schedule_times=tuple(t for t in row["schedule_times"].split(",") if t),
        schedule_weekdays=tuple(int(d) for d in row["schedule_weekdays"].split(",") if d),
    )


def open_catalog(directory: Path) -> Catalog:
    return Catalog(directory)


def catalog_exists(directory: Path) -> bool:
    """catalog.sqlite が既にあるか（無ければ作らずに「コレクション無し」と扱いたい読み取り側用）。"""
    return (directory / CATALOG_FILENAME).is_file()


__all__ = [
    "Catalog",
    "CatalogError",
    "Collection",
    "FileRecord",
    "catalog_exists",
    "normalize_schedule_times",
    "normalize_schedule_weekdays",
    "open_catalog",
    "validate_name",
]
