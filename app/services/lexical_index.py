"""Local deterministic lexical recall for immutable index generations.

The SQLite FTS artifact is a derived candidate index, never a source of
authority.  Navigation must still validate every returned id against the
canonical root/folder JSON and document cards before loading Markdown.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import unicodedata


LEXICAL_INDEX_FILENAME = "lexical.sqlite3"
LEXICAL_INDEX_VERSION = "lexical-part-v1"

_CJK_OR_WORD = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[a-z0-9]+",
    re.IGNORECASE,
)
_QUERY_STOP_TERMS = {
    "一下",
    "一个",
    "什么",
    "信息",
    "内容",
    "哪个",
    "哪些",
    "哪天",
    "哪里",
    "多少",
    "如何",
    "怎么",
    "情况",
    "我想",
    "文件",
    "文档",
    "是否",
    "有关",
    "材料",
    "查找",
    "相关",
    "给出",
    "请问",
    "资料",
    "这个",
    "那个",
    "里面",
    "项目",
}


class LexicalIndexError(RuntimeError):
    """A present lexical index is malformed or cannot be queried safely."""


@dataclass(frozen=True, slots=True)
class LexicalPartRecord:
    folder_id: str
    document_id: str
    part_id: str
    source_path: str
    title: str
    document_type: str
    topics: tuple[str, ...]
    entities: tuple[str, ...]
    label: str
    summary: str
    body: str


@dataclass(frozen=True, slots=True)
class LexicalCandidate:
    folder_id: str
    document_id: str
    part_id: str
    rank: int
    score: float


def build_lexical_index(
    path: Path,
    records: Sequence[LexicalPartRecord],
) -> None:
    """Build one self-contained FTS5 database inside a staging generation."""
    if path.exists() or path.is_symlink():
        raise LexicalIndexError("Lexical index destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.executescript(
            """
            CREATE TABLE lexical_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE VIRTUAL TABLE part_search USING fts5(
                folder_id UNINDEXED,
                document_id UNINDEXED,
                part_id UNINDEXED,
                source_path,
                title,
                metadata,
                part_summary,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO part_search (
                folder_id,
                document_id,
                part_id,
                source_path,
                title,
                metadata,
                part_summary,
                body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    record.folder_id,
                    record.document_id,
                    record.part_id,
                    _index_terms(record.source_path),
                    _index_terms(f"{record.title} {record.document_type}"),
                    _index_terms(" ".join((*record.topics, *record.entities))),
                    _index_terms(f"{record.label} {record.summary}"),
                    _index_terms(record.body),
                )
                for record in records
            ),
        )
        connection.executemany(
            "INSERT INTO lexical_metadata (key, value) VALUES (?, ?)",
            (
                ("schema_version", LEXICAL_INDEX_VERSION),
                ("part_count", str(len(records))),
            ),
        )
        connection.execute("INSERT INTO part_search(part_search) VALUES('optimize')")
        connection.execute(
            "INSERT INTO part_search(part_search) VALUES('integrity-check')"
        )
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        raise LexicalIndexError("Could not build the lexical index") from exc
    finally:
        connection.close()


def validate_lexical_index(
    path: Path,
    expected_parts: Iterable[tuple[str, str, str]],
) -> None:
    """Validate schema metadata and the complete canonical id whitelist."""
    expected = set(expected_parts)
    connection = _open_readonly(path)
    try:
        metadata = dict(connection.execute("SELECT key, value FROM lexical_metadata"))
        if metadata != {
            "schema_version": LEXICAL_INDEX_VERSION,
            "part_count": str(len(expected)),
        }:
            raise LexicalIndexError("Lexical index metadata is invalid")
        actual = {
            (str(folder_id), str(document_id), str(part_id))
            for folder_id, document_id, part_id in connection.execute(
                "SELECT folder_id, document_id, part_id FROM part_search"
            )
        }
        actual_count = int(
            connection.execute("SELECT count(*) FROM part_search").fetchone()[0]
        )
        if actual != expected or actual_count != len(expected):
            raise LexicalIndexError("Lexical index part set is incomplete")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise LexicalIndexError("Lexical index failed SQLite validation")
    except sqlite3.Error as exc:
        raise LexicalIndexError("Lexical index validation failed") from exc
    finally:
        connection.close()


def search_lexical_index(
    path: Path,
    question: str,
    *,
    limit: int,
    per_document_limit: int | None = None,
) -> list[LexicalCandidate]:
    """Return ranked part candidates without reading any content into a model."""
    if limit <= 0:
        return []
    bounded_limit = min(limit, 200)
    bounded_per_document_limit = (
        min(per_document_limit, bounded_limit)
        if isinstance(per_document_limit, int)
        and not isinstance(per_document_limit, bool)
        and per_document_limit > 0
        else bounded_limit
    )
    # Over-fetch before applying document diversity. Otherwise a very large
    # document can monopolize the global BM25 top-k and hide a relevant match
    # in another source.
    query_limit = 200
    if not path.exists():
        # Generations created before lexical-part-v1 remain readable.  The next
        # normal index build adds the derived database atomically.
        return []
    terms = _query_terms(question)
    if not terms:
        return []
    match_expression = " OR ".join(f'"{term}"' for term in terms)
    connection = _open_readonly(path)
    try:
        metadata = dict(connection.execute("SELECT key, value FROM lexical_metadata"))
        if metadata.get("schema_version") != LEXICAL_INDEX_VERSION:
            raise LexicalIndexError("Lexical index version is unsupported")
        rows = connection.execute(
            """
            SELECT
                folder_id,
                document_id,
                part_id,
                bm25(part_search, 0.0, 0.0, 0.0, 12.0, 10.0, 8.0, 6.0, 1.0)
                    AS lexical_score
            FROM part_search
            WHERE part_search MATCH ?
            ORDER BY lexical_score ASC, rowid ASC
            LIMIT ?
            """,
            (match_expression, query_limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise LexicalIndexError("Lexical candidate search failed") from exc
    finally:
        connection.close()
    results: list[LexicalCandidate] = []
    document_counts: dict[str, int] = {}
    for row in rows:
        document_id = str(row[1])
        if document_counts.get(document_id, 0) >= bounded_per_document_limit:
            continue
        document_counts[document_id] = document_counts.get(document_id, 0) + 1
        results.append(
            LexicalCandidate(
                folder_id=str(row[0]),
                document_id=document_id,
                part_id=str(row[2]),
                rank=len(results) + 1,
                score=float(row[3]),
            )
        )
        if len(results) >= bounded_limit:
            break
    return results


def _open_readonly(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise LexicalIndexError("Lexical index is missing or unsafe")
    try:
        return sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
    except (OSError, sqlite3.Error) as exc:
        raise LexicalIndexError("Lexical index cannot be opened read-only") from exc


def _index_terms(value: str) -> str:
    # A presence-oriented term set keeps the local index bounded.  CJK bigrams
    # and trigrams provide substring recall without an external tokenizer.
    return " ".join(dict.fromkeys(_lexical_terms(value)))


def _query_terms(value: str) -> list[str]:
    result: list[str] = []
    for term in _lexical_terms(value):
        if term in _QUERY_STOP_TERMS or term in result:
            continue
        result.append(term)
        if len(result) >= 48:
            break
    return result


def _lexical_terms(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    terms: list[str] = []
    for match in _CJK_OR_WORD.finditer(normalized):
        token = match.group(0)
        if _is_cjk(token[0]):
            if len(token) == 1:
                terms.append(token)
                continue
            terms.extend(token[index : index + 2] for index in range(len(token) - 1))
            if len(token) >= 3:
                terms.extend(
                    token[index : index + 3] for index in range(len(token) - 2)
                )
            if len(token) <= 8:
                terms.append(token)
        elif len(token) >= 2 or token.isdigit():
            terms.append(token[:200])
    return terms


def _is_cjk(character: str) -> bool:
    return (
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
    )
