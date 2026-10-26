from functools import lru_cache
from pathlib import Path

from rapidocr import RapidOCR
from rapidocr.utils.typings import ModelType

from app.core.config import settings

@lru_cache(maxsize=1)
def engine() -> RapidOCR:
    # Model weights are held outside the repository and provisioned explicitly.
    params = {"Global.use_cls": False, "Global.model_root_dir": settings.ocr_model_root,
              "EngineConfig.onnxruntime.intra_op_num_threads": 4,
              "EngineConfig.onnxruntime.inter_op_num_threads": 1,
              "Det.model_type": ModelType.TINY, "Rec.model_type": ModelType.TINY}
    return RapidOCR(params=params)

def extract(path: Path) -> dict:
    return extract_image(path)

def _format_result(result, page: int = 1) -> tuple[list[str], list[dict]]:
    lines, text = [], []
    for item in (getattr(result, "txts", None) or []):
        text.append(str(item))
    boxes = getattr(result, "boxes", None)
    scores = getattr(result, "scores", None)
    boxes = [] if boxes is None else boxes
    scores = [] if scores is None else scores
    for i, value in enumerate(text):
        lines.append({"text": value, "confidence": float(scores[i]) if i < len(scores) else None,
                      "bbox": boxes[i].tolist() if i < len(boxes) and hasattr(boxes[i], "tolist") else (boxes[i] if i < len(boxes) else None),
                      "page": page, "coordinate_space": "pixels", "source": "OCR"})
    return text, lines

def extract_image(image: Path | bytes, page: int = 1) -> dict:
    text, lines = _format_result(engine()(image, use_cls=False), page)
    return {"full_text": "\n".join(text), "lines": lines,
            "provider": "RapidOCR", "model_version": "PP-OCRv6-Tiny/CPU"}

def extract_pdf(path: Path, max_pages: int = 20) -> dict:
    import pymupdf

    document = pymupdf.open(path)
    try:
        return _extract_pdf_document(document,max_pages)
    finally:
        document.close()

def _extract_pdf_document(document,max_pages: int) -> dict:
    if document.needs_pass:
        raise ValueError("加密 PDF 暂不支持")
    if document.page_count < 1 or document.page_count > max_pages:
        raise ValueError(f"PDF 页数必须在 1 到 {max_pages} 页之间")
    all_text: list[str] = []
    all_lines: list[dict] = []
    used_ocr = False
    for page_number, page in enumerate(document, start=1):
        rect = page.rect
        if rect.width > 3000 or rect.height > 3000:
            raise ValueError("PDF 页面尺寸超过安全处理上限")
        page_text = page.get_text("text").strip()
        if len(page_text) >= 10:
            all_text.append(page_text)
            for block in page.get_text("blocks"):
                if len(block) >= 7 and block[6] == 0 and str(block[4]).strip():
                    all_lines.append({"text":str(block[4]).strip(),"confidence":None,
                        "bbox":[float(v) for v in block[:4]],"page":page_number,
                        "coordinate_space":"pdf_points","source":"PDF_TEXT_LAYER"})
        else:
            used_ocr = True
            pixmap = page.get_pixmap(dpi=144, alpha=False)
            text, lines = _format_result(engine()(pixmap.tobytes("png"), use_cls=False), page_number)
            all_text.append("\n".join(text))
            all_lines.extend(lines)
    return {"full_text":"\n".join(all_text),"lines":all_lines,
            "provider":"PyMuPDF+RapidOCR" if used_ocr else "PyMuPDF",
            "model_version":"PP-OCRv6-Tiny/CPU + PyMuPDF-1.28.2" if used_ocr else "PDF-TEXT-1"}
