"""Deterministic local lexical candidate-index tests."""

from pathlib import Path

from app.services.lexical_index import (
    LEXICAL_INDEX_FILENAME,
    LexicalPartRecord,
    build_lexical_index,
    search_lexical_index,
    validate_lexical_index,
)


def _record(
    document_id: str,
    part_id: str,
    *,
    title: str,
    body: str,
) -> LexicalPartRecord:
    return LexicalPartRecord(
        folder_id="contracts",
        document_id=document_id,
        part_id=part_id,
        source_path=f"项目合同/{title}.md",
        title=title,
        document_type="markdown",
        topics=("合同",),
        entities=("甲方", "乙方"),
        label="原文证据",
        summary="简短摘要未收录长尾数值。",
        body=body,
    )


def test_chinese_terms_and_exact_identifiers_recall_body_only_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / LEXICAL_INDEX_FILENAME
    records = [
        _record(
            "1",
            "part-001",
            title="付款信息",
            body="乙方账号为0200003319221666670，开户行为工商银行。",
        ),
        _record(
            "2",
            "part-003",
            title="保密协议",
            body="双方于2024年3月20日签订保密协议，未特别规定的信息需永久保密。",
        ),
    ]
    build_lexical_index(path, records)
    validate_lexical_index(
        path,
        {(record.folder_id, record.document_id, record.part_id) for record in records},
    )

    account = search_lexical_index(path, "乙方账号是多少？", limit=5)
    identifier = search_lexical_index(path, "0200003319221666670", limit=5)
    confidentiality = search_lexical_index(
        path,
        "保密协议是哪天签的，保密期限多久？",
        limit=5,
    )

    assert account[0].document_id == "1"
    assert identifier[0].document_id == "1"
    assert confidentiality[0].document_id == "2"
    assert confidentiality[0].part_id == "part-003"


def test_missing_pre_upgrade_index_is_a_safe_no_candidate_fallback(
    tmp_path: Path,
) -> None:
    assert search_lexical_index(
        tmp_path / LEXICAL_INDEX_FILENAME,
        "保密期限",
        limit=5,
    ) == []


def test_candidate_diversity_prevents_one_large_document_monopoly(
    tmp_path: Path,
) -> None:
    path = tmp_path / LEXICAL_INDEX_FILENAME
    records = [
        _record(
            "1",
            f"part-{number:03d}",
            title="超长投标文件",
            body=f"统一检索词，第 {number} 部分。",
        )
        for number in range(1, 31)
    ]
    records.append(
        _record(
            "2",
            "part-001",
            title="独立验收材料",
            body="统一检索词，独立来源。",
        )
    )
    build_lexical_index(path, records)

    candidates = search_lexical_index(
        path,
        "统一检索词",
        limit=5,
        per_document_limit=2,
    )

    assert len([item for item in candidates if item.document_id == "1"]) == 2
    assert any(item.document_id == "2" for item in candidates)
