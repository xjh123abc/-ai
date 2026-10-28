"""Durable SQL ledger for the posting coordinator; no connector is configured here."""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, PostingAttempt
from connectors.contracts import PostingCommand, PostingLine
from posting.orchestrator import PostingAttemptRecord


class SqlAlchemyPostingAttemptLedger:
    def __init__(self, db: Session, *, actor: str):
        self.db = db
        self.actor = actor

    def get(self, command: PostingCommand) -> PostingAttemptRecord | None:
        attempt = self.db.scalar(select(PostingAttempt).where(
            PostingAttempt.idempotency_key == command.idempotency_key,
            PostingAttempt.tenant_id == command.tenant_id,
            PostingAttempt.account_set_id == command.account_set_id,
            PostingAttempt.voucher_draft_id == command.voucher_id,
            PostingAttempt.voucher_version == command.version,
        ))
        return _record(attempt) if attempt else None

    def reserve(self, command: PostingCommand) -> bool:
        payload = _command_payload(command)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        attempt = PostingAttempt(
            tenant_id=command.tenant_id,
            account_set_id=command.account_set_id,
            voucher_draft_id=command.voucher_id,
            voucher_version=command.version,
            idempotency_key=command.idempotency_key,
            request_digest=hashlib.sha256(encoded).hexdigest(),
            request_payload=payload,
            status="SUBMITTING",
            created_by=self.actor,
        )
        self.db.add(attempt)
        try:
            self.db.flush()
            self.db.add(AuditEvent(
                tenant_id=command.tenant_id,
                actor=self.actor,
                event_type="VOUCHER_POSTING_ATTEMPT_STARTED",
                entity_ref=command.voucher_id,
                source_refs=[command.voucher_id],
                after_hash=attempt.request_digest,
            ))
            self.db.commit()
            return True
        except IntegrityError:
            self.db.rollback()
            if self.get(command) is not None:
                return False
            raise

    def update(self, record: PostingAttemptRecord, command: PostingCommand) -> None:
        attempt = self.db.scalar(select(PostingAttempt).where(
            PostingAttempt.idempotency_key == record.idempotency_key,
            PostingAttempt.tenant_id == command.tenant_id,
            PostingAttempt.account_set_id == command.account_set_id,
            PostingAttempt.voucher_draft_id == command.voucher_id,
            PostingAttempt.voucher_version == command.version,
        ))
        if attempt is None:
            raise LookupError("posting attempt reservation is missing")
        allowed_transitions = {
            "SUBMITTING": {"SUBMITTING", "UNKNOWN", "FAILED", "VERIFIED"},
            "UNKNOWN": {"UNKNOWN", "VERIFIED"},
            "FAILED": {"FAILED"},
            "VERIFIED": {"VERIFIED"},
        }
        if record.status not in allowed_transitions.get(attempt.status, set()):
            raise ValueError(f"posting attempt cannot transition from {attempt.status} to {record.status}")
        previous = {"status": attempt.status, "external_reference": attempt.external_reference}
        attempt.status = record.status
        attempt.external_reference = record.external_reference
        attempt.message = record.message or None
        self.db.add(AuditEvent(
            tenant_id=attempt.tenant_id,
            actor=self.actor,
            event_type=f"VOUCHER_POSTING_{record.status}",
            entity_ref=attempt.voucher_draft_id,
            source_refs=[attempt.voucher_draft_id],
            before_hash=_digest(previous),
            after_hash=_digest({"status": record.status, "external_reference": record.external_reference}),
        ))
        self.db.commit()


def posting_command_for_approved_draft(draft) -> PostingCommand:
    """Freeze the approved DB version into the connector command contract."""
    if draft.status != "APPROVED" or not draft.approved_by or not draft.approved_at:
        raise ValueError("只有已审批且有审批记录的凭证版本可以写入")
    lines = tuple(PostingLine(
        account_code=line.account_code,
        account_name=line.account_name,
        debit=line.debit,
        credit=line.credit,
        description=line.description,
    ) for line in draft.lines)
    debit_total = sum((line.debit for line in lines), start=0)
    credit_total = sum((line.credit for line in lines), start=0)
    if not lines or debit_total != credit_total or debit_total != draft.debit_total or credit_total != draft.credit_total:
        raise ValueError("凭证分录与已审批借贷合计不一致，禁止写入")
    return PostingCommand(
        tenant_id=draft.tenant_id,
        account_set_id=draft.account_set_id,
        voucher_id=draft.id,
        version=draft.version,
        period=draft.accounting_period,
        idempotency_key=f"{draft.tenant_id}:{draft.account_set_id}:{draft.id}:v{draft.version}",
        lines=lines,
    )


def _record(attempt: PostingAttempt) -> PostingAttemptRecord:
    return PostingAttemptRecord(
        idempotency_key=attempt.idempotency_key,
        status=attempt.status,
        external_reference=attempt.external_reference,
        message=attempt.message or "",
    )


def _command_payload(command: PostingCommand) -> dict:
    return {
        "tenant_id": command.tenant_id,
        "account_set_id": command.account_set_id,
        "voucher_id": command.voucher_id,
        "version": command.version,
        "period": command.period,
        "idempotency_key": command.idempotency_key,
        "lines": [{
            "account_code": line.account_code,
            "account_name": line.account_name,
            "debit": str(line.debit),
            "credit": str(line.credit),
            "description": line.description,
        } for line in command.lines],
    }


def _digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
