from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SourceArtifact:
    artifact_id: str
    request_id: str
    path: str
    sha256: str
    media_type: str
    page_count: int = 1

    @classmethod
    def from_file(cls, request_id: str, path: str | Path, media_type: str) -> "SourceArtifact":
        source = Path(path)
        digest = sha256(source.read_bytes()).hexdigest()
        return cls(
            artifact_id=f"artifact:{digest}", request_id=request_id, path=str(source.resolve()),
            sha256=digest, media_type=media_type,
        )


@dataclass(frozen=True)
class OCRObservation:
    observation_id: str
    artifact_id: str
    field: str
    raw_text: str
    normalized_value: str
    confidence: float
    page: int = 1
    bbox: tuple[float, float, float, float] | None = None
    line_id: str = ""

    def __post_init__(self) -> None:
        if not self.observation_id or not self.artifact_id or not self.field:
            raise ValueError("observation_id, artifact_id and field are required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.page <= 0:
            raise ValueError("page must be positive")


@dataclass(frozen=True)
class CompletenessResult:
    complete: bool
    missing_numbers: tuple[str, ...]
    duplicate_numbers: tuple[str, ...]
    low_confidence_observations: tuple[str, ...]


def check_intake_completeness(
    expected_numbers: Iterable[str], observations: Iterable[OCRObservation],
    confidence_threshold: float = 0.85,
) -> CompletenessResult:
    expected = tuple(str(value) for value in expected_numbers)
    observations_tuple = tuple(observations)
    seen = [
        item.normalized_value for item in observations_tuple if item.field == "line_number"
    ]
    duplicates = tuple(sorted({number for number in seen if seen.count(number) > 1}))
    missing = tuple(number for number in expected if number not in seen)
    low = tuple(
        item.observation_id
        for item in observations_tuple
        if item.confidence < confidence_threshold
    )
    return CompletenessResult(not missing and not duplicates and not low, missing, duplicates, low)
