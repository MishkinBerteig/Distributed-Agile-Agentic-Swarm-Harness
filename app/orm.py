from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.models import TaskStatus


class Base(DeclarativeBase):
    pass


class TeamORM(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vision_statement: Mapped[str] = mapped_column(Text, nullable=False)
    mission_statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tasks: Mapped[List["TaskORM"]] = relationship("TaskORM", back_populates="team", cascade="all, delete-orphan")


class TaskORM(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    acceptance_criteria: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="Pending")
    keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    embedding: Mapped[Optional[Vector]] = mapped_column(Vector(768), nullable=True)

    team: Mapped["TeamORM"] = relationship("TeamORM", back_populates="tasks")
    parent: Mapped[Optional["TaskORM"]] = relationship("TaskORM", remote_side=[id], backref="subtasks")


class MemoryEntryORM(Base):
    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Optional[Vector]] = mapped_column(Vector(768), nullable=True)
    memory_metadata: Mapped[dict] = mapped_column(Text, nullable=False, default={})
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TranscriptORM(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False)
    agent_role: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
