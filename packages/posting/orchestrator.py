"""Safe posting orchestration shared by authorized connectors and test doubles."""

from dataclasses import dataclass
from typing import Literal, Protocol

from connectors.contracts import AccountingConnector, PostedVoucher, PostingCommand, PostingSubmission


PostingResultStatus = Literal["BLOCKED", "FAILED", "UNKNOWN", "VERIFIED"]


@dataclass(frozen=True)
class PostingResult:
    status: PostingResultStatus
    external_reference: str | None = None
    message: str = ""


@dataclass(frozen=True)
class PostingAttemptRecord:
    idempotency_key: str
    status: Literal["SUBMITTING", "FAILED", "UNKNOWN", "VERIFIED"]
    external_reference: str | None = None
    message: str = ""


class PostingAttemptLedger(Protocol):
    """Durable unique-key ledger; reserve must be atomic across workers."""

    def get(self, command: PostingCommand) -> PostingAttemptRecord | None: ...

    def reserve(self, command: PostingCommand) -> bool: ...

    def update(self, attempt: PostingAttemptRecord, command: PostingCommand) -> None: ...


class PostingCoordinator:
    """Submit once, resolve uncertainty by lookup, then verify every line."""

    def __init__(self, connector: AccountingConnector, ledger: PostingAttemptLedger):
        self.connector = connector
        self.ledger = ledger

    def submit_approved(self, command: PostingCommand) -> PostingResult:
        """Submit an already approved immutable command through an authorized connector.

        The caller must enforce the voucher approval and actor permissions before calling.
        A durable attempt reservation is written before any remote request; a prior
        SUBMITTING/UNKNOWN record is only queried and can never trigger another write.
        """
        try:
            capabilities = self.connector.capabilities()
        except Exception:
            return PostingResult("BLOCKED", message="无法核验连接器能力，未发送写入请求")
        if not (
            capabilities.documented
            and capabilities.write_authorized
            and capabilities.read_authorized
            and capabilities.idempotency_supported
        ):
            return PostingResult("BLOCKED", message="连接器未同时具备文档、写入/读取授权和幂等能力")

        try:
            attempt = self.ledger.get(command)
        except Exception:
            return PostingResult("BLOCKED", message="无法读取本地写入尝试记录，未发送请求")
        if attempt is not None:
            return self._resolve_existing(attempt, command)

        try:
            reserved = self.ledger.reserve(command)
        except Exception:
            return PostingResult("BLOCKED", message="无法持久化写入尝试记录，未发送请求")
        if not reserved:
            return PostingResult("UNKNOWN", message="已有写入尝试正在处理；不会重复提交")

        try:
            existing = self.connector.find_by_idempotency_key(command.idempotency_key)
        except Exception:
            return self._store(command, PostingResult("UNKNOWN", message="幂等查询失败；不会发送写入请求"))
        if existing is not None:
            return self._store(command, self._resolve_submission(existing, command))

        try:
            submission = self.connector.submit(command)
        except Exception:
            submission = PostingSubmission(status="UNKNOWN", message="连接器调用结果不明")
        if submission.status == "FAILED":
            result = PostingResult("FAILED", message=submission.message or "连接器确认写入失败")
            return self._store(command, result)
        if submission.status == "UNKNOWN" or not submission.external_reference:
            try:
                resolved = self.connector.find_by_idempotency_key(command.idempotency_key)
            except Exception:
                resolved = None
            if resolved is None or resolved.status != "SUBMITTED" or not resolved.external_reference:
                return self._store(command, PostingResult(
                    "UNKNOWN", message=submission.message or "提交结果不明；未重发，请先人工核实目标账套"
                ))
            submission = resolved
        return self._store(command, self._verify_readback(submission.external_reference, command))

    def _resolve_existing(self, attempt: PostingAttemptRecord, command: PostingCommand) -> PostingResult:
        if attempt.status == "VERIFIED":
            return PostingResult("VERIFIED", attempt.external_reference, attempt.message)
        if attempt.status == "FAILED":
            return PostingResult("FAILED", attempt.external_reference, attempt.message)
        try:
            submission = self.connector.find_by_idempotency_key(command.idempotency_key)
        except Exception:
            submission = None
        if submission is None or submission.status != "SUBMITTED" or not submission.external_reference:
            return self._store(command, PostingResult(
                "UNKNOWN", attempt.external_reference,
                "此前写入结果仍不确定；已回查但没有可核验凭证，不会重发",
            ))
        return self._store(command, self._verify_readback(submission.external_reference, command))

    def _store(self, command: PostingCommand, result: PostingResult) -> PostingResult:
        if result.status != "BLOCKED":
            try:
                self.ledger.update(PostingAttemptRecord(
                    idempotency_key=command.idempotency_key,
                    status=result.status,
                    external_reference=result.external_reference,
                    message=result.message,
                ), command)
            except Exception:
                return PostingResult(
                    "UNKNOWN", result.external_reference,
                    "远端请求可能已执行，但本地结果保存失败；尝试记录已预留，禁止再次提交",
                )
        return result

    def _resolve_submission(self, submission: PostingSubmission, command: PostingCommand) -> PostingResult:
        if submission.status == "FAILED":
            return PostingResult("FAILED", message=submission.message or "幂等查询确认此前写入失败")
        if submission.status == "UNKNOWN" or not submission.external_reference:
            return PostingResult("UNKNOWN", message=submission.message or "幂等查询结果仍不确定；不会重发")
        return self._verify_readback(submission.external_reference, command)

    def _verify_readback(self, external_reference: str, command: PostingCommand) -> PostingResult:
        try:
            posted = self.connector.read_back(external_reference)
        except Exception:
            posted = None
        if posted is None:
            return PostingResult("UNKNOWN", external_reference, "已提交但无法取得回读结果；不会重发")
        if posted.external_reference != external_reference or not _matches(posted, command):
            return PostingResult("UNKNOWN", external_reference, "逐行回读与已批准凭证不一致，转人工核实；不会重发")
        if posted.debit_total != posted.credit_total:
            return PostingResult("UNKNOWN", external_reference, "ERP 回读借贷不平；不会标记已核实")
        return PostingResult("VERIFIED", external_reference, "ERP 凭证逐行回读一致")


def _matches(posted: PostedVoucher, command: PostingCommand) -> bool:
    debit_total = sum((line.debit for line in command.lines), start=0)
    credit_total = sum((line.credit for line in command.lines), start=0)
    return (
        posted.period == command.period
        and posted.debit_total == debit_total
        and posted.credit_total == credit_total
        and posted.lines == command.lines
    )
