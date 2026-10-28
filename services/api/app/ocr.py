from functools import lru_cache
from io import BytesIO
from math import ceil
from pathlib import Path

from PIL import Image, ImageOps
from rapidocr import RapidOCR
from rapidocr.utils.typings import ModelType

from app.core.config import settings

MAX_OCR_IMAGE_EDGE = 1800
MAX_OCR_IMAGE_PIXELS = 20_000_000


class OcrImageTooLargeError(ValueError):
    """Raised before decoding an image that exceeds the OCR memory budget."""

@lru_cache(maxsize=1)
def engine() -> RapidOCR:
    # Model weights are held outside the repository and provisioned explicitly.
    params = {"Global.use_cls": False, "Global.model_root_dir": settings.ocr_model_root,
              "EngineConfig.onnxruntime.intra_op_num_threads": 2,
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
    ocr_image, scale_x, scale_y = prepare_image_for_ocr(image)
    text, lines = _format_result(engine()(ocr_image, use_cls=False), page)
    if scale_x != 1 or scale_y != 1:
        for line in lines:
            line["bbox"] = scale_bbox_to_original(line.get("bbox"), scale_x, scale_y)
    return {"full_text": "\n".join(text), "lines": lines,
            "provider": "RapidOCR", "model_version": f"PP-OCRv6-Tiny/CPU; max-edge={MAX_OCR_IMAGE_EDGE}px; threads=2/1"}


def prepare_image_for_ocr(image: Path | bytes, max_edge: int = MAX_OCR_IMAGE_EDGE) -> tuple[Path | bytes, float, float]:
    """Shrink oversized OCR inputs while returning scale factors to original pixel coordinates."""
    source = BytesIO(image) if isinstance(image, bytes) else image
    with Image.open(source) as opened:
        original_width, original_height = opened.size
        source_format = opened.format
        if original_width * original_height > MAX_OCR_IMAGE_PIXELS:
            raise OcrImageTooLargeError(
                f"图片像素总量超过 OCR 安全上限 {MAX_OCR_IMAGE_PIXELS:,} 像素"
            )
        exif_orientation = opened.getexif().get(274, 1)
        oriented = ImageOps.exif_transpose(opened)
        oriented_width, oriented_height = oriented.size
        if max(oriented_width, oriented_height) <= max_edge and exif_orientation == 1:
            return image, 1.0, 1.0
        resized = oriented
        resized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS, reducing_gap=3.0)
        output = BytesIO()
        if source_format == "JPEG":
            if resized.mode not in {"RGB", "L"}:
                resized = resized.convert("RGB")
            resized.save(output, format="JPEG", quality=95, subsampling=0)
        else:
            if resized.mode not in {"RGB", "RGBA", "L"}:
                resized = resized.convert("RGB")
            resized.save(output, format="PNG")
        return output.getvalue(), oriented_width / resized.width, oriented_height / resized.height


def scale_bbox_to_original(bbox, scale_x: float, scale_y: float):
    if not bbox:
        return bbox
    if isinstance(bbox[0], (list, tuple)):
        return [[round(point[0] * scale_x, 3), round(point[1] * scale_y, 3), *point[2:]]
                for point in bbox]
    if len(bbox) == 4:
        return [round(bbox[0] * scale_x, 3), round(bbox[1] * scale_y, 3),
                round(bbox[2] * scale_x, 3), round(bbox[3] * scale_y, 3)]
    return bbox

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
        page_size_points=[float(rect.width),float(rect.height)]
        if rect.width > 3000 or rect.height > 3000:
            raise ValueError("PDF 页面尺寸超过安全处理上限")
        page_text = page.get_text("text").strip()
        if len(page_text) >= 10:
            all_text.append(page_text)
            for block in page.get_text("blocks"):
                if len(block) >= 7 and block[6] == 0 and str(block[4]).strip():
                    all_lines.append({"text":str(block[4]).strip(),"confidence":None,
                        "bbox":[float(v) for v in block[:4]],"page":page_number,
                        "coordinate_space":"pdf_points","page_size_points":page_size_points,
                        "source":"PDF_TEXT_LAYER"})
        else:
            used_ocr = True
            raster_width = ceil(rect.width * 2)
            raster_height = ceil(rect.height * 2)
            if raster_width * raster_height > MAX_OCR_IMAGE_PIXELS:
                raise OcrImageTooLargeError(
                    f"PDF 扫描页像素总量超过 OCR 安全上限 {MAX_OCR_IMAGE_PIXELS:,} 像素"
                )
            pixmap = page.get_pixmap(dpi=144, alpha=False)
            page_extraction = extract_image(pixmap.tobytes("png"), page_number)
            text = page_extraction["full_text"].splitlines()
            for line in page_extraction["lines"]:
                line["page_size_points"]=page_size_points
            all_text.append("\n".join(text))
            all_lines.extend(page_extraction["lines"])
    return {"full_text":"\n".join(all_text),"lines":all_lines,
            "provider":"PyMuPDF+RapidOCR" if used_ocr else "PyMuPDF",
            "model_version":f"PP-OCRv6-Tiny/CPU; max-edge={MAX_OCR_IMAGE_EDGE}px; threads=2/1 + PyMuPDF-1.28.2" if used_ocr else "PDF-TEXT-1"}
