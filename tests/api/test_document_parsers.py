from io import BytesIO
from pathlib import Path

import pymupdf
from openpyxl import Workbook

from app.ocr import extract_pdf
from app.structured_files import extract_csv, extract_xlsx


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


def test_scanned_pdf_page_uses_tiny_ocr(tmp_path):
    repo=Path(__file__).resolve().parents[2]
    image=next((repo/"发票账单数据/zhangqing_raw_docs_20261025/01_采购入库付款").glob("*发票*45200.jpg"))
    pdf=pymupdf.open();page=pdf.new_page(width=595,height=842);page.insert_image(page.rect,filename=str(image))
    path=tmp_path/"scanned.pdf";pdf.save(path);pdf.close()
    result=extract_pdf(path)
    assert "45200.00" in result["full_text"].replace(",","")
    assert result["provider"]=="PyMuPDF+RapidOCR"
    assert result["lines"][0]["coordinate_space"]=="pixels"
