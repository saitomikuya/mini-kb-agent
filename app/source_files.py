"""Stable source-file state identifiers."""

from enum import StrEnum


class SourceStatus(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"


class ConversionStatus(StrEnum):
    NEW = "NEW"
    CHANGED = "CHANGED"
    QUEUED = "QUEUED"
    CONVERTING = "CONVERTING"
    READY = "READY"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class IndexStatus(StrEnum):
    NOT_INDEXED = "NOT_INDEXED"
    INDEXED = "INDEXED"
    STALE = "STALE"
