from decimal import Decimal

from connectors.contracts import (
    ConnectorCapabilities,
    PostedVoucher,
    PostingCommand,
    PostingLine,
    PostingSubmission,
)
from posting.orchestrator import PostingAttemptRecord, PostingCoordinator


class FakeLedger:
    def __init__(self):
        self.records = {}

    def get(self, command):
        return self.records.get(command.idempotency_key)

    def reserve(self, command):
        if command.idempotency_key in self.records:
            return False
        self.records[command.idempotency_key] = PostingAttemptRecord(
            command.idempotency_key, "SUBMITTING"
        )
        return True

    def update(self, attempt, command):
        assert attempt.idempotency_key == command.idempotency_key
        self.records[attempt.idempotency_key] = attempt


class FakeConnector:
    def __init__(self, *, authorized=True, timeout=False, committed_before_timeout=False, altered_readback=False):
        self._capabilities = ConnectorCapabilities(
            documented=True,
            write_authorized=authorized,
            read_authorized=authorized,
            idempotency_supported=True,
        )
        self.timeout = timeout
        self.committed_before_timeout = committed_before_timeout
        self.altered_readback = altered_readback
        self.submissions = 0
        self.lookups = 0
        self.reference = None

    def capabilities(self):
        return self._capabilities

    def submit(self, command):
        self.submissions += 1
        if self.timeout:
            if self.committed_before_timeout:
                self.reference = "ERP-VOUCHER-001"
            return PostingSubmission(status="UNKNOWN", message="simulated network timeout")
        self.reference = "ERP-VOUCHER-001"
        return PostingSubmission(status="SUBMITTED", external_reference=self.reference)

    def find_by_idempotency_key(self, key):
        self.lookups += 1
        if self.reference:
            return PostingSubmission(status="SUBMITTED", external_reference=self.reference)
        return None

    def read_back(self, external_reference):
        if not self.reference or external_reference != self.reference:
            return None
        command = expected_command()
        lines = command.lines
        if self.altered_readback:
            lines = (PostingLine("1002", "银行存款", Decimal("0"), Decimal("45199.99"), "采购款"),)
        return PostedVoucher(
            external_reference=self.reference,
            period=command.period,
            debit_total=Decimal("45200.00"),
            credit_total=Decimal("45200.00"),
            lines=lines,
        )


def expected_command():
    return PostingCommand(
        tenant_id="tenant-1",
        account_set_id="account-set-1",
        voucher_id="voucher-1",
        version=2,
        period="2024-12",
        idempotency_key="tenant-1:account-set-1:voucher-1:v2",
        lines=(
            PostingLine("1405", "库存商品", Decimal("40000.00"), Decimal("0"), "采购入库"),
            PostingLine("222101", "进项税额", Decimal("5200.00"), Decimal("0"), "采购入库"),
            PostingLine("1002", "银行存款", Decimal("0"), Decimal("45200.00"), "采购款"),
        ),
    )


def test_authorized_submission_is_verified_only_after_exact_readback():
    connector = FakeConnector()

    result = PostingCoordinator(connector, FakeLedger()).submit_approved(expected_command())

    assert result.status == "VERIFIED"
    assert result.external_reference == "ERP-VOUCHER-001"
    assert connector.submissions == 1
    assert connector.lookups == 1


def test_timeout_stays_unknown_and_retry_never_submits_again():
    connector = FakeConnector(timeout=True)
    ledger = FakeLedger()
    coordinator = PostingCoordinator(connector, ledger)

    first = coordinator.submit_approved(expected_command())
    second = PostingCoordinator(connector, ledger).submit_approved(expected_command())

    assert first.status == "UNKNOWN"
    assert second.status == "UNKNOWN"
    assert connector.submissions == 1
    assert connector.lookups == 3


def test_lost_response_is_resolved_by_idempotency_lookup_and_readback():
    connector = FakeConnector(timeout=True, committed_before_timeout=True)

    result = PostingCoordinator(connector, FakeLedger()).submit_approved(expected_command())

    assert result.status == "VERIFIED"
    assert result.external_reference == "ERP-VOUCHER-001"
    assert connector.submissions == 1


def test_unreadable_or_unauthorized_connector_is_blocked_without_write():
    connector = FakeConnector(authorized=False)

    result = PostingCoordinator(connector, FakeLedger()).submit_approved(expected_command())

    assert result.status == "BLOCKED"
    assert connector.submissions == 0
    assert connector.lookups == 0


def test_mismatched_readback_is_never_verified():
    connector = FakeConnector(altered_readback=True)

    result = PostingCoordinator(connector, FakeLedger()).submit_approved(expected_command())

    assert result.status == "UNKNOWN"
    assert connector.submissions == 1
    assert "逐行回读" in result.message
