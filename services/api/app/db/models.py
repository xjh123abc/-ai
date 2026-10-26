from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSON_DOCUMENT


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AccountSet(Base):
    __tablename__ = "account_sets"
    __table_args__ = (Index("ix_account_sets_tenant", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    chart_accounts: Mapped[list["ChartAccount"]] = relationship(back_populates="account_set", cascade="all, delete-orphan")


class AppUser(Base):
    __tablename__ = "app_users"
    __table_args__ = (UniqueConstraint("email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "account_set_id"),
        Index("ix_memberships_scope", "tenant_id", "account_set_id", "role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    account_set_id: Mapped[str] = mapped_column(ForeignKey("account_sets.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_expiry", "user_id", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False)
    membership_id: Mapped[str] = mapped_column(ForeignKey("tenant_memberships.id", ondelete="CASCADE"), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChartAccount(Base):
    __tablename__ = "chart_accounts"
    __table_args__ = (
        UniqueConstraint("account_set_id", "code"),
        Index("ix_chart_accounts_set_active", "account_set_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_set_id: Mapped[str] = mapped_column(ForeignKey("account_sets.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_leaf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_auxiliary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    account_set: Mapped[AccountSet] = relationship(back_populates="chart_accounts")


class AccountingPeriod(Base):
    __tablename__ = "accounting_periods"
    __table_args__ = (UniqueConstraint("account_set_id", "period"), CheckConstraint("status IN ('OPEN','CLOSED')", name="valid_period_status"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_set_id: Mapped[str] = mapped_column(ForeignKey("account_sets.id", ondelete="CASCADE"), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class DocumentAsset(Base):
    __tablename__ = "document_assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_set_id", "sha256"),
        Index("ix_document_assets_scope_status", "tenant_id", "account_set_id", "processing_status"),
        CheckConstraint("byte_size > 0", name="positive_byte_size"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    account_set_id: Mapped[str] = mapped_column(ForeignKey("account_sets.id", ondelete="RESTRICT"), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    origin: Mapped[str] = mapped_column(String(50), nullable=False, default="USER_UPLOAD")
    processing_status: Mapped[str] = mapped_column(String(40), nullable=False, default="UPLOADED")
    uploaded_by: Mapped[str] = mapped_column(String(200), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    extractions: Mapped[list["DocumentExtraction"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentExtraction(Base):
    __tablename__ = "document_extractions"
    __table_args__ = (UniqueConstraint("document_id", "version"), Index("ix_extractions_type", "document_type"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("document_assets.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    document_type: Mapped[str] = mapped_column(String(50), nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fields: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    lines: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="NEEDS_REVIEW")
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    document: Mapped[DocumentAsset] = relationship(back_populates="extractions")


class BusinessEvent(Base):
    __tablename__ = "business_events"
    __table_args__ = (Index("ix_business_events_scope_status", "tenant_id", "account_set_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    account_set_id: Mapped[str] = mapped_column(ForeignKey("account_sets.id", ondelete="RESTRICT"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    party_ref: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    event_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="NEEDS_REVIEW")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False, default="demo-purchase-v1")
    findings: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    documents: Mapped[list["BusinessEventDocument"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class BusinessEventDocument(Base):
    __tablename__ = "business_event_documents"
    __table_args__ = (
        UniqueConstraint("event_id", "document_id"),
        Index("ix_business_event_documents_document", "document_id"),
        CheckConstraint("allocated_amount IS NULL OR allocated_amount >= 0", name="nonnegative_allocated_amount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("business_events.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("document_assets.id", ondelete="RESTRICT"), nullable=False)
    extraction_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False, default="USER_CONFIRMED")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    allocated_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    confirmed_by: Mapped[str] = mapped_column(String(200), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    event: Mapped[BusinessEvent] = relationship(back_populates="documents")


class ExceptionCase(Base):
    __tablename__ = "exception_cases"
    __table_args__ = (Index("ix_exception_cases_scope_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("business_events.id", ondelete="SET NULL"))
    case_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="OPEN")
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="REVIEW")
    details: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    source_refs: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VoucherDraft(Base):
    __tablename__ = "voucher_drafts"
    __table_args__ = (
        UniqueConstraint("event_id", "version"),
        Index("ix_voucher_drafts_scope_status", "tenant_id", "account_set_id", "status"),
        CheckConstraint("debit_total >= 0 AND credit_total >= 0", name="nonnegative_totals"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    account_set_id: Mapped[str] = mapped_column(ForeignKey("account_sets.id", ondelete="RESTRICT"), nullable=False)
    accounting_period: Mapped[str] = mapped_column(String(7), nullable=False)
    accounting_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    validations: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    debit_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    credit_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    export_key: Mapped[str | None] = mapped_column(String(500))

    lines: Mapped[list["VoucherDraftLine"]] = relationship(back_populates="draft", cascade="all, delete-orphan", order_by="VoucherDraftLine.sequence")


class VoucherDraftLine(Base):
    __tablename__ = "voucher_draft_lines"
    __table_args__ = (
        UniqueConstraint("draft_id", "sequence"),
        CheckConstraint("debit >= 0 AND credit >= 0", name="nonnegative_line_amounts"),
        CheckConstraint("NOT (debit > 0 AND credit > 0)", name="single_sided_line"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(ForeignKey("voucher_drafts.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    account_code: Mapped[str] = mapped_column(String(40), nullable=False)
    account_name: Mapped[str] = mapped_column(String(200), nullable=False)
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    auxiliary: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    evidence_refs: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)

    draft: Mapped[VoucherDraft] = relationship(back_populates="lines")


class VoucherApproval(Base):
    __tablename__ = "voucher_approvals"
    __table_args__ = (Index("ix_approvals_draft_version", "draft_id", "draft_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(ForeignKey("voucher_drafts.id", ondelete="CASCADE"), nullable=False)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    approver: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_tenant_time", "tenant_id", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    source_refs: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
