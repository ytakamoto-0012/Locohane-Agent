"""本文のチャンクのキーワード索引（[vector_db].dir/keywords.sqlite、SQLite FTS5 の BM25）。

意味の近さで探す埋め込みは、型番・品番・数値のような完全一致の語句に弱いため、
キーワードでも探して search.py で埋め込みの結果と順位で統合する。
保存先（backend = sqlite_vec / qdrant）によらず、常にこの SQLite に持つ。

日本語は単語に分けずに使えるよう、漢字・かな等の連続を2文字ずつ（バイグラム）に区切り、
英数字は語ごとに小文字にして索引に入れる（_tokens）。ひらがなだけの2文字（「の幅」でなく
「する」「ます」等）は助詞・語尾で、どの文書にも現れて順位を乱すため除く。
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import unicodedata

from .store import ChunkRecord, Hit

KEYWORDS_FILENAME = "keywords.sqlite"

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS keyword_chunks USING fts5(
    tokens,
    collection UNINDEXED,
    source_path UNINDEXED,
    locator UNINDEXED,
    text UNINDEXED
);
-- 索引に入れたファイル（チャンクの無いファイルも記録し、旧版から移るときの索引の作り直しの要否に使う）。
CREATE TABLE IF NOT EXISTS keyword_sources (
    collection TEXT NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY (collection, source_path)
);
"""

# 英数字の語（型番の "ABC-123" は "abc" と "123" に分かれる。どちらも語として一致する）。
_WORD_RE = re.compile(r"[0-9a-z]+")
_HIRAGANA_RE = re.compile(r"^[ぁ-ゟ]+$")


def _cjk_runs(text: str) -> list[str]:
    """英数字以外の文字（漢字・かな等。文字種が字・数字のもの）の連続を取り出す。長音「ー」も続ける。"""
    runs: list[str] = []
    current: list[str] = []
    for ch in text:
        if not ch.isascii() and (unicodedata.category(ch)[0] in "LN" or ch == "ー"):
            current.append(ch)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _tokens(text: str) -> list[str]:
    """text を索引・検索に使う語の一覧にする（重複を含む。BM25 の語の頻度に使う）。

    全角英数字・半角カナは NFKC で正規化してから区切る。
    """
    text = unicodedata.normalize("NFKC", text).lower()
    tokens = _WORD_RE.findall(text)
    for run in _cjk_runs(text):
        grams = [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
        tokens.extend(g for g in grams if not _HIRAGANA_RE.match(g))
    return tokens


def _match_query(query: str) -> str | None:
    """検索語を FTS5 の MATCH 式（語の OR）にする。語が無ければ None。"""
    unique = list(dict.fromkeys(_tokens(query)))
    if not unique:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in unique)


class KeywordIndex:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / KEYWORDS_FILENAME
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "KeywordIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def has_source(self, collection: str, source_path: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM keyword_sources WHERE collection = ? AND source_path = ?", (collection, source_path)
        ).fetchone()
        return row is not None

    def replace_source(
        self, collection: str, source_path: str, chunks: list[ChunkRecord], *, index_texts: list[str] | None = None
    ) -> None:
        """source_path のチャンクを丸ごと入れ替える（chunks が空でも索引済みとして記録する）。

        index_texts を渡すと、索引の語はチャンクの text ではなくこちら（chunks と同じ順）から作る
        （検索結果の text はチャンクの text のまま）。
        """
        texts = index_texts if index_texts is not None else [c.text for c in chunks]
        with self._conn:
            self._delete_source(collection, source_path)
            self._conn.executemany(
                "INSERT INTO keyword_chunks (tokens, collection, source_path, locator, text) VALUES (?, ?, ?, ?, ?)",
                [(" ".join(_tokens(t)), collection, source_path, c.locator, c.text) for c, t in zip(chunks, texts, strict=True)],
            )
            self._conn.execute("INSERT INTO keyword_sources (collection, source_path) VALUES (?, ?)", (collection, source_path))

    def _delete_source(self, collection: str, source_path: str) -> None:
        self._conn.execute("DELETE FROM keyword_chunks WHERE collection = ? AND source_path = ?", (collection, source_path))
        self._conn.execute("DELETE FROM keyword_sources WHERE collection = ? AND source_path = ?", (collection, source_path))

    def delete_source(self, collection: str, source_path: str) -> None:
        with self._conn:
            self._delete_source(collection, source_path)

    def drop_collection(self, collection: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM keyword_chunks WHERE collection = ?", (collection,))
            self._conn.execute("DELETE FROM keyword_sources WHERE collection = ?", (collection,))

    def search(self, collection: str, query: str, top_k: int) -> list[Hit]:
        """query の語を含むチャンクを BM25 の高い順に最大 top_k 件返す。

        Hit.score は 0.0（BM25 の値はコサイン類似度と比べられないため載せない。順位だけを統合に使う）。
        """
        match = _match_query(query)
        if match is None:
            return []
        rows = self._conn.execute(
            """
            SELECT source_path, locator, text FROM keyword_chunks
            WHERE keyword_chunks MATCH ? AND collection = ?
            ORDER BY bm25(keyword_chunks) LIMIT ?
            """,
            (match, collection, int(top_k)),
        ).fetchall()
        return [Hit(collection=collection, score=0.0, source_path=p, locator=loc, text=t) for p, loc, t in rows]


def open_keyword_index(directory: Path) -> KeywordIndex:
    return KeywordIndex(directory)
