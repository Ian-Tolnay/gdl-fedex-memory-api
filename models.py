from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict


class RecordType(str, Enum):
    project_state = "project_state"
    decision = "decision"
    architecture = "architecture"
    task = "task"
    issue = "issue"
    validation = "validation"
    source_note = "source_note"
    shipping_rule = "shipping_rule"
    wix_event_flow = "wix_event_flow"
    fedex_api_note = "fedex_api_note"
    inventory_note = "inventory_note"
    handover_summary = "handover_summary"
    operator_runbook = "operator_runbook"
    note = "note"


class Priority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class MemoryStatus(str, Enum):
    draft = "draft"
    pending_review = "pending_review"
    active = "active"
    superseded = "superseded"
    deprecated = "deprecated"


class ReviewStatus(str, Enum):
    auto_accepted = "auto_accepted"
    pending_review = "pending_review"
    reviewed = "reviewed"
    rejected = "rejected"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryWriteRequest(BaseModel):
    """Stable, GPT-friendly memory write envelope."""
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., description="Stable project ID, e.g. gdl-fedex-mk2")
    record_type: RecordType = Field(default=RecordType.note)
    title: str = Field(..., min_length=1, max_length=200)
    raw_body: str = Field(..., min_length=1, description="Full human-readable evidence or note")
    human_summary: Optional[str] = Field(default=None, max_length=2000)
    status: MemoryStatus = Field(default=MemoryStatus.pending_review)
    priority: Priority = Field(default=Priority.medium)
    tags: List[str] = Field(default_factory=list)
    linked_record_ids: List[str] = Field(default_factory=list)
    source_refs: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.75, ge=0, le=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    review_status: ReviewStatus = Field(default=ReviewStatus.pending_review)


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    query: str = Field(..., min_length=1)
    record_types: List[RecordType] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    status: Optional[MemoryStatus] = None
    limit: int = Field(default=8, ge=1, le=25)
    include_raw: bool = Field(default=False)


class QuickCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    capture_type: RecordType = Field(default=RecordType.note)
    text: str = Field(..., min_length=1)
    title: Optional[str] = Field(default=None, max_length=200)
    priority: Priority = Field(default=Priority.medium)
    tags: List[str] = Field(default_factory=list)
    source_ref: Optional[str] = None
    review_status: ReviewStatus = Field(default=ReviewStatus.pending_review)


class SessionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str
    body: Optional[str] = None
    priority: Priority = Field(default=Priority.medium)
    tags: List[str] = Field(default_factory=list)


class SessionCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    session_title: str
    session_summary: str
    decisions: List[SessionItem] = Field(default_factory=list)
    tasks: List[SessionItem] = Field(default_factory=list)
    issues: List[SessionItem] = Field(default_factory=list)
    architecture_notes: List[SessionItem] = Field(default_factory=list)
    validation_notes: List[SessionItem] = Field(default_factory=list)
    next_actions: List[str] = Field(default_factory=list)
    source_chat_ref: Optional[str] = None
    review_status: ReviewStatus = Field(default=ReviewStatus.pending_review)


class ValidationLogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    system: str
    endpoint_or_feature: str
    test_case: str
    expected_result: str
    actual_result: str
    result: str = Field(..., description="pass, fail, partial, or blocked")
    evidence_ref: Optional[str] = None
    linked_issue_id: Optional[str] = None


class FileRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    id: str
    mime_type: Optional[str] = None
    download_link: Optional[str] = None


class FileCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    note: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    openaiFileIdRefs: List[FileRef] = Field(
        default_factory=list,
        description="Files attached to the ChatGPT conversation. GPT Actions provides download links valid for a short time.",
    )


class ContextBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    query: str
    token_budget: int = Field(default=2500, ge=500, le=15000)
    record_types: List[RecordType] = Field(default_factory=list)
    include_raw: bool = False
