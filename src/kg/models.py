"""Pydantic models for the temporal Knowledge Graph."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class OntologyClass(BaseModel):
    name: str
    description: Optional[str] = None
    created_by: str = "system"


class OntologyRelation(BaseModel):
    name: str
    domain: Optional[str] = None  # None = any class
    range: Optional[str] = None   # None = any class
    description: Optional[str] = None
    created_by: str = "system"


class Entity(BaseModel):
    id: int
    name: str
    entity_class: str
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class Triplet(BaseModel):
    id: int
    subject: str
    subject_class: str
    relation: str
    object: str
    object_class: str
    confidence: float = 1.0
    source_chat: Optional[str] = None
    context: Optional[str] = None
    created_at: str
    updated_at: str
    ttl_seconds: Optional[int] = None
    expires_at: Optional[str] = None
    is_active: bool = True
    invalidated_reason: Optional[str] = None

    def to_line(self) -> str:
        """Compact single-line rendering, used in LLM-facing tool output."""
        line = (
            f"[#{self.id}] {self.subject} ({self.subject_class}) "
            f"--{self.relation}--> {self.object} ({self.object_class}) "
            f"| confidence={self.confidence:.2f}"
        )
        if self.expires_at:
            line += f" | expires={self.expires_at} UTC"
        if not self.is_active:
            line += f" | INACTIVE ({self.invalidated_reason or 'unknown'})"
        if self.context:
            line += f" | context: {self.context}"
        return line

