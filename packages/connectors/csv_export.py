"""Generic voucher-file export; a format preset is not an ERP integration."""

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from io import StringIO
import json
from typing import Protocol


@dataclass(frozen=True)
class VoucherExportLine:
    account_code: str
    account_name: str
    debit: Decimal
    credit: Decimal
    description: str
    auxiliary: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VoucherExportData:
    voucher_id: str
    version: int
    period: str
    lines: tuple[VoucherExportLine, ...]


@dataclass(frozen=True)
class VoucherCsvFormat:
    format_id: str = "generic-voucher-csv-v1"
    headers: tuple[str, ...] = (
        "凭证ID", "版本", "期间", "科目编码", "科目名称", "借方", "贷方", "说明", "辅助核算", "状态",
    )
    posting_status_label: str = "待人工入账"
    encoding: str = "utf-8-sig"


@dataclass(frozen=True)
class ExportArtifact:
    filename: str
    content: bytes
    media_type: str
    format_id: str
    posting_status: str = "EXPORTED_PENDING_MANUAL_POST"


class VoucherExportAdapter(Protocol):
    def export(self, voucher: VoucherExportData) -> ExportArtifact: ...


class GenericCsvVoucherExporter:
    def __init__(self, format_config: VoucherCsvFormat | None = None):
        self.format_config = format_config or VoucherCsvFormat()

    def export(self, voucher: VoucherExportData) -> ExportArtifact:
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(self.format_config.headers)
        for line in voucher.lines:
            writer.writerow((
                voucher.voucher_id,
                voucher.version,
                voucher.period,
                line.account_code,
                line.account_name,
                str(line.debit),
                str(line.credit),
                line.description,
                json.dumps(line.auxiliary, ensure_ascii=False, sort_keys=True),
                self.format_config.posting_status_label,
            ))
        filename = f"{voucher.voucher_id}-v{voucher.version}.csv"
        return ExportArtifact(
            filename=filename,
            content=output.getvalue().encode(self.format_config.encoding),
            media_type="text/csv; charset=utf-8",
            format_id=self.format_config.format_id,
        )
