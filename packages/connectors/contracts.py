"""Contracts for ERP integrations; implementations require explicit authorization."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol


@dataclass(frozen=True)
class ConnectorCapabilities:
    documented: bool
    write_authorized: bool
    read_authorized: bool
    idempotency_supported: bool


@dataclass(frozen=True)
class PostingLine:
    account_code: str
    account_name: str
    debit: Decimal
    credit: Decimal
    description: str


@dataclass(frozen=True)
class PostingCommand:
    tenant_id: str
    account_set_id: str
    voucher_id: str
    version: int
    period: str
    idempotency_key: str
    lines: tuple[PostingLine, ...]


@dataclass(frozen=True)
class PostingSubmission:
    status: Literal["SUBMITTED", "UNKNOWN", "FAILED"]
    external_reference: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class PostedVoucher:
    external_reference: str
    period: str
    debit_total: Decimal
    credit_total: Decimal
    lines: tuple[PostingLine, ...]


class AccountingConnector(Protocol):
    """Authorized server-side ERP write/read seam. No connector is configured yet."""

    def capabilities(self) -> ConnectorCapabilities: ...

    def submit(self, command: PostingCommand) -> PostingSubmission: ...

    def find_by_idempotency_key(self, key: str) -> PostingSubmission | None: ...

    def read_back(self, external_reference: str) -> PostedVoucher | None: ...
