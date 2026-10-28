import csv
from decimal import Decimal
from io import StringIO

from connectors.csv_export import GenericCsvVoucherExporter, VoucherExportData, VoucherExportLine


def test_generic_csv_adapter_formats_pending_manual_post_without_claiming_erp_success():
    artifact = GenericCsvVoucherExporter().export(VoucherExportData(
        voucher_id="voucher-fixture",
        version=2,
        period="2024-12",
        lines=(VoucherExportLine(
            account_code="1002",
            account_name="银行存款",
            debit=Decimal("0.00"),
            credit=Decimal("45200.00"),
            description="测试采购",
            auxiliary={"department": "Operations"},
        ),),
    ))
    rows = list(csv.reader(StringIO(artifact.content.decode("utf-8-sig"))))

    assert artifact.format_id == "generic-voucher-csv-v1"
    assert artifact.posting_status == "EXPORTED_PENDING_MANUAL_POST"
    assert artifact.filename == "voucher-fixture-v2.csv"
    assert rows[0] == ["凭证ID", "版本", "期间", "科目编码", "科目名称", "借方", "贷方", "说明", "辅助核算", "状态"]
    assert rows[1] == [
        "voucher-fixture", "2", "2024-12", "1002", "银行存款",
        "0.00", "45200.00", "测试采购", '{"department": "Operations"}', "待人工入账",
    ]
    assert rows[1][-1] == "待人工入账"
    assert "已核实" not in rows[1][-1]
