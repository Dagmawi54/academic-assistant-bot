"""SQLAlchemy ORM models for the academic bot."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class Group(Base):
    """A Telegram group representing one department-year-section."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    department: Mapped[str | None] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(10))
    semester: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_moderation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    topics: Mapped[list["Topic"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    courses: Mapped[list["Course"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class Topic(Base):
    """A forum topic inside a Telegram group."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_thread_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topic_name: Mapped[str] = mapped_column(String(255), nullable=False)
    topic_type: Mapped[str] = mapped_column(
        String(20), default="ignored"
    )  # course, general, discussion, ignored
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, closed
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    group: Mapped["Group"] = relationship(back_populates="topics")
    course: Mapped["Course | None"] = relationship(back_populates="topic", uselist=False)

    __table_args__ = (Index("ix_topic_chat_thread", "chat_id", "message_thread_id", unique=True),)


class Course(Base):
    """A course linked to a topic within a group for a given semester."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), nullable=False)
    course_name: Mapped[str] = mapped_column(String(255), nullable=False)
    semester: Mapped[int] = mapped_column(Integer, nullable=False)
    topic_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("topics.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    group: Mapped["Group"] = relationship(back_populates="courses")
    topic: Mapped["Topic | None"] = relationship(back_populates="course")
    academic_items: Mapped[list["AcademicItem"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class AcademicItem(Base):
    """An academic event: assignment, exam, schedule update, etc."""

    __tablename__ = "academic_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("courses.id"))
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), nullable=False)
    item_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # assignment, exam, quiz, exam_coverage, schedule_update
    title: Mapped[str | None] = mapped_column(String(500))
    deadline: Mapped[datetime | None] = mapped_column(DateTime)
    room: Mapped[str | None] = mapped_column(String(100))
    coverage: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default="new"
    )  # new, verified, active, completed, archived
    version: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float | None] = mapped_column()
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_link: Mapped[str | None] = mapped_column(String(255))
    raw_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    course: Mapped["Course | None"] = relationship(back_populates="academic_items")
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="academic_item", cascade="all, delete-orphan"
    )


class Reminder(Base):
    """A scheduled reminder for an academic item."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("academic_items.id"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int | None] = mapped_column(BigInteger)
    send_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    academic_item: Mapped["AcademicItem"] = relationship(back_populates="reminders")

    __table_args__ = (Index("ix_reminder_pending", "sent", "cancelled", "send_time"),)


class User(Base):
    """A user with a role in a specific group."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20), default="student"
    )  # owner, dept_admin, section_admin, representative, moderator, student
    username: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    group: Mapped["Group"] = relationship(back_populates="users")

    __table_args__ = (Index("ix_user_group", "telegram_user_id", "group_id", unique=True),)


class AuditLog(Base):
    """Audit log for admin actions and system events."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DuplicateLog(Base):
    """Audit trail for when an academic item is suppressed as a duplicate."""

    __tablename__ = "duplicate_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), nullable=False)
    existing_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("academic_items.id"), nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(255))
    raw_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
