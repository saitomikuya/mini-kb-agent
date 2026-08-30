"""Safe handling for social messages that do not need knowledge retrieval."""

from __future__ import annotations

import re
from typing import Literal

from app.schemas.answers import AnswerResult


SmallTalkKind = Literal["greeting", "thanks", "farewell", "capability"]

_SOCIAL_PATTERNS: tuple[tuple[SmallTalkKind, re.Pattern[str]], ...] = (
    (
        "greeting",
        re.compile(
            r"^(?:(?:你|您)好|哈+喽+|哈+啰+|嗨+|在吗|有人吗|"
            r"早上好|上午好|下午好|晚上好|早安|午安|晚安|"
            r"hello|hi|hey)(?:呀|啊|哇|哦|啦|呢)?[!！?？。,.，~～]*$",
            re.IGNORECASE,
        ),
    ),
    (
        "thanks",
        re.compile(
            r"^(?:谢谢|多谢|感谢|辛苦了|thanks|thank\s+you)"
            r"(?:你|您|啦|了|呀|啊)?[!！。,.，~～]*$",
            re.IGNORECASE,
        ),
    ),
    (
        "farewell",
        re.compile(
            r"^(?:再见|拜拜|回头见|下次见|bye|goodbye)"
            r"(?:啦|了|呀|啊)?[!！。,.，~～]*$",
            re.IGNORECASE,
        ),
    ),
    (
        "capability",
        re.compile(
            r"^(?:你是谁|您是谁|你叫什么|您叫什么|你能做什么|"
            r"你可以做什么|怎么使用你|如何使用你)[?？!！。]*$",
            re.IGNORECASE,
        ),
    ),
)


def detect_small_talk(question: str) -> SmallTalkKind | None:
    """Recognize only complete, unambiguous social messages.

    Full-string matching is deliberate: ``你好，请问产品规格`` must continue
    through normal knowledge navigation instead of being swallowed as a greeting.
    """
    normalized = " ".join(question.strip().split())
    if not normalized:
        return None
    for kind, pattern in _SOCIAL_PATTERNS:
        if pattern.fullmatch(normalized):
            return kind
    return None


def small_talk_answer(
    question: str,
    *,
    app_name: str = "知问",
) -> AnswerResult:
    """Build a source-free response for a non-knowledge conversation turn."""
    kind = detect_small_talk(question)
    messages = {
        "greeting": "你好！有什么可以帮你？",
        "thanks": "不客气！如果还想查询知识库中的内容，随时告诉我。",
        "farewell": "再见！有需要时随时来找我。",
        "capability": (
            f"我是{app_name}，可以帮你查找、整理和下载知识库中的资料。"
            "你可以直接告诉我想了解的内容。"
        ),
    }
    answer_markdown = messages.get(
        kind,
        (
            f"我是{app_name}，主要负责知识库问答和资料查找。"
            "请告诉我想查询的内容。"
        ),
    )
    return AnswerResult(
        answer_markdown=answer_markdown,
        citations=[],
        conflicts=[],
        downloads=[],
        research_handoff=None,
    )
