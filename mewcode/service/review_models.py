"""Service-owned review facts; model output cannot set versions or coverage."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    severity: Literal["high", "medium", "low"]
    path: str = Field(min_length=1, max_length=1024)
    side: Literal["old", "new"]
    line: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=2000)


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(max_length=2000)
    findings: list[Finding] = Field(max_length=20)


@dataclass
class Coverage:
    path: str
    old_path: str = ""
    change: str = "M"
    status: Literal["full", "partial", "none"] = "none"
    reason: str = "not_sent"
    diff_bytes: int = 0
    sent_bytes: int = 0


@dataclass
class ReviewResult:
    review_id: str
    head_sha: str
    base_sha: str = ""
    status: Literal["findings", "no_findings", "incomplete"] = "incomplete"
    reason: str = "not_started"
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    coverage_complete: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    usage_known: bool = False
    request_attempted: bool = False
    budget_charge: int = 0
    elapsed_seconds: float = 0
    provider: str = ""
    model: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
