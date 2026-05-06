"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    report_keys: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    receipts: Mapped[list[Receipt]] = relationship(
        back_populates="trip", cascade="all, delete-orphan", order_by="Receipt.seq"
    )

    __table_args__ = (
        Index(
            "ix_trips_user_active_unique",
            "user_id",
            unique=True,
            postgresql_where="status = 'active'",
        ),
    )


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    trip: Mapped[Trip] = relationship(back_populates="receipts")
    expense: Mapped[Expense | None] = relationship(
        back_populates="receipt", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("trip_id", "seq", name="uq_receipts_trip_seq"),)


class Expense(Base):
    __tablename__ = "expenses"

    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), primary_key=True
    )
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    subtotal: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    vat: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    line_items: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    receipt: Mapped[Receipt] = relationship(back_populates="expense")
