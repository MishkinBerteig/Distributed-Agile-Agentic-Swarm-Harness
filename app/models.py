from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "Pending"
    READY = "Ready"
    IN_PROGRESS = "In-Progress"
    DONE = "Done"


class SwarmStatus(str, Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    USER_FEEDBACK = "USER_FEEDBACK"
    VERIFICATION = "VERIFICATION"
    LEARNING = "LEARNING"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"

    @staticmethod
    def terminal() -> tuple[str, ...]:
        return ("ARCHIVED", "DELETED")

    @staticmethod
    def allowed(next_state: str) -> dict[str, set[str]]:
        return {
            "CREATED":         {"ACTIVE", "DELETED"},
            "ACTIVE":          {"PAUSED", "VERIFICATION"},
            "PAUSED":          {"ACTIVE", "USER_FEEDBACK", "VERIFICATION"},
            "USER_FEEDBACK":   {"ACTIVE"},
            "VERIFICATION":    {"LEARNING", "ARCHIVED", "DELETED"},
            "LEARNING":        {"ARCHIVED"},
            "ARCHIVED":        set(),
            "DELETED":         set(),
        }


# --- Task ---

class TaskBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    parent_id: Optional[str] = None
    team_id: str
    status: TaskStatus = TaskStatus.PENDING
    keywords: List[str] = Field(default_factory=list)
    acceptance_criteria: str = "" # Added back


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    status: Optional[TaskStatus] = None
    keywords: Optional[List[str]] = None
    rejection_reason: Optional[str] = None
    acceptance_criteria: Optional[str] = None # Added back


class Task(TaskBase):
    id: str
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# --- Alignment & Judgement ---

from pydantic import BaseModel


class MissionJudgementRequest(BaseModel):
    """Payload for the Mission Judgement endpoint."""
    team_id: str
    task_text: str


# --- Team ---

class TeamBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    vision_statement: str
    mission_statement: str = ""


class TeamCreate(TeamBase):
    id: str


class Team(TeamBase):
    id: str
    lifecycle_state: str = SwarmStatus.CREATED.value
    created_at: datetime


# --- Memory / Vector ---

class MemoryEntryCreate(BaseModel):
    task_id: str
    content: str = Field(..., min_length=1)
    metadata: dict = Field(default_factory=dict)


class MemoryEntry(BaseModel):
    id: str
    task_id: str
    content: str
    embedding: Optional[List[float]] = None
    metadata: dict
    created_at: datetime


# --- Audit Transcript ---

class TranscriptChunkCreate(BaseModel):
    team_id: str
    agent_role: str
    message: str
    task_id: Optional[str] = None


class TranscriptChunk(BaseModel):
    id: str
    team_id: str
    agent_role: str
    message: str
    task_id: Optional[str] = None
    created_at: datetime
