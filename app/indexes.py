"""Stable hierarchical-index generation state identifiers."""

from enum import StrEnum


class IndexGenerationStatus(StrEnum):
    BUILDING = "BUILDING"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"
