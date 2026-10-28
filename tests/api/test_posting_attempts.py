from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
import pytest

from app.db.base import Base
from app.db.models import AccountSet, AuditEvent, BusinessEvent, PostingAttempt, Tenant, VoucherDraft, VoucherDraftLine
from app.posting_attempts import SqlAlchemyPostingAttemptLedger, posting_command_for_approved_draft
from connectors.contracts import ConnectorCapabilities, PostingCommand, PostingSubmission
from posting.orchestrator import PostingAttemptRecord, PostingCoordinator


class TimedOutConnector:
    def __init__(self):
        self.submissions = 0
        self.lookups = 0

    def capabilities(self):
        return ConnectorCapabilities(True, True, True, True)

    def find_by_idempotency_key(self, key):
        self.lookups += 1
        return None

    def submit(self, command):
        self.submissions += 1
        return PostingSubmission(status="UNKNOWN", message="simulated timeout")

    def read_back(self, external_reference):
        return None


def test_timeout_is_persisted_and_new_session_never_repeats_remote_write():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = Tenant(name="Posting tenant")
        db.add(tenant)
        db.flush()
        account = AccountSet(tenant_id=tenant.id, name="Posting account", currency="CNY")
        db.add(account)
        db.flush()
        event = BusinessEvent(
            tenant_id=tenant.id, account_set_id=account.id, event_type="PURCHASE",
            status="MATCHED", created_by="accountant@example.test",
        )
        db.add(event)
        db.flush()
        draft = VoucherDraft(
            event_id=event.id, tenant_id=tenant.id, account_set_id=account.id,
            accounting_period="2024-12", accounting_date=date(2024, 12, 1),
            status="APPROVED", version=2, source_hash="a" * 64, policy={},
            debit_total=Decimal("45200.00"), credit_total=Decimal("45200.00"),
            created_by="accountant@example.test", approved_by="approver@example.test",
            approved_at=datetime.now(timezone.utc),
        )
        db.add(draft)
        db.flush()
        db.add_all([
            VoucherDraftLine(draft_id=draft.id, sequence=1, account_code="1405", account_name="库存商品",
                             debit=Decimal("40000.00"), credit=Decimal("0"), description="采购入库"),
            VoucherDraftLine(draft_id=draft.id, sequence=2, account_code="222101", account_name="进项税额",
                             debit=Decimal("5200.00"), credit=Decimal("0"), description="采购入库"),
            VoucherDraftLine(draft_id=draft.id, sequence=3, account_code="1002", account_name="银行存款",
                             debit=Decimal("0"), credit=Decimal("45200.00"), description="采购款"),
        ])
        db.flush()
        command = posting_command_for_approved_draft(draft)
        assert command.idempotency_key == f"{tenant.id}:{account.id}:{draft.id}:v2"
        ledger = SqlAlchemyPostingAttemptLedger(db, actor="accountant@example.test")
        connector = TimedOutConnector()

        first = PostingCoordinator(connector, ledger).submit_approved(command)

        assert first.status == "UNKNOWN"
        assert connector.submissions == 1
        assert not ledger.reserve(command)
        command_key = command.idempotency_key

    with Session(engine) as db:
        next_ledger = SqlAlchemyPostingAttemptLedger(db, actor="next-process@example.test")
        stored = next_ledger.get(command)
        second = PostingCoordinator(connector, next_ledger).submit_approved(command)
        assert stored is not None
        assert stored.status == "UNKNOWN"
        assert stored.message == "simulated timeout"
        assert second.status == "UNKNOWN"
        assert connector.submissions == 1
        with pytest.raises(ValueError, match="cannot transition"):
            next_ledger.update(PostingAttemptRecord(command_key, "FAILED"), command)
        assert db.scalar(select(PostingAttempt).where(PostingAttempt.idempotency_key == command_key)).request_digest
        assert {row.event_type for row in db.scalars(select(AuditEvent)).all()} == {
            "VOUCHER_POSTING_ATTEMPT_STARTED", "VOUCHER_POSTING_UNKNOWN",
        }
    engine.dispose()
