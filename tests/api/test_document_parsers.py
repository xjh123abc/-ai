from io import BytesIO
from pathlib import Path
import struct
import zlib

import pymupdf
from openpyxl import Workbook
import pytest

from app.ocr import extract_image, extract_pdf, prepare_image_for_ocr, scale_bbox_to_original
from app.structured_files import extract_csv, extract_xlsx
from ingestion.candidates import field_candidates


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


def test_bank_credit_notice_extracts_type_and_confirmable_fields():
    def line(text, x, y, width=80):
        return {"text": text, "page": 1, "bbox": [x, y, x + width, y + 24]}

    lines = [
        line("中国工商银行进账单（收账通知）", 397, 0, 530),
        line("2019年6月2日", 563, 43, 196),
        line("第06326号", 1145, 43, 137),
        line("全称", 118, 91, 127),
        line("ABC 公司", 396, 98, 114),
        line("全称", 713, 92, 129),
        line("华兴有限责任公司", 961, 99, 217),
        line("出", 58, 111, 32), line("票", 57, 149, 34), line("人", 57, 191, 33),
        line("收", 652, 110, 36), line("款", 653, 149, 34), line("人", 654, 188, 36),
        line("账", 123, 150, 40), line("号", 196, 149, 45), line("12 - 654789231", 367, 153, 171),
        line("账", 717, 149, 43), line("号", 794, 149, 43), line("56 - 78963214782", 968, 154, 202),
        line("开户银行", 125, 203, 114), line("农业银行营业部", 358, 208, 190),
        line("开户银行", 721, 205, 114), line("工商银行如意支行", 963, 208, 214),
        line("人民币", 114, 266, 86), line("陆拾万元整", 222, 273, 218),
        line("仟佰拾万仟佰拾元角分", 940, 259, 339),
        line("票据种类", 98, 367, 115), line("转账支票", 395, 369, 113),
        line("票据张数", 98, 421, 115), line("壹", 431, 421, 43),
    ]
    full_text = " ".join(item["text"] for item in lines)

    fields = field_candidates(full_text, lines)

    assert fields["document_type_candidate"]["normalized_value"] == "BANK_CREDIT_NOTICE"
    assert fields["receipt_number"]["normalized_value"] == "06326"
    assert fields["settlement_date"]["normalized_value"] == "2019-06-02"
    assert fields["payer"]["normalized_value"] == "ABC 公司"
    assert fields["payee"]["normalized_value"] == "华兴有限责任公司"
    assert fields["payer_account_number"]["normalized_value"] == "12-654789231"
    assert fields["payee_account_number"]["normalized_value"] == "56-78963214782"
    assert fields["payer_bank_name"]["normalized_value"] == "农业银行营业部"
    assert fields["payee_bank_name"]["normalized_value"] == "工商银行如意支行"
    assert fields["gross_amount"]["normalized_value"] == "600000.00"
    assert fields["instrument_type"]["normalized_value"] == "转账支票"
    assert fields["instrument_count"]["normalized_value"] == "1"


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


def test_payment_proof_extracts_its_account_subject_number_and_signature_status():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "支付证明单", "bbox": [220, 5, 450, 30], "page": 1},
        {"text": "总号第 PAY-2024-12-007 号", "bbox": [480, 45, 650, 70], "page": 1},
        {"text": "科目：管理费用", "bbox": [10, 65, 180, 85], "page": 1},
        {"text": "主管人 李小静 会计 重玉 出纳 王维 记账 邱宇 证明人 丁森 经手人 马涛", "bbox": [15, 285, 650, 310], "page": 1},
    ]

    fields = field_candidates("支付证明单\n总号第 PAY-2024-12-007 号\n科目：管理费用\n主管人 李小静 会计 重玉 出纳 王维 记账 邱宇 证明人 丁森 经手人 马涛", lines)

    assert fields["expense_subject"]["normalized_value"] == "管理费用"
    assert fields["expense_subject"]["evidence_text"] == "科目：管理费用"
    assert fields["proof_number"]["normalized_value"] == "PAY-2024-12-007"
    assert fields["approved"]["normalized_value"] == "true"
    assert fields["approved"]["evidence_text"].startswith("主管人")


def test_payment_proof_extracts_quantity_from_its_own_table():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "支付证明单", "bbox": [220, 5, 450, 30], "page": 1},
        {"text": "数量", "bbox": [450, 100, 500, 120], "page": 1},
        {"text": "2", "bbox": [455, 125, 490, 145], "page": 1},
    ]

    fields = field_candidates("支付证明单 数量 2", lines)

    assert fields["quantity"]["normalized_value"] == "2"
    assert fields["quantity"]["evidence_text"] == "2"


def test_payment_proof_extracts_quantity_when_label_and_value_share_one_ocr_line():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "支付证明单", "bbox": [220, 5, 450, 30], "page": 1},
        {"text": "数量：2", "bbox": [450, 100, 520, 120], "page": 1},
    ]

    fields = field_candidates("支付证明单 数量：2", lines)

    assert fields["quantity"]["normalized_value"] == "2"
    assert fields["quantity"]["evidence_text"] == "数量：2"


def test_invoice_extracts_its_item_unit_quantity_and_unit_price():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "电子发票普通发票", "bbox": [300, 5, 520, 30], "page": 1},
        {"text": "项目名称", "bbox": [60, 100, 180, 120], "page": 1},
        {"text": "单位", "bbox": [360, 100, 410, 120], "page": 1},
        {"text": "数量", "bbox": [450, 100, 500, 120], "page": 1},
        {"text": "单价", "bbox": [550, 100, 600, 120], "page": 1},
        {"text": "*其他机械设备*教学专用", "bbox": [60, 125, 250, 145], "page": 1},
        {"text": "VR眼镜", "bbox": [60, 146, 140, 166], "page": 1},
        {"text": "副", "bbox": [370, 125, 400, 145], "page": 1},
        {"text": "100", "bbox": [455, 125, 490, 145], "page": 1},
        {"text": "400.00", "bbox": [550, 125, 610, 145], "page": 1},
    ]

    fields = field_candidates("电子发票普通发票 项目名称 单位 数量 单价", lines)

    assert fields["item"]["normalized_value"] == "*其他机械设备*教学专用 VR眼镜"
    assert fields["item"]["evidence_text"] == "*其他机械设备*教学专用 VR眼镜"
    assert fields["item"]["bbox"] == [60, 125, 250, 166]
    assert fields["item"]["confidence"] == 0.25
    assert fields["unit"]["normalized_value"] == "副"
    assert fields["quantity"]["normalized_value"] == "100"
    assert fields["unit_price"]["normalized_value"] == "400.00"


def test_invoice_maps_distinct_item_rows_when_each_row_has_its_own_cells():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税普通发票", "bbox": [300, 5, 500, 30], "page": 1},
        {"text": "项目名称", "bbox": [60, 100, 180, 120], "page": 1},
        {"text": "数量", "bbox": [450, 100, 500, 120], "page": 1},
        {"text": "单价", "bbox": [550, 100, 600, 120], "page": 1},
        {"text": "第一行商品", "bbox": [60, 125, 200, 145], "page": 1},
        {"text": "第二行商品", "bbox": [60, 155, 200, 175], "page": 1},
        {"text": "1", "bbox": [455, 125, 490, 145], "page": 1},
        {"text": "2", "bbox": [455, 155, 490, 175], "page": 1},
        {"text": "10.00", "bbox": [550, 125, 600, 145], "page": 1},
        {"text": "20.00", "bbox": [550, 155, 600, 175], "page": 1},
    ]

    fields = field_candidates("增值税普通发票 项目名称 数量 单价", lines)

    assert fields["item"]["normalized_value"] == "第一行商品；第二行商品"
    assert fields["item"]["evidence_text"] == "第一行商品；第二行商品"
    assert fields["item"]["bbox"] == [60, 125, 200, 175]


def test_expense_invoice_keeps_lodging_and_catering_items_for_voucher_context():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "电子发票普通发票", "page": 1, "bbox": [300, 5, 520, 30]},
        {"text": "项目名称", "page": 1, "bbox": [60, 100, 260, 120]},
        {"text": "单位", "page": 1, "bbox": [700, 100, 750, 120]},
        {"text": "数量", "page": 1, "bbox": [850, 100, 900, 120]},
        {"text": "单价", "page": 1, "bbox": [990, 100, 1040, 120]},
        {"text": "金额", "page": 1, "bbox": [1100, 100, 1150, 120]},
        {"text": "税率/征收率", "page": 1, "bbox": [1200, 100, 1290, 120]},
        {"text": "税额", "page": 1, "bbox": [1320, 100, 1370, 120]},
        {"text": "*住宿服务*住宿费", "page": 1, "bbox": [60, 130, 260, 150]},
        {"text": "1", "page": 1, "bbox": [860, 130, 890, 150]},
        {"text": "800.00", "page": 1, "bbox": [990, 130, 1040, 150]},
        {"text": "800.00", "page": 1, "bbox": [1100, 130, 1150, 150]},
        {"text": "6%", "page": 1, "bbox": [1220, 130, 1260, 150]},
        {"text": "48.00", "page": 1, "bbox": [1320, 130, 1370, 150]},
        {"text": "*餐饮服务*餐饮费", "page": 1, "bbox": [60, 160, 260, 180]},
        {"text": "1", "page": 1, "bbox": [860, 160, 890, 180]},
        {"text": "3396.23", "page": 1, "bbox": [990, 160, 1040, 180]},
        {"text": "3396.23", "page": 1, "bbox": [1100, 160, 1150, 180]},
        {"text": "6%", "page": 1, "bbox": [1220, 160, 1260, 180]},
        {"text": "203.77", "page": 1, "bbox": [1320, 160, 1370, 180]},
    ]

    fields = field_candidates(
        "电子发票普通发票 项目名称 单位 数量 单价 金额 税率/征收率 税额 "
        "*住宿服务*住宿费 1 800.00 800.00 6% 48.00 "
        "*餐饮服务*餐饮费 1 3396.23 3396.23 6% 203.77",
        lines,
    )

    assert fields["item"]["normalized_value"] == "*住宿服务*住宿费；*餐饮服务*餐饮费"
    assert fields["item"]["evidence_text"] == "*住宿服务*住宿费；*餐饮服务*餐饮费"
    assert fields["item"]["bbox"] == [60, 130, 260, 180]


def test_invoice_does_not_merge_next_item_row_when_its_quantity_was_missed():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税普通发票", "bbox": [300, 5, 500, 30], "page": 1},
        {"text": "项目名称", "bbox": [60, 100, 180, 120], "page": 1},
        {"text": "数量", "bbox": [450, 100, 500, 120], "page": 1},
        {"text": "单价", "bbox": [550, 100, 600, 120], "page": 1},
        {"text": "第一行商品", "bbox": [60, 125, 200, 145], "page": 1},
        {"text": "第二行商品", "bbox": [60, 155, 200, 175], "page": 1},
        {"text": "1", "bbox": [455, 125, 490, 145], "page": 1},
        {"text": "10.00", "bbox": [550, 125, 600, 145], "page": 1},
        {"text": "20.00", "bbox": [550, 155, 600, 175], "page": 1},
    ]

    fields = field_candidates("增值税普通发票 项目名称 数量 单价", lines)

    assert fields["quantity"]["normalized_value"] == "1"
    assert fields["item"]["normalized_value"] is None


def test_invoice_does_not_merge_item_rows_when_all_second_row_cells_were_missed():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税普通发票", "bbox": [300, 5, 500, 30], "page": 1},
        {"text": "项目名称", "bbox": [60, 100, 180, 120], "page": 1},
        {"text": "数量", "bbox": [450, 100, 500, 120], "page": 1},
        {"text": "第一行商品", "bbox": [60, 125, 200, 145], "page": 1},
        {"text": "第二行商品", "bbox": [60, 155, 200, 175], "page": 1},
        {"text": "1", "bbox": [455, 125, 490, 145], "page": 1},
    ]

    fields = field_candidates("增值税普通发票 项目名称 数量", lines)

    assert fields["quantity"]["normalized_value"] == "1"
    assert fields["item"]["normalized_value"] is None


def test_invoice_does_not_merge_two_categorized_item_rows_when_second_row_cells_were_missed():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税普通发票", "bbox": [300, 5, 500, 30], "page": 1},
        {"text": "项目名称", "bbox": [60, 100, 180, 120], "page": 1},
        {"text": "数量", "bbox": [450, 100, 500, 120], "page": 1},
        {"text": "*办公用品*电脑", "bbox": [60, 125, 200, 145], "page": 1},
        {"text": "*办公用品*鼠标", "bbox": [60, 155, 200, 175], "page": 1},
        {"text": "1", "bbox": [455, 125, 490, 145], "page": 1},
    ]

    fields = field_candidates("增值税普通发票 项目名称 数量", lines)

    assert fields["item"]["normalized_value"] is None


def test_invoice_does_not_merge_when_second_item_category_mark_was_missed_by_ocr():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税普通发票", "bbox": [300, 5, 500, 30], "page": 1},
        {"text": "项目名称", "bbox": [60, 100, 180, 120], "page": 1},
        {"text": "数量", "bbox": [450, 100, 500, 120], "page": 1},
        {"text": "*办公用品*电脑", "bbox": [60, 125, 200, 145], "page": 1},
        {"text": "鼠标", "bbox": [60, 155, 120, 175], "page": 1},
        {"text": "1", "bbox": [455, 125, 490, 145], "page": 1},
    ]

    fields = field_candidates("增值税普通发票 项目名称 数量", lines)

    assert fields["item"]["normalized_value"] is None


def test_manual_document_type_reuses_ocr_lines_to_match_its_own_fields():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "科目", "bbox": [60, 100, 110, 120], "page": 1},
        {"text": "管理费用", "bbox": [140, 100, 220, 120], "page": 1},
    ]

    fields = field_candidates("科目 管理费用", lines, document_type="PAYMENT_PROOF")

    assert fields["document_type_candidate"]["normalized_value"] is None
    assert fields["expense_subject"]["normalized_value"] == "管理费用"
    assert fields["expense_subject"]["evidence_text"] == "科目 管理费用"


def test_goods_receipt_extracts_department_source_number_and_row_unit_price():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "入库单", "bbox": [370, 0, 500, 32], "page": 1},
        {"text": "入库数量：100", "bbox": [20, 50, 120, 70], "page": 1},
        {"text": "入库单位及部门", "bbox": [60, 80, 175, 105], "page": 1},
        {"text": "行政部门", "bbox": [180, 80, 250, 105], "page": 1},
        {"text": "发票号码或生产单号", "bbox": [260, 80, 410, 105], "page": 1},
        {"text": "25165954", "bbox": [420, 80, 490, 105], "page": 1},
        {"text": "名称及规格", "bbox": [165, 111, 238, 131], "page": 1},
        {"text": "单位", "bbox": [280, 111, 325, 131], "page": 1},
        {"text": "单价", "bbox": [500, 111, 555, 131], "page": 1},
        {"text": "教学专用VR眼镜", "bbox": [151, 136, 253, 156], "page": 1},
        {"text": "副", "bbox": [285, 136, 315, 156], "page": 1},
        {"text": "452.00", "bbox": [500, 136, 555, 156], "page": 1},
    ]

    fields = field_candidates("入库单 行政部门 25165954 名称及规格 单位 单价", lines)

    assert fields["department"]["normalized_value"] == "行政部门"
    assert fields["source_reference_number"]["normalized_value"] == "25165954"
    assert fields["unit"]["normalized_value"] == "副"
    assert fields["unit_price"]["normalized_value"] == "452.00"
    assert fields["received"]["normalized_value"] is None


def test_goods_receipt_only_marks_received_when_the_document_says_so():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "入库单", "bbox": [370, 0, 500, 32], "page": 1},
        {"text": "已收货", "bbox": [60, 80, 120, 105], "page": 1},
    ]

    fields = field_candidates("入库单 已收货", lines)

    assert fields["received"]["normalized_value"] == "true"
    assert fields["received"]["evidence_text"] == "已收货"


def test_goods_receipt_leaves_conflicting_status_text_for_review():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "入库单", "bbox": [370, 0, 500, 32], "page": 1},
        {"text": "待收货", "bbox": [60, 80, 120, 105], "page": 1},
        {"text": "已收货", "bbox": [130, 80, 190, 105], "page": 1},
    ]

    fields = field_candidates("入库单 待收货 已收货", lines)

    assert fields["received"]["normalized_value"] is None


def test_bank_receipt_extracts_its_transaction_summary_and_type():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "网上银行电子回单", "bbox": [100, 10, 300, 30], "page": 1},
        {"text": "用途", "bbox": [55, 140, 110, 160], "page": 1},
        {"text": "采购软件配套商品", "bbox": [145, 140, 300, 160], "page": 1},
        {"text": "摘要", "bbox": [350, 140, 390, 160], "page": 1},
        {"text": "业务种类", "bbox": [350, 140, 430, 160], "page": 1},
        {"text": "转账", "bbox": [440, 140, 500, 160], "page": 1},
    ]

    fields = field_candidates("网上银行电子回单 用途 采购软件配套商品 摘要 业务种类 转账", lines)

    assert fields["transaction_summary"]["normalized_value"] == "采购软件配套商品"
    assert fields["transaction_type"]["normalized_value"] == "转账"


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

    assert fields["settled"]["normalized_value"] == "SETTLED"
    assert fields["settled"]["evidence_text"] == "交易成功"
    assert fields["settled"]["status"] == "NEEDS_REVIEW"


def test_bank_receipt_without_explicit_status_leaves_settled_empty():
    from ingestion.candidates import field_candidates

    fields = field_candidates("网上银行电子回单 交易流水号 202412011234")

    assert fields["settled"]["normalized_value"] == "UNKNOWN"


def test_bank_receipt_does_not_mark_success_when_receiving_side_failed():
    from ingestion.candidates import field_candidates

    lines = [{"text": "支付成功但到账失败", "bbox": [100, 100, 240, 120], "page": 1}]
    fields = field_candidates("网上银行电子回单 支付成功但到账失败", lines)

    assert fields["settled"]["normalized_value"] == "UNKNOWN"


def test_bookkeeping_posted_text_is_not_bank_settlement_evidence():
    from ingestion.candidates import field_candidates

    lines = [{"text": "已入账", "bbox": [100, 100, 160, 120], "page": 1}]
    fields = field_candidates("网上银行电子回单 已入账", lines)

    assert fields["settled"]["normalized_value"] == "UNKNOWN"


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


def test_invoice_classifier_separates_ordinary_and_vat_special_invoices():
    from ingestion.candidates import field_candidates

    ordinary = field_candidates("电子发票普通发票")
    special_with_misread_title = field_candidates("票(增慎税专用发票)发票号码：244234139350671047")
    special = field_candidates("增值税专用发票")

    assert ordinary["document_type_candidate"]["normalized_value"] == "VAT_INVOICE"
    assert special_with_misread_title["document_type_candidate"]["normalized_value"] == "VAT_SPECIAL_INVOICE"
    assert special["document_type_candidate"]["normalized_value"] == "VAT_SPECIAL_INVOICE"


def test_invoice_type_uses_the_top_heading_before_a_lower_caption():
    from ingestion.candidates import field_candidates

    title = {"text": "普通发票", "page": 1, "bbox": [300, 40, 500, 65]}
    caption = {"text": "图2-2 增值税专用发票（第一联）", "page": 1, "bbox": [300, 850, 650, 875]}
    fields = field_candidates("图2-2 增值税专用发票（第一联） 普通发票", [caption, title])

    assert fields["document_type_candidate"]["normalized_value"] == "VAT_INVOICE"
    assert fields["document_type_candidate"]["bbox"] == title["bbox"]


def test_vat_special_invoice_heading_overrides_a_lower_ordinary_invoice_mention():
    from ingestion.candidates import field_candidates

    title = {"text": "增值税专用发票", "page": 1, "bbox": [300, 40, 520, 65]}
    lower_text = {"text": "图2-2 普通发票（第一联）", "page": 1, "bbox": [300, 850, 650, 875]}
    fields = field_candidates("增值税专用发票 图2-2 普通发票（第一联）", [lower_text, title])

    assert fields["document_type_candidate"]["normalized_value"] == "VAT_SPECIAL_INVOICE"
    assert fields["document_type_candidate"]["bbox"] == title["bbox"]
    assert fields["document_type_candidate"]["evidence_text"] == "增值税专用发票"


def test_vat_special_invoice_maps_name_unit_quantity_and_spaced_unit_price():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税专用发票", "page": 1, "bbox": [300, 5, 520, 30]},
        {"text": "货物及应税劳务名称", "page": 1, "bbox": [60, 100, 270, 120]},
        {"text": "单位", "page": 1, "bbox": [760, 100, 810, 120]},
        {"text": "数量", "page": 1, "bbox": [860, 100, 910, 120]},
        {"text": "单价", "page": 1, "bbox": [980, 100, 1030, 120]},
        {"text": "*FUD", "page": 1, "bbox": [90, 130, 160, 150]},
        {"text": "件", "page": 1, "bbox": [770, 130, 800, 150]},
        {"text": "100", "page": 1, "bbox": [870, 130, 900, 150]},
        {"text": "3 200.00", "page": 1, "bbox": [980, 130, 1050, 150]},
    ]

    fields = field_candidates("增值税专用发票 货物及应税劳务名称 单位 数量 单价", lines)

    assert fields["item"]["normalized_value"] == "*FUD"
    assert fields["unit"]["normalized_value"] == "件"
    assert fields["quantity"]["normalized_value"] == "100"
    assert fields["unit_price"]["normalized_value"] == "3200.00"


def test_textbook_vat_special_invoice_maps_each_table_column_and_prefers_its_title():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税专用发票", "page": 1, "bbox": [300, 5, 520, 30]},
        {"text": "23404011158", "page": 1, "bbox": [30, 40, 150, 60]},
        {"text": "No.06214751", "page": 1, "bbox": [500, 40, 620, 60]},
        {"text": "货物及应税劳务名称", "page": 1, "bbox": [60, 300, 270, 320]},
        {"text": "单位", "page": 1, "bbox": [760, 300, 810, 320]},
        {"text": "数量", "page": 1, "bbox": [860, 300, 910, 320]},
        {"text": "单价", "page": 1, "bbox": [980, 300, 1030, 320]},
        {"text": "金额", "page": 1, "bbox": [1080, 300, 1160, 320]},
        {"text": "税率", "page": 1, "bbox": [1180, 300, 1240, 320]},
        {"text": "税额", "page": 1, "bbox": [1270, 300, 1350, 320]},
        {"text": "*FUD", "page": 1, "bbox": [90, 330, 160, 350]},
        {"text": "件", "page": 1, "bbox": [770, 330, 800, 350]},
        {"text": "100", "page": 1, "bbox": [870, 330, 900, 350]},
        {"text": "3 200.00", "page": 1, "bbox": [980, 330, 1050, 350]},
        {"text": "320000.00", "page": 1, "bbox": [1080, 330, 1160, 350]},
        {"text": "16%", "page": 1, "bbox": [1190, 330, 1230, 350]},
        {"text": "51200.00", "page": 1, "bbox": [1270, 330, 1350, 350]},
        {"text": "价税合计(小写) ￥371200.00", "page": 1, "bbox": [668, 454, 802, 475]},
        {"text": "图2-2 增值税专用发票（第一联）", "page": 1, "bbox": [300, 850, 650, 875]},
    ]

    fields = field_candidates(
        "增值税专用发票 23404011158 No.06214751 货物及应税劳务名称 单位 数量 单价 金额 税率 税额 "
        "*FUD 件 100 3 200.00 320000.00 16% 51200.00 价税合计(小写) ￥371200.00 "
        "图2-2 增值税专用发票（第一联）",
        lines,
    )

    assert fields["document_type_candidate"]["normalized_value"] == "VAT_SPECIAL_INVOICE"
    assert fields["document_type_candidate"]["evidence_text"] == "增值税专用发票"
    assert fields["invoice_code"]["normalized_value"] == "23404011158"
    assert fields["invoice_number"]["normalized_value"] == "06214751"
    assert fields["item"]["normalized_value"] == "*FUD"
    assert fields["unit"]["normalized_value"] == "件"
    assert fields["quantity"]["normalized_value"] == "100"
    assert fields["unit_price"]["normalized_value"] == "3200.00"
    assert fields["net_amount"]["normalized_value"] == "320000.00"
    assert fields["tax_rate"]["normalized_value"] == "16%"
    assert fields["tax_amount"]["normalized_value"] == "51200.00"
    assert fields["gross_amount"]["normalized_value"] == "371200.00"


def test_textbook_vat_special_invoice_maps_buyer_and_split_small_total():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "增值税专用发票", "page": 1, "bbox": [300, 5, 520, 30]},
        {"text": "购货单位", "page": 1, "bbox": [40, 130, 90, 240]},
        {"text": "名称：青岛长城公司", "page": 1, "bbox": [100, 135, 290, 155]},
        {"text": "纳税人识别号：382415675361234", "page": 1, "bbox": [100, 160, 360, 180]},
        {"text": "销货单位", "page": 1, "bbox": [40, 390, 90, 500]},
        {"text": "名称：华兴有限责任公司", "page": 1, "bbox": [100, 400, 310, 420]},
        {"text": "纳税人识别号：91150102MA0NB78Q9M", "page": 1, "bbox": [100, 425, 390, 445]},
        {"text": "价税合计（大写）", "page": 1, "bbox": [60, 455, 220, 475]},
        {"text": "叁拾柒万壹仟贰佰元整", "page": 1, "bbox": [225, 455, 420, 475]},
        {"text": "（小写）", "page": 1, "bbox": [1025, 455, 1090, 475]},
        {"text": "371 200.00", "page": 1, "bbox": [1100, 455, 1190, 475]},
    ]

    fields = field_candidates(
        "增值税专用发票 购货单位 名称：青岛长城公司 纳税人识别号：382415675361234 "
        "销货单位 名称：华兴有限责任公司 纳税人识别号：91150102MA0NB78Q9M "
        "价税合计（大写）叁拾柒万壹仟贰佰元整（小写）371 200.00",
        lines,
    )

    assert fields["buyer"]["normalized_value"] == "青岛长城公司"
    assert fields["buyer"]["evidence_text"] == "名称：青岛长城公司"
    assert fields["seller"]["normalized_value"] == "华兴有限责任公司"
    assert fields["gross_amount"]["normalized_value"] == "371200.00"
    assert fields["gross_amount"]["evidence_text"] == "371 200.00"


def test_vat_special_invoice_matches_its_additional_keywords():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "用发票", "page": 1, "bbox": [300, 5, 420, 30]},
        {"text": "1100162130", "page": 1, "bbox": [30, 40, 150, 60]},
        {"text": "No. 06214751", "page": 1, "bbox": [500, 40, 620, 60]},
        {"text": "开票日期：2019年06月04日", "page": 1, "bbox": [500, 70, 700, 90]},
        {"text": "称：", "page": 1, "bbox": [100, 100, 140, 120]},
        {"text": "青岛长城公司", "page": 1, "bbox": [170, 100, 280, 120]},
        {"text": "纳税人识别号：", "page": 1, "bbox": [100, 130, 220, 150]},
        {"text": "382415675361234", "page": 1, "bbox": [230, 130, 360, 150]},
        {"text": "称：", "page": 1, "bbox": [100, 220, 140, 240]},
        {"text": "华兴有限责任公司", "page": 1, "bbox": [170, 220, 310, 240]},
        {"text": "纳税人识别号：", "page": 1, "bbox": [100, 250, 220, 270]},
        {"text": "91150102MA0NB78Q9M", "page": 1, "bbox": [230, 250, 390, 270]},
        {"text": "货物或应税劳务、服务名称", "page": 1, "bbox": [100, 300, 420, 320]},
        {"text": "佳能相机", "page": 1, "bbox": [80, 330, 190, 350]},
    ]
    fields = field_candidates("用发票 1100162130 No. 06214751 开票日期：2019年06月04日 购买方 销售方 纳税人识别号 货物或应税劳务、服务名称", lines)

    assert fields["document_type_candidate"]["normalized_value"] == "VAT_SPECIAL_INVOICE"
    assert fields["invoice_code"]["normalized_value"] == "1100162130"
    assert fields["invoice_number"]["normalized_value"] == "06214751"
    assert fields["buyer"]["normalized_value"] == "青岛长城公司"
    assert fields["buyer_tax_id"]["normalized_value"] == "382415675361234"
    assert fields["seller"]["normalized_value"] == "华兴有限责任公司"
    assert fields["seller_tax_id"]["normalized_value"] == "91150102MA0NB78Q9M"
    assert fields["item"]["normalized_value"] == "佳能相机"


def test_vat_special_invoice_matches_side_by_side_parties_when_title_is_unreadable():
    from ingestion.candidates import field_candidates

    lines = [
        {"text": "1100162130", "page": 1, "bbox": [30, 40, 150, 60]},
        {"text": "发票号码", "page": 1, "bbox": [500, 40, 580, 60]},
        {"text": "06214751", "page": 1, "bbox": [590, 40, 680, 60]},
        {"text": "名称：", "page": 1, "bbox": [500, 100, 550, 120]},
        {"text": "销售公司", "page": 1, "bbox": [560, 100, 650, 120]},
        {"text": "名称：", "page": 1, "bbox": [100, 100, 150, 120]},
        {"text": "购买公司", "page": 1, "bbox": [160, 100, 250, 120]},
        {"text": "纳税人识别号：", "page": 1, "bbox": [500, 130, 620, 150]},
        {"text": "91150102MA0NB78Q9M", "page": 1, "bbox": [630, 130, 790, 150]},
        {"text": "纳税人识别号：", "page": 1, "bbox": [100, 130, 220, 150]},
        {"text": "382415675361234", "page": 1, "bbox": [230, 130, 360, 150]},
    ]

    fields = field_candidates("1100162130 发票号码：06214751 名称 纳税人识别号", lines)

    assert fields["document_type_candidate"]["normalized_value"] == "VAT_SPECIAL_INVOICE"
    assert fields["invoice_number"]["normalized_value"] == "06214751"
    assert fields["buyer"]["normalized_value"] == "购买公司"
    assert fields["seller"]["normalized_value"] == "销售公司"
    assert fields["buyer_tax_id"]["normalized_value"] == "382415675361234"
    assert fields["seller_tax_id"]["normalized_value"] == "91150102MA0NB78Q9M"


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
