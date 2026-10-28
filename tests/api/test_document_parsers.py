from io import BytesIO
from pathlib import Path
import struct
import zlib

import pymupdf
from openpyxl import Workbook
import pytest

from app.ocr import extract_image, extract_pdf, prepare_image_for_ocr, scale_bbox_to_original
from app.structured_files import extract_csv, extract_xlsx


def local_purchase_invoice(repo: Path) -> Path:
    sample_dir = repo / "发票账单数据/zhangqing_raw_docs_20261025/01_采购入库付款"
    image = next(sample_dir.glob("*发票*45200.jpg"), None)
    if image is None:
        pytest.skip("需要本地解密/授权的 45,200 元 OCR 样例图")
    return image


def test_csv_and_xlsx_are_preserved_as_row_evidence():
    csv_result=extract_csv("日期,金额\n2024-12-01,45200.00\n".encode())
    assert csv_result["document_type"]=="STRUCTURED_DATA"
    assert csv_result["fields"]["row_count"]["normalized_value"]==1
    assert csv_result["lines"][0]["cells"]=={"日期":"2024-12-01","金额":"45200.00"}

    workbook=Workbook()
    sheet=workbook.active
    sheet.title="流水"
    sheet.append(["日期","金额"])
    sheet.append(["2024-12-01",45200])
    buffer=BytesIO();workbook.save(buffer)
    xlsx_result=extract_xlsx(buffer.getvalue())
    assert xlsx_result["fields"]["row_count"]["normalized_value"]==1
    assert xlsx_result["lines"][0]["sheet"]=="流水"


def test_pdf_text_layer_is_extracted_with_page_evidence(tmp_path):
    pdf=pymupdf.open()
    page=pdf.new_page()
    page.insert_text((72,72),"Invoice No 12345678 Total 45200.00")
    path=tmp_path/"text.pdf";pdf.save(path);pdf.close()
    result=extract_pdf(path)
    assert "45200.00" in result["full_text"]
    assert result["provider"]=="PyMuPDF"
    assert result["lines"][0]["coordinate_space"]=="pdf_points"


def test_pdf_page_count_is_bounded(tmp_path):
    pdf=pymupdf.open()
    for _ in range(21):pdf.new_page()
    path=tmp_path/"too-many-pages.pdf";pdf.save(path);pdf.close()
    try:
        extract_pdf(path)
    except ValueError as exc:
        assert "页数" in str(exc)
    else:
        raise AssertionError("超过上限的 PDF 应拒绝解析")


def test_large_image_is_resized_for_ocr_and_bbox_returns_to_original_coordinates(tmp_path):
    from PIL import Image

    path = tmp_path / "large.png"
    Image.new("RGB", (2400, 1200), "white").save(path)
    prepared, scale_x, scale_y = prepare_image_for_ocr(path, max_edge=1200)

    assert isinstance(prepared, bytes)
    with Image.open(BytesIO(prepared)) as resized:
        assert resized.size == (1200, 600)
    assert scale_x == 2
    assert scale_y == 2
    assert scale_bbox_to_original([[10, 20], [30, 40]], scale_x, scale_y) == [[20, 40], [60, 80]]


def test_exif_rotated_image_is_normalized_before_ocr_and_bbox_uses_display_orientation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PIL import Image
    import app.ocr as ocr

    path = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (2400, 1200), "white")
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)

    class FakeEngine:
        def __call__(self, payload, use_cls):
            with Image.open(BytesIO(payload)) as raster:
                assert raster.size == (900, 1800)
                assert raster.getexif().get(274, 1) == 1
            return SimpleNamespace(
                txts=["价税合计 100.00"],
                boxes=[[[10, 20], [30, 20], [30, 40], [10, 40]]],
                scores=[0.99],
            )

    monkeypatch.setattr(ocr, "engine", lambda: FakeEngine())
    result = ocr.extract_image(path)

    assert result["lines"][0]["bbox"] == [
        [13.333, 26.667], [40.0, 26.667], [40.0, 53.333], [13.333, 53.333],
    ]


def test_image_pixel_limit_is_checked_from_header_before_decode(tmp_path):
    from app.ocr import OcrImageTooLargeError, prepare_image_for_ocr

    width = height = 5000
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    def png_chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    png_header = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", b"")
        + png_chunk(b"IEND", b"")
    )
    path = tmp_path / "pixel-bomb-header.png"
    path.write_bytes(png_header)

    with pytest.raises(OcrImageTooLargeError, match="像素总量超过 OCR 安全上限"):
        prepare_image_for_ocr(path)


def test_scanned_pdf_pixel_limit_is_checked_before_rendering(monkeypatch):
    from types import SimpleNamespace
    import app.ocr as ocr

    class Page:
        rect = SimpleNamespace(width=2400, height=2400)

        def get_text(self, kind):
            return ""

        def get_pixmap(self, **kwargs):
            raise AssertionError("oversized page must be rejected before rendering")

    class Document:
        needs_pass = False
        page_count = 1

        def __iter__(self):
            return iter([Page()])

    with pytest.raises(ocr.OcrImageTooLargeError, match="PDF 扫描页像素总量超过 OCR 安全上限"):
        ocr._extract_pdf_document(Document(), 20)


def test_scanned_pdf_page_uses_bounded_image_and_restores_bbox_coordinates(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import numpy as np
    from PIL import Image
    import app.ocr as ocr

    image_buffer = BytesIO()
    Image.new("RGB", (2400, 1200), "white").save(image_buffer, format="PNG")
    pdf = pymupdf.open()
    page = pdf.new_page(width=1200, height=600)
    page.insert_image(page.rect, stream=image_buffer.getvalue())
    path = tmp_path / "large-scan.pdf"
    pdf.save(path)
    pdf.close()

    class FakeEngine:
        def __call__(self, image, use_cls):
            with Image.open(BytesIO(image)) as raster:
                assert max(raster.size) == 1800
            return SimpleNamespace(
                txts=["价税合计 45200.00"],
                boxes=[np.array([[10, 20], [30, 20], [30, 40], [10, 40]])],
                scores=[0.99],
            )

    monkeypatch.setattr(ocr, "engine", lambda: FakeEngine())
    result = ocr.extract_pdf(path)

    assert result["lines"][0]["page"] == 1
    assert result["lines"][0]["coordinate_space"] == "pixels"
    assert result["lines"][0]["bbox"] == [[13.333, 26.667], [40.0, 26.667], [40.0, 53.333], [13.333, 53.333]]


def test_scanned_pdf_page_uses_tiny_ocr(tmp_path):
    repo=Path(__file__).resolve().parents[2]
    image=local_purchase_invoice(repo)
    pdf=pymupdf.open();page=pdf.new_page(width=595,height=842);page.insert_image(page.rect,filename=str(image))
    path=tmp_path/"scanned.pdf";pdf.save(path);pdf.close()
    result=extract_pdf(path)
    assert "45200.00" in result["full_text"].replace(",","")
    assert result["provider"]=="PyMuPDF+RapidOCR"
    assert result["lines"][0]["coordinate_space"]=="pixels"


def test_purchase_invoice_ocr_candidates_match_reviewed_fixture_and_keep_evidence():
    import json
    from ingestion.candidates import field_candidates

    repo = Path(__file__).resolve().parents[2]
    fixture = json.loads((repo / "tests/fixtures/expected/purchase_45200.json").read_text())
    image_path = local_purchase_invoice(repo)
    result = extract_image(image_path)
    candidates = field_candidates(result["full_text"], result["lines"])

    for field, expected_value in fixture["documents"][0]["fields"].items():
        assert candidates[field]["normalized_value"] == expected_value
        assert candidates[field]["page"] == 1
        assert candidates[field]["bbox"]
        assert candidates[field]["evidence_text"]


def test_gross_amount_uses_the_total_label_line_instead_of_a_duplicate_number():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "（小写）￥45200.00", "bbox": [600, 400, 800, 420], "page": 1, "coordinate_space": "pixels"},
        {"text": "45200.00", "bbox": [600, 100, 800, 120], "page": 2, "coordinate_space": "pixels"},
    ]
    fields = field_candidates("（小写）￥45200.00\n45200.00", lines)

    assert fields["gross_amount"]["normalized_value"] == "45200.00"
    assert fields["gross_amount"]["page"] == 1
    assert fields["gross_amount"]["bbox"] == [600, 400, 800, 420]


def test_payment_proof_extracts_labeled_purpose_and_total_with_evidence():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "支付证明单", "bbox": [100, 10, 200, 30], "page": 1},
        {"text": "事由或品名", "bbox": [50, 100, 150, 120], "page": 1},
        {"text": "报销业务招待费", "bbox": [10, 130, 100, 150], "page": 1},
        {"text": "共计金额", "bbox": [20, 220, 80, 240], "page": 1},
        {"text": "￥4448.00", "bbox": [200, 220, 260, 240], "page": 1},
    ]

    fields = field_candidates("支付证明单", lines)

    assert fields["purpose"]["normalized_value"] == "报销业务招待费"
    assert fields["purpose"]["status"] == "NEEDS_REVIEW"
    assert fields["purpose"]["bbox"] == [10, 130, 100, 150]
    assert fields["gross_amount"]["normalized_value"] == "4448.00"
    assert fields["gross_amount"]["bbox"] == [200, 220, 260, 240]


def test_payment_proof_conflicting_totals_remain_empty():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "支付证明单", "bbox": [100, 10, 200, 30], "page": 1},
        {"text": "共计金额", "bbox": [20, 220, 80, 240], "page": 1},
        {"text": "￥4448.00", "bbox": [200, 220, 260, 240], "page": 1},
        {"text": "￥4488.00", "bbox": [300, 220, 360, 240], "page": 1},
    ]

    fields = field_candidates("支付证明单", lines)

    assert fields["gross_amount"]["normalized_value"] is None


def test_payment_proof_date_keeps_combined_evidence_location():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "支付证明单", "bbox": [100, 10, 200, 30], "page": 1},
        {"text": "2024", "bbox": [170, 65, 210, 85], "page": 1, "coordinate_space": "pixels"},
        {"text": "年12月07", "bbox": [215, 64, 376, 85], "page": 1, "coordinate_space": "pixels"},
        {"text": "日", "bbox": [368, 66, 390, 85], "page": 1, "coordinate_space": "pixels"},
    ]

    fields = field_candidates("支付证明单 2024 年12月07 日", lines)

    assert fields["payment_date"]["normalized_value"] == "2024-12-07"
    assert fields["payment_date"]["page"] == 1
    assert fields["payment_date"]["bbox"] == [170, 64, 390, 85]
    assert fields["payment_date"]["evidence_text"] == "2024年12月07日"
    assert fields["issue_date"]["normalized_value"] is None
    assert fields["issue_date"]["raw_value"] is None


def test_payment_proof_date_with_two_dates_in_one_ocr_box_stays_empty():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "支付证明单", "bbox": [100, 10, 200, 30], "page": 1},
        {"text": "2024年12月07日 / 2024年12月08日", "bbox": [170, 65, 390, 85], "page": 1},
    ]

    fields = field_candidates("支付证明单", lines)

    assert fields["payment_date"]["normalized_value"] is None


def test_bank_receipt_uses_labeled_settlement_date_and_keeps_print_date_separate():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "网上银行电子回单", "bbox": [100, 10, 300, 30], "page": 1},
        {"text": "交易时间戳2024-12-01-18.16.29", "bbox": [40, 200, 300, 220], "page": 1},
        {"text": "记账日期2024-12-01", "bbox": [40, 220, 260, 240], "page": 1, "coordinate_space": "pixels"},
        {"text": "打印日期：2024-12-02", "bbox": [40, 250, 260, 270], "page": 1},
    ]

    fields = field_candidates("网上银行电子回单", lines)

    assert fields["settlement_date"]["normalized_value"] == "2024-12-01"
    assert fields["settlement_date"]["evidence_text"] == "记账日期2024-12-01"
    assert fields["settlement_date"]["bbox"] == [40, 220, 260, 240]
    assert fields["issue_date"]["raw_value"] is None


def test_bank_receipt_requires_explicit_success_text_for_settled_candidate():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "网上银行电子回单", "bbox": [100, 10, 300, 30], "page": 1},
        {"text": "交易流水号", "bbox": [40, 200, 150, 220], "page": 1},
        {"text": "交易成功", "bbox": [300, 200, 380, 220], "page": 1},
    ]

    fields = field_candidates("网上银行电子回单", lines)

    assert fields["settled"]["normalized_value"] == "true"
    assert fields["settled"]["evidence_text"] == "交易成功"
    assert fields["settled"]["status"] == "NEEDS_REVIEW"


def test_bank_receipt_without_explicit_status_leaves_settled_empty():
    from ingestion.candidates import field_candidates

    fields = field_candidates("网上银行电子回单 交易流水号 202412011234")

    assert fields["settled"]["normalized_value"] is None


def test_bank_receipt_does_not_mark_success_when_receiving_side_failed():
    from ingestion.candidates import field_candidates

    lines = [{"text": "支付成功但到账失败", "bbox": [100, 100, 240, 120], "page": 1}]
    fields = field_candidates("网上银行电子回单 支付成功但到账失败", lines)

    assert fields["settled"]["normalized_value"] is None


def test_bookkeeping_posted_text_is_not_bank_settlement_evidence():
    from ingestion.candidates import field_candidates

    lines = [{"text": "已入账", "bbox": [100, 100, 160, 120], "page": 1}]
    fields = field_candidates("网上银行电子回单 已入账", lines)

    assert fields["settled"]["normalized_value"] is None


def test_goods_receipt_extracts_number_date_and_item_from_labeled_rows():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "入库单", "bbox": [370, 0, 500, 32], "page": 1},
        {"text": "NO56784001", "bbox": [686, 4, 781, 31], "page": 1},
        {"text": "入库", "bbox": [674, 64, 714, 91], "page": 1},
        {"text": "日期", "bbox": [674, 83, 714, 111], "page": 1},
        {"text": "2024/12/01", "bbox": [727, 77, 802, 97], "page": 1},
        {"text": "名称及规格", "bbox": [165, 111, 238, 131], "page": 1},
        {"text": "教学专用VR眼镜", "bbox": [151, 136, 253, 156], "page": 1},
    ]

    fields = field_candidates("入库单", lines)

    assert fields["receipt_number"]["normalized_value"] == "NO56784001"
    assert fields["receipt_date"]["normalized_value"] == "2024-12-01"
    assert fields["item"]["normalized_value"] == "教学专用VR眼镜"
    assert fields["issue_date"]["raw_value"] is None


def test_goods_receipt_accepts_labeled_date_and_value_in_one_ocr_box():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "入库单", "bbox": [370, 0, 500, 32], "page": 1},
        {"text": "入库日期2024/12/01", "bbox": [674, 77, 802, 97], "page": 1},
    ]

    fields = field_candidates("入库单", lines)

    assert fields["receipt_date"]["normalized_value"] == "2024-12-01"
    assert fields["receipt_date"]["evidence_text"] == "入库日期2024/12/01"


def test_goods_receipt_with_two_dates_in_separate_value_box_stays_empty():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "入库单", "bbox": [370, 0, 500, 32], "page": 1},
        {"text": "入库", "bbox": [674, 64, 714, 91], "page": 1},
        {"text": "日期", "bbox": [674, 83, 714, 111], "page": 1},
        {"text": "2024/12/01 2024/12/02", "bbox": [727, 77, 802, 97], "page": 1},
    ]

    fields = field_candidates("入库单", lines)

    assert fields["receipt_date"]["normalized_value"] is None


def test_acceptance_report_extracts_unique_labeled_date_and_checked_result():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "验收报告", "bbox": [100, 10, 250, 35], "page": 1},
        {"text": "验收日期", "bbox": [100, 100, 180, 120], "page": 1},
        {"text": "2024.12.11", "bbox": [300, 100, 400, 120], "page": 1},
        {"text": "■通过验收", "bbox": [100, 200, 220, 225], "page": 1},
        {"text": "□未通过验收，需整改", "bbox": [100, 240, 300, 265], "page": 1},
    ]

    fields = field_candidates("验收报告", lines)

    assert fields["acceptance_date"]["normalized_value"] == "2024-12-11"
    assert fields["acceptance_date"]["bbox"] == [300, 100, 400, 120]
    assert fields["acceptance_result"]["normalized_value"] == "通过验收"
    assert fields["acceptance_result"]["bbox"] == [100, 200, 220, 225]
    assert fields["issue_date"]["raw_value"] is None


def test_acceptance_report_conflicting_checked_outcomes_remain_empty():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "验收报告", "bbox": [100, 10, 250, 35], "page": 1},
        {"text": "■通过验收", "bbox": [100, 200, 220, 225], "page": 1},
        {"text": "■未通过验收", "bbox": [100, 240, 300, 265], "page": 1},
    ]

    fields = field_candidates("验收报告", lines)

    assert fields["acceptance_result"]["normalized_value"] is None


def test_acceptance_report_does_not_map_a_review_date_to_acceptance_date():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "验收报告", "bbox": [100, 10, 250, 35], "page": 1},
        {"text": "审核日期", "bbox": [100, 100, 180, 120], "page": 1},
        {"text": "2024.12.11", "bbox": [300, 100, 400, 120], "page": 1},
    ]

    fields = field_candidates("验收报告", lines)

    assert fields["acceptance_date"]["normalized_value"] is None


def test_contract_number_requires_an_explicit_contract_number_label():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "购销合同", "bbox": [100, 10, 220, 35], "page": 1},
        {"text": "合同编号：25326952", "bbox": [559, 58, 668, 72], "page": 1},
    ]

    fields = field_candidates("购销合同", lines)

    assert fields["contract_number"]["normalized_value"] == "25326952"
    assert fields["contract_number"]["status"] == "NEEDS_REVIEW"
    assert fields["contract_number"]["evidence_text"] == "合同编号：25326952"


def test_contract_number_without_explicit_label_stays_empty():
    from ingestion.candidates import field_candidates

    fields = field_candidates("购销合同 25326952")

    assert fields["contract_number"]["normalized_value"] is None


def test_contract_number_with_two_values_in_same_ocr_box_stays_empty():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "购销合同", "bbox": [100, 10, 220, 35], "page": 1},
        {"text": "合同编号：25326952 / 合同编号：25326953", "bbox": [400, 58, 700, 72], "page": 1},
    ]

    fields = field_candidates("购销合同", lines)

    assert fields["contract_number"]["normalized_value"] is None


def test_ambiguous_party_column_is_left_unassigned():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "购买方信息", "bbox": [100, 100, 120, 120], "page": 1, "coordinate_space": "pixels"},
        {"text": "销售方信息", "bbox": [200, 100, 220, 120], "page": 1, "coordinate_space": "pixels"},
        {"text": "名称：Unclear Company", "bbox": [145, 100, 175, 120], "page": 1, "coordinate_space": "pixels"},
    ]
    fields = field_candidates("购买方信息 销售方信息 名称：Unclear Company", lines)

    assert fields["buyer"]["normalized_value"] is None
    assert fields["seller"]["normalized_value"] is None


def test_conflicting_total_labels_and_unlabeled_numbers_are_not_guessed():
    from ingestion.candidates import field_candidates

    conflicting_lines = [
        {"text": "小写￥45200.00", "bbox": [10, 10, 120, 25], "page": 1, "coordinate_space": "pixels"},
        {"text": "价税合计￥45201.00", "bbox": [10, 10, 120, 25], "page": 2, "coordinate_space": "pixels"},
    ]
    conflicting = field_candidates("小写￥45200.00 价税合计￥45201.00", conflicting_lines)
    unlabeled = field_candidates("发票代码 1100162130 2024-12-01 45200.00", [
        {"text": "发票代码 1100162130", "page": 1, "bbox": [10, 10, 100, 25]},
        {"text": "45200.00", "page": 1, "bbox": [10, 30, 100, 45]},
    ])

    assert conflicting["gross_amount"]["normalized_value"] is None
    assert conflicting["gross_amount"]["page"] is None
    assert unlabeled["invoice_number"]["normalized_value"] is None
    assert unlabeled["gross_amount"]["normalized_value"] is None


def test_conflicting_net_and_tax_candidates_across_pages_are_left_empty():
    from ingestion.candidates import field_candidates

    labeled_lines = [
        {"text": "不含税金额 100.00", "page": 1, "bbox": [10, 10, 140, 25]},
        {"text": "税额 13.00", "page": 1, "bbox": [10, 30, 100, 45]},
        {"text": "不含税金额 200.00", "page": 2, "bbox": [10, 10, 140, 25]},
        {"text": "税额 26.00", "page": 2, "bbox": [10, 30, 100, 45]},
    ]
    labeled = field_candidates("不含税金额100.00 税额13.00 不含税金额200.00 税额26.00", labeled_lines)

    column_lines = [
        {"text": "金额", "page": 1, "bbox": [100, 100, 150, 120]},
        {"text": "税额", "page": 1, "bbox": [200, 100, 250, 120]},
        {"text": "100.00", "page": 1, "bbox": [100, 130, 150, 150]},
        {"text": "13.00", "page": 1, "bbox": [200, 130, 250, 150]},
        {"text": "价税合计113.00", "page": 1, "bbox": [100, 180, 250, 200]},
        {"text": "金额", "page": 2, "bbox": [100, 100, 150, 120]},
        {"text": "税额", "page": 2, "bbox": [200, 100, 250, 120]},
        {"text": "200.00", "page": 2, "bbox": [100, 130, 150, 150]},
        {"text": "26.00", "page": 2, "bbox": [200, 130, 250, 150]},
        {"text": "价税合计226.00", "page": 2, "bbox": [100, 180, 250, 200]},
    ]
    columnar = field_candidates("金额 税额 100.00 13.00 金额 税额 200.00 26.00", column_lines)

    assert labeled["net_amount"]["normalized_value"] is None
    assert labeled["tax_amount"]["normalized_value"] is None
    assert columnar["net_amount"]["normalized_value"] is None
    assert columnar["tax_amount"]["normalized_value"] is None


def test_invoice_total_row_recovers_net_and_tax_after_split_ocr_headers():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "普通发票", "page": 1, "bbox": [10, 5, 80, 20]},
        {"text": "金额", "page": 1, "bbox": [650, 250, 710, 275]},
        {"text": "税额", "page": 1, "bbox": [890, 250, 940, 275]},
        {"text": "800.00", "page": 1, "bbox": [650, 280, 710, 305]},
        {"text": "48.00", "page": 1, "bbox": [890, 280, 940, 305]},
        {"text": "3396.23", "page": 1, "bbox": [650, 310, 710, 335]},
        {"text": "203.77", "page": 1, "bbox": [890, 310, 940, 335]},
        {"text": "合", "page": 1, "bbox": [90, 425, 130, 450]},
        {"text": "计", "page": 1, "bbox": [160, 425, 200, 450]},
        {"text": "￥4196.23", "page": 1, "bbox": [640, 425, 700, 450]},
        {"text": "￥251.77", "page": 1, "bbox": [890, 425, 940, 450]},
        {"text": "价税合计（大写）", "page": 1, "bbox": [90, 460, 210, 485]},
    ]

    fields = field_candidates("普通发票 金额 税额 合 计 价税合计（大写）", lines)

    assert fields["net_amount"]["normalized_value"] == "4196.23"
    assert fields["net_amount"]["bbox"] == [640, 425, 700, 450]
    assert fields["tax_amount"]["normalized_value"] == "251.77"
    assert fields["tax_amount"]["bbox"] == [890, 425, 940, 450]


def test_toll_receipt_accepts_one_explicit_integer_currency_amount():
    from ingestion.candidates import field_candidates

    fields = field_candidates("浙江省公路车辆通行费票据", [
        {"text": "浙江省公路车辆通行费票据", "page": 1, "bbox": [10, 10, 200, 30]},
        {"text": "￥20", "page": 1, "bbox": [200, 100, 260, 130]},
    ])

    assert fields["gross_amount"]["normalized_value"] == "20.00"
    assert fields["gross_amount"]["evidence_text"] == "￥20"

    malformed = field_candidates("浙江省公路车辆通行费票据", [
        {"text": "浙江省公路车辆通行费票据", "page": 1, "bbox": [10, 10, 200, 30]},
        {"text": "￥20.123", "page": 1, "bbox": [200, 100, 280, 130]},
    ])

    assert malformed["gross_amount"]["normalized_value"] is None


def test_contract_buyer_and_seller_are_taken_from_explicit_party_labels():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "购销合同", "page": 1, "bbox": [10, 5, 80, 20]},
        {"text": "卖方：南昌市杰盛科技有限公司", "page": 1, "bbox": [20, 30, 250, 50]},
        {"text": "买方：暨中大学", "page": 1, "bbox": [20, 55, 150, 75]},
    ]

    fields = field_candidates("购销合同\n卖方：南昌市杰盛科技有限公司\n买方：暨中大学", lines)

    assert fields["seller"]["normalized_value"] == "南昌市杰盛科技有限公司"
    assert fields["seller"]["evidence_text"] == "卖方：南昌市杰盛科技有限公司"
    assert fields["buyer"]["normalized_value"] == "暨中大学"
    assert fields["buyer"]["evidence_text"] == "买方：暨中大学"


def test_contract_order_and_receipt_quantity_candidates_keep_their_labeled_evidence():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "合同采购数量：100 件", "page": 1, "bbox": [20, 30, 180, 50]},
        {"text": "实际入库数量：90 件", "page": 1, "bbox": [25, 70, 185, 90]},
    ]

    fields = field_candidates("合同采购数量：100 件\n实际入库数量：90 件", lines)

    assert fields["ordered_quantity"]["normalized_value"] == "100"
    assert fields["ordered_quantity"]["evidence_text"] == "合同采购数量：100 件"
    assert fields["ordered_quantity"]["bbox"] == [20, 30, 180, 50]
    assert fields["quantity"]["normalized_value"] == "90"
    assert fields["quantity"]["evidence_text"] == "实际入库数量：90 件"
    assert fields["quantity"]["bbox"] == [25, 70, 185, 90]


def test_receipt_quantity_uses_a_unique_cell_below_a_separate_table_header():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "入库单", "page": 1, "bbox": [20, 10, 80, 30]},
        {"text": "数量", "page": 1, "bbox": [360, 108, 398, 134]},
        {"text": "100.0", "page": 1, "bbox": [358, 136, 401, 158]},
    ]

    fields = field_candidates("入库单\n数量\n100.0", lines)

    assert fields["quantity"]["normalized_value"] == "100"
    assert fields["quantity"]["evidence_text"] == "100.0"
    assert fields["quantity"]["bbox"] == [358, 136, 401, 158]


def test_quantity_column_with_multiple_rows_is_not_silently_summed():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "购销合同", "page": 1, "bbox": [20, 10, 100, 30]},
        {"text": "数量", "page": 1, "bbox": [250, 40, 290, 60]},
        {"text": "1", "page": 1, "bbox": [260, 65, 275, 85]},
        {"text": "1", "page": 1, "bbox": [260, 292, 275, 312]},
    ]

    fields = field_candidates("购销合同\n数量\n1\n1", lines)

    assert fields["ordered_quantity"]["normalized_value"] is None


def test_document_type_classifier_returns_an_evidence_backed_unconfirmed_candidate():
    from ingestion.candidates import field_candidates

    lines = [{"text": "中国农业银行网上银行电子回单", "page": 1,
              "bbox": [10, 20, 300, 55], "confidence": 0.91}]
    fields = field_candidates("中国农业银行网上银行电子回单", lines)
    candidate = fields["document_type_candidate"]

    assert candidate["normalized_value"] == "BANK_RECEIPT"
    assert candidate["status"] == "NEEDS_REVIEW"
    assert candidate["confidence"] == 0.5
    assert candidate["evidence_confidence"] == 0.91
    assert candidate["bbox"] == [10, 20, 300, 55]


def test_document_type_classifier_leaves_conflicting_signals_unclassified():
    from ingestion.candidates import field_candidates

    fields = field_candidates("入库单 网上银行电子回单")

    assert fields["document_type_candidate"]["normalized_value"] is None


def test_bank_receipt_extracts_payer_payee_and_amount_from_labeled_rows():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "网上银行电子回单", "page": 1, "bbox": [187, 14, 509, 46]},
        {"text": "付款人", "page": 1, "bbox": [23, 98, 48, 162]},
        {"text": "户名", "page": 1, "bbox": [66, 90, 127, 117]},
        {"text": "Buyer Ltd", "page": 1, "bbox": [145, 95, 314, 113]},
        {"text": "收款人", "page": 1, "bbox": [357, 99, 382, 161]},
        {"text": "户名", "page": 1, "bbox": [402, 90, 463, 117]},
        {"text": "Seller Ltd", "page": 1, "bbox": [480, 95, 618, 113]},
        {"text": "金额", "page": 1, "bbox": [52, 180, 112, 207]},
        {"text": "￥45200.00", "page": 1, "bbox": [582, 184, 663, 204]},
    ]

    fields = field_candidates("网上银行电子回单\n付款人\n户名\nBuyer Ltd\n收款人\n户名\nSeller Ltd\n金额\n￥45200.00", lines)

    assert fields["payer"]["normalized_value"] == "Buyer Ltd"
    assert fields["payer"]["evidence_text"] == "Buyer Ltd"
    assert fields["payer"]["bbox"] == [145, 95, 314, 113]
    assert fields["payee"]["normalized_value"] == "Seller Ltd"
    assert fields["payee"]["evidence_text"] == "Seller Ltd"
    assert fields["payee"]["bbox"] == [480, 95, 618, 113]
    assert fields["gross_amount"]["normalized_value"] == "45200.00"
    assert fields["gross_amount"]["evidence_text"] == "￥45200.00"
    assert fields["gross_amount"]["bbox"] == [582, 184, 663, 204]


def test_bank_receipt_leaves_party_and_amount_candidates_empty_when_ambiguous():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "电子回单号码", "page": 1, "bbox": [20, 10, 130, 30]},
        {"text": "付款人", "page": 1, "bbox": [23, 98, 48, 162]},
        {"text": "户名", "page": 1, "bbox": [66, 90, 127, 117]},
        {"text": "Possible A", "page": 1, "bbox": [145, 95, 230, 113]},
        {"text": "Possible B", "page": 1, "bbox": [145, 95, 230, 113]},
        {"text": "金额", "page": 1, "bbox": [52, 180, 112, 207]},
        {"text": "￥45200.00", "page": 1, "bbox": [582, 184, 663, 204]},
        {"text": "￥55200.00", "page": 1, "bbox": [582, 184, 663, 204]},
    ]

    fields = field_candidates("电子回单号码\n付款人\n户名\nPossible A\nPossible B\n金额\n￥45200.00\n￥55200.00", lines)

    assert fields["payer"]["normalized_value"] is None
    assert fields["gross_amount"]["normalized_value"] is None


def test_bank_receipt_does_not_reuse_one_account_name_for_both_parties():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "电子回单号码", "page": 1, "bbox": [20, 10, 130, 30]},
        {"text": "付款人", "page": 1, "bbox": [23, 98, 48, 162]},
        {"text": "收款人", "page": 1, "bbox": [357, 99, 382, 161]},
        {"text": "户名", "page": 1, "bbox": [66, 90, 127, 117]},
        {"text": "南昌市杰盛科技有限公司", "page": 1, "bbox": [145, 95, 314, 113]},
    ]

    fields = field_candidates("电子回单号码\n付款人\n收款人\n户名\n南昌市杰盛科技有限公司", lines)

    assert fields["payer"]["normalized_value"] is None
    assert fields["payee"]["normalized_value"] is None
