"""
Pydantic schemas for the Store Intelligence API.

The Event model is the contract between the detection pipeline (Part A) and the
Intelligence API (Part B). It mirrors the schema in the challenge spec exactly.
Validation here is deliberately strict on structure but lenient on confidence:
low-confidence detections must be ingested and flagged, never silently dropped.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, ConfigDict


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class EventMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    queue_depth: Optional[int] = Field(
        None,
        ge=0,
        description="Observed billing queue depth"
    )

    sku_zone: Optional[str] = None

    session_seq: Optional[int] = Field(
        None,
        ge=1,
        description="Visitor session sequence number"
    )


class Event(BaseModel):
    """A single behavioural event emitted by the detection layer."""

    model_config = ConfigDict(use_enum_values=True)

    event_id: str = Field(
        ...,
        description="Globally unique UUID"
    )

    store_id: str
    camera_id: str
    visitor_id: str

    event_type: EventType

    timestamp: datetime

    zone_id: Optional[str] = None

    dwell_ms: int = Field(
        0,
        ge=0,
        description="Milliseconds spent in the current zone"
    )

    is_staff: bool = False

    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
    )

    metadata: EventMetadata = Field(
        default_factory=EventMetadata
    )

    @field_validator("event_id")
    @classmethod
    def _validate_uuid(cls, v: str) -> str:
        """
        Accept any UUID form (v1/v4/v5/etc.).
        Reject malformed identifiers so ingestion can return
        structured per-record errors instead of a server error.
        """
        try:
            UUID(v)
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                "event_id must be a valid UUID"
            )

        return v


class IngestRequest(BaseModel):
    events: List[dict] = Field(
        ...,
        max_length=500,
    )


class IngestRecordError(BaseModel):
    index: int
    event_id: Optional[str] = None
    error: str


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int

    errors: List[IngestRecordError] = Field(
        default_factory=list
    )


class Anomaly(BaseModel):
    anomaly_type: str
    severity: Severity
    store_id: str
    detected_at: datetime
    detail: str
    suggested_action: str