from datetime import date
from itertools import permutations
import re

from domain.settlement import normalize_bank_settlement_state
from ingestion.party_matching import match_party_names


def normalize_date_value(value: str | None) -> tuple[str | None, str | None]:
    """Return an ISO date and the matching source text when an OCR date is recognizable."""
    if not value:
        return None, None
    compact = re.sub(r"\s+", "", value)
    match = re.search(r"(?<!\d)(20\d{2})(?:年|[-/.])(\d{1,2})(?:月|[-/.])(\d{1,2})日?", compact)
    if match is None:
        match = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", compact)
    if match is None:
        return None, None
    try:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None, match.group(0)
    return parsed.isoformat(), match.group(0)


def complete_date_matches(value: str) -> list[str]:
    """Return all complete supported date strings so ambiguous rows can be rejected."""
    return re.findall(
        r"(?<!\d)20\d{2}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}日?|(?<!\d)20\d{6}(?!\d)",
        value,
    )


def document_type_candidate(full_text: str, lines: list[dict] | None = None) -> dict:
    """Return a conservative rule-based type suggestion with its OCR evidence."""
    compact_text = re.sub(r"\s+", "", full_text)
    title_rules = (
        ("EXPENSE_REIMBURSEMENT", ("费用报销单", "报销单")),
        ("BUSINESS_RECEIPT", ("业务回单（收款）", "业务回单(收款)", "业务回单（付款）", "业务回单(付款)", "业务回单")),
        ("VAT_SPECIAL_INVOICE", ("增值税专用发票", "专用发票", "增值税专票")),
        ("VAT_INVOICE", ("增值税普通发票", "电子发票普通发票", "电子普通发票", "普通发票")),
        ("PAYMENT_APPLICATION", ("付款申请书",)),
        ("ASSET_REGISTRATION", ("资产登记表",)),
        ("OPENING_INVENTORY_INFO", ("物料期初信息", "物料期初库存")),
        ("OPENING_PAYABLE", ("期初应付单", "期初应付款")),
        ("TRAVELER_INFO", ("出差人员表", "差旅人员表")),
        ("TRAVEL_POLICY", ("商旅平台差旅标准", "差旅标准")),
        ("EMPLOYEE_DEDUCTION_SUMMARY", ("员工专项扣除统计表", "专项附加扣除统计表")),
    )
    positioned = []
    for line in lines or []:
        bbox = line.get("bbox") or []
        ys = [point[1] for point in bbox if len(point) > 1] if bbox and isinstance(bbox[0], (list, tuple)) else [bbox[1], bbox[3]] if len(bbox) == 4 else []
        if ys:
            positioned.append((line, (min(ys) + max(ys)) / 2))
    title_lines = []
    if positioned:
        top = min(y for _, y in positioned)
        bottom = max(y for _, y in positioned)
        title_lines = [line for line, y in positioned if y <= top + (bottom - top) * 0.3]
    title_text = "".join(re.sub(r"\s+", "", str(line.get("text") or "")) for line in title_lines)
    title_matches = [(document_type, phrase) for document_type, phrases in title_rules
        for phrase in phrases if phrase in title_text]
    rules = (
        ("EXPENSE_REIMBURSEMENT", ("费用报销单", "报销单")),
        ("BUSINESS_RECEIPT", ("业务回单（收款）", "业务回单(收款)", "业务回单（付款）", "业务回单(付款)", "业务回单")),
        ("TAXI_RECEIPT", ("出租汽车", "出租汽军", "出租车客运")),
        ("TOLL_RECEIPT", ("通行费票据", "车辆通行费")),
        ("PAYMENT_APPLICATION", ("付款申请书", "付款申请")),
        ("ASSET_REGISTRATION", ("资产登记表",)),
        ("OPENING_INVENTORY_INFO", ("物料期初信息", "物料期初库存", "期初库存")),
        ("OPENING_PAYABLE", ("期初应付单", "期初应付款")),
        ("TRAVELER_INFO", ("出差人员表", "差旅人员表")),
        ("TRAVEL_POLICY", ("商旅平台差旅标准", "差旅标准")),
        ("EMPLOYEE_DEDUCTION_SUMMARY", ("员工专项扣除统计表", "专项附加扣除统计表")),
        ("BANK_CREDIT_NOTICE", ("银行进账单", "进账单", "收账通知")),
        ("BANK_RECEIPT", ("网上银行电子回单", "银行电子回单", "电子回单号码")),
        ("PAYMENT_PROOF", ("支付证明单", "支付证明")),
        ("ACCEPTANCE_REPORT", ("验收报告", "系统验收结论")),
        ("GOODS_RECEIPT", ("入库单",)),
        ("CONTRACT", ("购销合同", "合同编号")),
        ("VAT_SPECIAL_INVOICE", ("增值税专用发票", "专用发票", "增值税专票")),
        ("VAT_INVOICE", ("增值税普通发票", "电子发票普通发票", "电子普通发票", "普通发票", "发票号码", "发票")),
    )
    matches = [(document_type, phrase) for document_type, phrases in rules
        for phrase in phrases if phrase in compact_text]
    title_evidence = None
    if "业务回单" in compact_text:
        matches = [("BUSINESS_RECEIPT", "业务回单")]
        title_matches = [("BUSINESS_RECEIPT", "业务回单")]
        title_evidence = next((line for line in (lines or [])
            if "业务回单" in re.sub(r"\s+", "", str(line.get("text") or ""))), None)
    if title_matches:
        matches = title_matches
    title_evidence = title_evidence or next((line for line in title_lines
        if any(phrase in re.sub(r"\s+", "", str(line.get("text") or ""))
               for _, phrase in title_matches)), None)

    tax_id_labels = [line for line in (lines or [])
        if "纳税人识别号" in re.sub(r"\s+", "", str(line.get("text") or ""))]
    invoice_code_lines = [line for line in (lines or [])
        if re.fullmatch(r"\d{10,12}", str(line.get("text") or "").strip())]
    invoice_number_lines = [line for line in (lines or [])
        if (re.search(r"\bNo\.?\s*\d{8,20}", str(line.get("text") or ""), re.IGNORECASE)
            or re.search(r"发票号码\s*[:：]?\s*\d{8,20}", str(line.get("text") or ""))
            or re.fullmatch(r"发票号码\s*[:：]?", str(line.get("text") or "").strip()))]
    special_invoice_layout = not title_matches and len(tax_id_labels) >= 2 and invoice_code_lines and invoice_number_lines
    special_layout_evidence = None
    strong_matches = [(document_type, phrase) for document_type, phrase in matches
        if document_type != "VAT_INVOICE"]
    strong_types = {document_type for document_type, _ in strong_matches}
    if len(strong_types) > 1:
        return {"raw_value": None, "normalized_value": None, "confidence": 0,
            "status": "NEEDS_REVIEW", "page": None, "bbox": None,
            "evidence_text": ""}
    if special_invoice_layout and not any(document_type == "VAT_SPECIAL_INVOICE" for document_type, _ in strong_matches):
        document_type, phrase = "VAT_SPECIAL_INVOICE", "发票代码及购销双方纳税人识别号"
        special_layout_evidence = invoice_code_lines[0]
        evidence_text = "；".join([
            str(special_layout_evidence.get("text") or "发票代码"),
            str(invoice_number_lines[0].get("text") or "发票号码"),
            *(str(line.get("text") or "纳税人识别号") for line in tax_id_labels[:2]),
        ])
    elif strong_matches:
        document_type, phrase = strong_matches[0]
        evidence_text = None
    elif matches:
        document_type, phrase = matches[0]
        evidence_text = None
    else:
        return {"raw_value": None, "normalized_value": None, "confidence": 0,
            "status": "NEEDS_REVIEW", "page": None, "bbox": None,
            "evidence_text": ""}
    if document_type == "BUSINESS_RECEIPT":
        receipt_text = compact_text
        if re.search(r"业务回单[（(]收款[）)]", receipt_text):
            document_type = "BUSINESS_RECEIPT_COLLECTION"
        elif re.search(r"业务回单[（(]付款[）)]", receipt_text):
            document_type = "BUSINESS_RECEIPT_PAYMENT"
        else:
            document_type = "BUSINESS_RECEIPT"
    evidence = title_evidence or special_layout_evidence or next((line for line in (lines or [])
        if phrase in re.sub(r"\s+", "", str(line.get("text") or ""))), {})
    candidate = {"raw_value": document_type, "normalized_value": document_type,
        "confidence": 0.5, "status": "NEEDS_REVIEW", "page": evidence.get("page"),
        "bbox": evidence.get("bbox"), "coordinate_space": evidence.get("coordinate_space"),
        "evidence_text": evidence_text or str(evidence.get("text") or phrase)}
    if evidence.get("confidence") is not None:
        candidate["evidence_confidence"] = float(evidence["confidence"])
    return candidate


def uppercase_rmb_amount(full_text: str) -> tuple[str | None, str | None]:
    """Read a Chinese uppercase RMB amount and return its decimal value and source text."""
    digits = {"零": 0, "〇": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4,
              "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}

    def integer_value(value: str) -> int | None:
        total = section = number = 0
        for char in value:
            if char in digits:
                number = digits[char]
            elif char in {"拾", "佰", "仟"}:
                unit = {"拾": 10, "佰": 100, "仟": 1000}[char]
                section += (number or 1) * unit
                number = 0
            elif char in {"万", "亿"}:
                unit = {"万": 10_000, "亿": 100_000_000}[char]
                section += number
                total += (section or 1) * unit
                section = number = 0
            else:
                return None
        return total + section + number

    compact_text = re.sub(r"\s+", "", full_text)
    pattern = r"([零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿]+)元(?:([零〇壹贰叁肆伍陆柒捌玖])角)?(?:([零〇壹贰叁肆伍陆柒捌玖])分)?(?:整|正)?"
    matches = [match for match in re.finditer(pattern, compact_text)
        if match.group(2) or match.group(3) or match.group(0).endswith(("整", "正"))]
    if len(matches) != 1:
        return None, None
    match = matches[0]
    integer = integer_value(match.group(1))
    if integer is None:
        return None, None
    jiao = digits.get(match.group(2), 0)
    fen = digits.get(match.group(3), 0)
    return f"{integer}.{jiao}{fen}", match.group(0)


def field_candidates(full_text: str, lines: list[dict] | None = None,
                     document_type: str | None = None) -> dict:
    # Candidates are intentionally conservative. They remain unconfirmed until edited/accepted by a user.
    lines = lines or []
    amount_pattern = re.compile(r"(?<![\d-])[￥¥]?\s*(\d+(?:(?:[,，]|\s+)\d{3})*\.\d{2})(?![\d-])")
    normalized_date, date_evidence = normalize_date_value(full_text)
    type_candidate = document_type_candidate(full_text, lines)
    selected_type = document_type or type_candidate.get("normalized_value")
    def compact(value: str) -> str:
        return re.sub(r"[\s,，]", "", value)

    def line_position(line: dict) -> tuple[float, float] | None:
        if line.get("_position") is not None:
            return line["_position"]
        bbox = line.get("bbox")
        if not bbox:
            return None
        if isinstance(bbox[0], (list, tuple)):
            xs = [point[0] for point in bbox if len(point) >= 2]
            ys = [point[1] for point in bbox if len(point) >= 2]
        elif len(bbox) == 4:
            xs = [bbox[0], bbox[2]]
            ys = [bbox[1], bbox[3]]
        else:
            return None
        if not xs or not ys:
            return None
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    def invoice_party_order(labels: list[dict]) -> list[dict]:
        if len(labels) == 2:
            first, second = (line_position(label) for label in labels)
            if (labels[0].get("page") == labels[1].get("page")
                    and first is not None and second is not None
                    and abs(first[1] - second[1]) <= 35):
                return sorted(labels, key=lambda label: line_position(label)[0])
        return sorted(labels, key=lambda label: (label.get("page") or 0, line_position(label)[1]))

    def explicit_total_rows() -> dict[int | None, float]:
        rows: dict[int | None, list[float]] = {}
        for line in lines:
            text = compact(str(line.get("text") or ""))
            position = line_position(line)
            if position is not None and "合计" in text and "价税合计" not in text:
                rows.setdefault(line.get("page"), []).append(position[1])
        fragments = [(line, line_position(line)) for line in lines
            if compact(str(line.get("text") or "")) in {"合", "计"} and line_position(line) is not None]
        for index, (left_line, left_position) in enumerate(fragments):
            for right_line, right_position in fragments[index + 1:]:
                if (left_line.get("page") == right_line.get("page")
                        and compact(str(left_line.get("text") or "")) != compact(str(right_line.get("text") or ""))
                        and abs(left_position[1] - right_position[1]) <= 25
                        and abs(left_position[0] - right_position[0]) <= 260):
                    rows.setdefault(left_line.get("page"), []).append((left_position[1] + right_position[1]) / 2)
        result = {}
        for page, values in rows.items():
            distinct_rows = []
            for value in sorted(values):
                if not distinct_rows or abs(value - distinct_rows[-1]) > 25:
                    distinct_rows.append(value)
            if len(distinct_rows) == 1:
                result[page] = distinct_rows[0]
        return result

    total_rows = explicit_total_rows()

    def page_boundary_y(page: int | None, below_y: float) -> float | None:
        positions = [line_position(line) for line in lines
            if line.get("page") == page
            and any(label in compact(str(line.get("text") or "")) for label in ("价税合计", "小写"))
            and line_position(line) is not None]
        return min((position[1] for position in positions if position[1] > below_y), default=None)

    def make_candidate(value, raw_value=None, evidence_line=None, confidence=0.45):
        evidence_line = evidence_line or {}
        return {"raw_value": raw_value if raw_value is not None else value,
                "normalized_value": value,
                "confidence": confidence if value else 0,
                "status": "NEEDS_REVIEW",
                "page": evidence_line.get("page"), "bbox": evidence_line.get("bbox"),
                "coordinate_space": evidence_line.get("coordinate_space"),
                "page_size_points": evidence_line.get("page_size_points"),
                "evidence_text": str(evidence_line.get("text") or "")}

    def direct_labeled_amount(labels: tuple[str, ...], exclude: tuple[str, ...] = ()):
        candidates = []
        ambiguous_label = False
        for line in lines:
            text = str(line.get("text") or "")
            normalized = compact(text)
            if any(label in normalized for label in labels) and not any(label in normalized for label in exclude):
                values = amount_pattern.findall(text)
                if len(values) == 1:
                    candidates.append((values[0].replace("，", ""), line))
                elif len(values) > 1:
                    ambiguous_label = True
        if len(candidates) > 1:
            ambiguous_label = True
        return (candidates[0] if len(candidates) == 1 and not ambiguous_label else (None, None)), ambiguous_label

    def column_amount(header: str, excludes: tuple[str, ...] = ()):
        header_lines = [line for line in lines
            if compact(str(line.get("text") or "")) == header
            and not any(label in compact(str(line.get("text") or "")) for label in excludes)
            and line_position(line) is not None]
        if not header_lines and header in {"金额", "税额"}:
            first_char, remaining = header[0], header[1:]
            first_lines = [line for line in lines
                if compact(str(line.get("text") or "")) == first_char and line_position(line) is not None]
            remaining_lines = [line for line in lines
                if compact(str(line.get("text") or "")) == remaining and line_position(line) is not None]
            for first in first_lines:
                first_x, first_y = line_position(first)
                for remainder in remaining_lines:
                    remainder_x, remainder_y = line_position(remainder)
                    if (first.get("page") == remainder.get("page") and remainder_x > first_x
                            and remainder_x - first_x <= 200 and abs(remainder_y - first_y) <= 30):
                        header_lines.append({"page": first.get("page"),
                            "_position": (remainder_x, (first_y + remainder_y) / 2)})
        if not header_lines:
            return None, None
        candidates = []
        for header_line in header_lines:
            header_x, header_y = line_position(header_line)
            page = header_line.get("page")
            boundary_y = page_boundary_y(page, header_y)
            other_header = next((line for line in lines
                if line is not header_line and line.get("page") == page and line_position(line) is not None
                and ((header == "金额" and "税额" in compact(str(line.get("text") or "")))
                     or (header == "税额" and compact(str(line.get("text") or "")) == "金额"))), None)
            other_position = line_position(other_header) if other_header else None
            tolerance = max(20.0, abs(header_x - other_position[0]) * 0.3) if other_position else 45.0
            for line in lines:
                position = line_position(line)
                values = amount_pattern.findall(str(line.get("text") or ""))
                if (position is None or line.get("page") != page or len(values) != 1
                        or position[1] <= header_y):
                    continue
                if boundary_y is not None and position[1] >= boundary_y:
                    continue
                total_y = total_rows.get(page)
                if total_y is not None and abs(position[1] - total_y) > 40:
                    continue
                if abs(position[0] - header_x) <= tolerance:
                    candidates.append((values[0].replace("，", ""), line))
        candidate_pages = {candidate[1].get("page") for candidate in candidates}
        distinct_values = {candidate[0] for candidate in candidates}
        if len(candidate_pages) != 1 or len(distinct_values) != 1:
            return None, None
        return max(candidates, key=lambda candidate: line_position(candidate[1])[1])

    def gross_amount_candidate():
        labels = ("价税合计", "小写", "合计")
        candidates = []
        for line in lines:
            text = str(line.get("text") or "")
            if any(label in compact(text) for label in labels):
                values = amount_pattern.findall(text)
                if len(values) == 1:
                    candidates.append((values[0].replace("，", ""), line))
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            # Printed invoices often split “（小写）” and its Arabic total into
            # neighboring OCR boxes. Keep the mapping only when that label has
            # one unambiguous amount to its right on the same row.
            small_total_labels = [line for line in lines
                if "小写" in compact(str(line.get("text") or "")) and line_position(line) is not None]
            split_small_totals = []
            for label in small_total_labels:
                label_x, label_y = line_position(label)
                for line in lines:
                    position = line_position(line)
                    values = amount_pattern.findall(str(line.get("text") or ""))
                    if (position is not None and line.get("page") == label.get("page")
                            and len(values) == 1 and position[0] > label_x
                            and abs(position[1] - label_y) <= 30):
                        normalized = re.sub(r"\s+", "", values[0]).replace("，", "").replace(",", "")
                        split_small_totals.append((normalized, line, abs(position[0] - label_x)))
            distinct_small_totals = {value for value, _, _ in split_small_totals}
            if len(distinct_small_totals) == 1 and split_small_totals:
                value, evidence, _ = min(split_small_totals, key=lambda item: item[2])
                return value, evidence

            # Some forms place the currency amount in a neighboring OCR box
            # beside an explicit “合计” label.
            nearby_totals = []
            total_labels = [line for line in lines
                if "合计" in compact(str(line.get("text") or ""))
                and "价税合计" not in compact(str(line.get("text") or ""))
                and line_position(line) is not None]
            for label in total_labels:
                label_x, label_y = line_position(label)
                for line in lines:
                    position = line_position(line)
                    values = amount_pattern.findall(str(line.get("text") or ""))
                    if (position is not None and line.get("page") == label.get("page")
                            and len(values) == 1 and position[0] > label_x
                            and abs(position[1] - label_y) <= 35):
                        nearby_totals.append((values[0].replace("，", ""), line))
            if len(nearby_totals) == 1:
                return nearby_totals[0]

            # Older invoice layouts put the Arabic total beside the Chinese
            # uppercase total instead of on the same OCR line as its label.
            uppercase_totals = [line for line in lines
                if re.search(r"[零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+[元圆]", str(line.get("text") or ""))
                and line_position(line) is not None]
            adjacent = []
            for uppercase in uppercase_totals:
                ux, uy = line_position(uppercase)
                for line in lines:
                    text = str(line.get("text") or "")
                    position = line_position(line)
                    values = amount_pattern.findall(text)
                    if (position is not None and line.get("page") == uppercase.get("page")
                            and len(values) == 1 and re.search(r"[￥¥]", text)
                            and abs(position[1] - uy) <= 25 and position[0] > ux + 100):
                        adjacent.append((values[0].replace("，", ""), line))
            distinct = {value for value, _ in adjacent}
            if len(adjacent) == 1 and len(distinct) == 1:
                return adjacent[0]
        return None, None

    def toll_amount_candidate():
        # Some toll receipts print a currency-marked integer instead of a
        # two-decimal amount. Accept only one explicit currency value.
        currency_pattern = re.compile(r"[￥¥]\s*(\d+(?:[,，]\d{3})*(?:\.\d{1,2})?)(?![\d.,，])")
        candidates = []
        for line in lines:
            matches = currency_pattern.findall(str(line.get("text") or ""))
            if len(matches) == 1:
                value = matches[0].replace(",", "").replace("，", "")
                if "." not in value:
                    value = f"{value}.00"
                elif len(value.rsplit(".", 1)[1]) == 1:
                    value += "0"
                candidates.append((value, line))
        return candidates[0] if len(candidates) == 1 else (None, None)

    def payment_proof_candidates():
        # Payment proofs often split the label and its value into separate OCR
        # boxes. Require a unique same-row amount and a unique value below the
        # explicit purpose header; ambiguous layouts stay empty for review.
        amount_labels = [line for line in lines
            if "共计金额" in compact(str(line.get("text") or "")) and line_position(line) is not None]
        amounts = []
        for label in amount_labels:
            x, y = line_position(label)
            for line in lines:
                pos = line_position(line)
                values = amount_pattern.findall(str(line.get("text") or ""))
                if (pos is not None and line.get("page") == label.get("page")
                        and len(values) == 1 and abs(pos[1] - y) <= 30 and pos[0] > x):
                    amounts.append((values[0].replace("，", ""), line))
        amount_values = {item[0] for item in amounts}
        amount = (amounts[0] if len(amounts) == 1 else
                  next((item for item in amounts if len(amount_values) == 1), (None, None)))
        # Multiple OCR boxes with the same value can be duplicates; differing
        # values or multiple distinct purpose text boxes are unresolved.
        purpose_headers = [line for line in lines
            if compact(str(line.get("text") or "")) == "事由或品名" and line_position(line) is not None]
        purpose_candidates = []
        for header in purpose_headers:
            x, y = line_position(header)
            for line in lines:
                pos = line_position(line)
                value = str(line.get("text") or "").strip()
                if (pos is not None and value and line is not header
                        and line.get("page") == header.get("page")
                        and y < pos[1] <= y + 80 and pos[0] < x + 120
                        and compact(value) not in {"数量", "单位", "单价", "金额"}):
                    purpose_candidates.append((value, line))
        purposes = {item[0] for item in purpose_candidates}
        purpose = (next((item for item in purpose_candidates if item[0] in purposes), (None, None))
                   if len(purposes) == 1 else (None, None))
        return amount, purpose

    def payment_date_candidate():
        year_lines = [line for line in lines
            if re.search(r"20\d{2}", str(line.get("text") or "")) and line_position(line) is not None]
        if len(year_lines) != 1:
            return None, None, None
        year_line = year_lines[0]
        year_x, year_y = line_position(year_line)
        related = []
        for line in lines:
            position = line_position(line)
            text = str(line.get("text") or "").strip()
            if (position is None or line.get("page") != year_line.get("page")
                    or abs(position[1] - year_y) > 30 or position[0] < year_x - 20
                    or position[0] > year_x + 230):
                continue
            if line is year_line or re.search(r"[年月日/]", text):
                related.append((position[0], line))
        related.sort(key=lambda item: item[0])
        raw_date = "".join(str(line.get("text") or "") for _, line in related)
        date_matches = complete_date_matches(raw_date)
        if len(date_matches) != 1:
            return None, None, None
        normalized, evidence_text = normalize_date_value(raw_date)
        if normalized is None:
            return None, None, None
        boxes = []
        for _, line in related:
            bbox = line.get("bbox")
            if not bbox:
                continue
            if isinstance(bbox[0], (list, tuple)):
                boxes.extend(point for point in bbox if len(point) >= 2)
            elif len(bbox) == 4:
                left, top, right, bottom = bbox
                boxes.extend(((left, top), (right, bottom)))
        bbox = ([min(point[0] for point in boxes), min(point[1] for point in boxes),
                 max(point[0] for point in boxes), max(point[1] for point in boxes)] if boxes else None)
        evidence = {"page": year_line.get("page"), "bbox": bbox,
            "coordinate_space": year_line.get("coordinate_space"),
            "text": "".join(str(line.get("text") or "") for _, line in related)}
        return normalized, evidence_text, evidence

    def bank_settlement_date_candidate():
        candidates = []
        for line in lines:
            text = str(line.get("text") or "")
            if not any(label in compact(text) for label in ("记账日期", "交易日期")):
                continue
            values = complete_date_matches(text)
            if len(values) != 1:
                continue
            normalized, _ = normalize_date_value(text)
            if normalized:
                candidates.append((normalized, line))
        return candidates[0] if len(candidates) == 1 else (None, None)

    def bank_settled_candidate():
        success_terms = ("交易成功", "转账成功", "支付成功", "汇款成功", "已成功到账", "到账成功")
        failure_terms = ("交易失败", "转账失败", "支付失败", "汇款失败", "到账失败", "未到账", "未成功")
        outcomes = []
        for line in lines:
            text = compact(str(line.get("text") or ""))
            succeeded = any(term in text for term in success_terms)
            failed = any(term in text for term in failure_terms)
            if succeeded != failed:
                outcomes.append(("true" if succeeded else "false", line))
        distinct_outcomes = {value for value, _ in outcomes}
        if len(outcomes) == 1 and len(distinct_outcomes) == 1:
            value, line = outcomes[0]
            return normalize_bank_settlement_state(value), line
        return "UNKNOWN", None

    def bank_auxiliary_candidate(labels: tuple[str, ...], value_pattern: str):
        candidates = []
        known_field_labels = {
            "用途", "摘要", "交易摘要", "业务种类", "交易类型", "单位", "数量", "单价", "金额",
            "入库单位及部门", "入库单位", "发票号码或生产单号", "发票号码或", "生产单号", "来源单号",
            "科目", "会计科目", "付款人账号", "付款方账号", "收款人账号", "收款方账号",
            "付款人账户", "付款方账户", "收款人账户", "收款方账户", "银行账号", "银行账户",
            "电子回单号", "电子回单号码", "电子回单编号", "回单编号", "交易流水号", "流水号", "交易序号",
            "付款人", "付款方", "付款人户名", "付款人名称", "付款账号", "付款人账号", "付款方账号",
            "付款人账号（卡号）", "付款方账号（卡号）", "付款开户行", "付款人开户行", "付款方开户行",
            "收款人", "收款方", "收款人户名", "收款人名称", "收款账号", "收款人账号", "收款方账号",
            "收款人账号（卡号）", "收款方账号（卡号）", "收款开户行", "收款人开户行", "收款方开户行",
            "业务种类", "业务（产品）种类", "业务产品种类", "凭证种类", "凭证号码", "凭证编号", "币种", "日期", "交易日期", "业务日期",
        }
        label_pattern = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
        value_regex = re.compile(rf"^(?:{value_pattern})$", re.IGNORECASE)
        for index, line in enumerate(lines):
            text = str(line.get("text") or "")
            match = re.search(rf"(?:{label_pattern})\s*[:：]?\s*({value_pattern})", text, re.IGNORECASE)
            if match:
                value = match.group(1).strip().rstrip("，,。；;")
                if value:
                    candidates.append((value, line))
                continue

            if not any(label in compact(text) for label in labels):
                continue
            label_position = line_position(line)
            same_row_values = []
            if label_position is not None:
                for value_line in lines:
                    value_position = line_position(value_line)
                    value_text = str(value_line.get("text") or "").strip()
                    if (value_line.get("page") != line.get("page") or value_position is None
                            or compact(value_text) in known_field_labels
                            or value_position[0] <= label_position[0]
                            or abs(value_position[1] - label_position[1]) > 25
                            or not value_regex.fullmatch(value_text)):
                        continue
                    intervening_label = any(
                        other is not line and other.get("page") == line.get("page")
                        and compact(str(other.get("text") or "")) in known_field_labels
                        and (other_position := line_position(other)) is not None
                        and abs(other_position[1] - label_position[1]) <= 25
                        and label_position[0] < other_position[0] < value_position[0]
                        for other in lines
                    )
                    if intervening_label:
                        continue
                    same_row_values.append((abs(value_position[0] - label_position[0]), value_text, value_line))
            if same_row_values:
                same_row_values.sort(key=lambda item: item[0])
                if len(same_row_values) == 1 or same_row_values[1][0] - same_row_values[0][0] >= 35:
                    _, value, value_line = same_row_values[0]
                    candidates.append((value, {**value_line, "text": f"{text.strip()} {value}"}))
                    continue

            if index + 1 < len(lines):
                next_line = lines[index + 1]
                next_text = str(next_line.get("text") or "").strip()
                next_compact = compact(next_text)
                is_following_label = next_compact in known_field_labels
                if value_regex.fullmatch(next_text) and not is_following_label:
                    candidates.append((next_text, {**next_line, "text": f"{text.strip()} {next_text}"}))
        distinct_values = {value for value, _ in candidates}
        if len(distinct_values) != 1:
            return None, None
        return candidates[0]

    def labeled_value_candidate(labels: tuple[str, ...], value_pattern: str):
        """Read a value beside a form label, accepting inline and split OCR boxes."""
        known_labels = {
            "申请日期", "付款单号", "付款编号", "付字编号", "收款单位", "收款单位名称", "收款账号",
            "收款人账号", "收款方账号", "开户行", "开户银行", "收款银行全称", "付款金额", "金额",
            "付款原因", "付款用途", "用途", "汇款方式", "备注", "附件张数", "附件数量", "审批状态",
            "资产类别", "类别", "资产名称", "名称", "单位", "购入日期", "取得日期", "投入使用日期",
            "启用日期", "折旧年限", "使用年限", "资产位置", "存放地点", "供应商", "供货单位", "使用部门",
            "部门", "费用项目", "物料", "物料名称", "仓库", "期初数量", "数量", "单价", "期初单价",
            "供应商", "采购员", "采购部门", "业务日期", "到期日", "含税单价", "税率", "姓名", "员工姓名",
            "职级", "职务", "职称", "证件号码", "身份证号码", "手机号", "手机号码", "交通类型", "住宿类型",
            "交通/住宿类型", "报销标准", "城市等级", "金额上限", "员工", "基本工资", "子女教育", "继续教育",
            "住房贷款", "住房贷款利息", "住房租金", "赡养老人", "婴幼儿照护", "大病医疗",
            "付款人户名", "付款人名称", "付款人", "付款方", "付款账号", "付款人账号", "付款方账号",
            "付款人账号（卡号）", "付款人账号(卡号)", "付款方账号（卡号）", "付款方账号(卡号)",
            "付款开户行", "付款人开户行", "付款方开户行", "收款人户名", "收款人名称", "收款人", "收款方",
            "收款账号", "收款人账号", "收款方账号", "收款人账号（卡号）", "收款人账号(卡号)",
            "收款方账号（卡号）", "收款方账号(卡号)", "收款开户行", "收款人开户行", "收款方开户行",
            "业务种类", "业务（产品）种类", "业务产品种类", "凭证种类", "凭证号码", "凭证编号", "币种",
            "回单编号", "回单号", "日期", "交易日期",
        }
        known_labels = sorted(known_labels | set(labels), key=len, reverse=True)
        known_compact = {compact(label) for label in known_labels}
        label_pattern = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
        following_labels = "|".join(re.escape(label) for label in known_labels)
        value_regex = re.compile(rf"^(?:{value_pattern})$", re.IGNORECASE)
        inline_regex = re.compile(
            rf"(?:{label_pattern})\s*[:：]?\s*({value_pattern}?)(?=\s*(?:{following_labels})\s*[:：]?|$)",
            re.IGNORECASE,
        )
        candidates = []
        for index, line in enumerate(lines):
            text = str(line.get("text") or "")
            line_candidates = []
            for match in inline_regex.finditer(text):
                value = match.group(1).strip().strip(" :：,，;；。")
                if value and value_regex.fullmatch(value):
                    line_candidates.append((value, line))
            if line_candidates:
                candidates.extend(line_candidates)
                continue

            normalized_text = compact(text)
            if not any(compact(label) in normalized_text for label in labels):
                continue
            label_position = line_position(line)
            nearby = []
            if label_position is not None:
                for value_line in lines:
                    position = line_position(value_line)
                    value = str(value_line.get("text") or "").strip()
                    if (position is None or value_line.get("page") != line.get("page")
                            or compact(value) in known_compact or position[0] <= label_position[0]
                            or abs(position[1] - label_position[1]) > 25
                            or not value_regex.fullmatch(value)):
                        continue
                    between = any(
                        other is not line and other.get("page") == line.get("page")
                        and compact(str(other.get("text") or "")) in known_compact
                        and (other_position := line_position(other)) is not None
                        and abs(other_position[1] - label_position[1]) <= 25
                        and label_position[0] < other_position[0] < position[0]
                        for other in lines
                    )
                    if not between:
                        nearby.append((abs(position[0] - label_position[0]), value, value_line))
            if nearby:
                nearby.sort(key=lambda item: item[0])
                if len(nearby) == 1 or nearby[1][0] - nearby[0][0] >= 35:
                    _, value, value_line = nearby[0]
                    candidates.append((value, {**value_line, "text": f"{text.strip()} {value}"}))
                    continue

            if index + 1 < len(lines):
                next_line = lines[index + 1]
                next_text = str(next_line.get("text") or "").strip()
                if (compact(next_text) not in known_compact and value_regex.fullmatch(next_text)):
                    candidates.append((next_text, {**next_line, "text": f"{text.strip()} {next_text}"}))

        values = {value for value, _ in candidates}
        return candidates[0] if len(values) == 1 and candidates else (None, None)

    def goods_receipt_candidates():
        numbers = []
        for line in lines:
            match = re.search(r"\bNO\s*([0-9]{6,})\b", str(line.get("text") or ""), re.IGNORECASE)
            if match:
                numbers.append((f"NO{match.group(1)}", line))
        receipt_number = numbers[0] if len(numbers) == 1 else (None, None)

        # The receipt date is often a separate OCR box beside a two-line
        # “入库 / 日期” label. Require the explicit label and a unique date row.
        date_headers = [line for line in lines
            if "入库" in compact(str(line.get("text") or "")) and line_position(line) is not None]
        date_lines = [line for line in lines
            if "日期" in compact(str(line.get("text") or "")) and line_position(line) is not None]
        direct_dates = []
        for line in lines:
            text = str(line.get("text") or "")
            if "入库日期" not in compact(text):
                continue
            date_values = complete_date_matches(text)
            normalized, _ = normalize_date_value(text)
            if len(date_values) == 1 and normalized:
                direct_dates.append((normalized, line))
        if direct_dates:
            receipt_date = direct_dates[0] if len(direct_dates) == 1 else (None, None)
        else:
            dates = []
            for header in date_headers:
                hx, hy = line_position(header)
                for date_label in date_lines:
                    lx, ly = line_position(date_label)
                    if date_label.get("page") != header.get("page") or abs(lx - hx) > 45 or abs(ly - hy) > 40:
                        continue
                    for line in lines:
                        pos = line_position(line)
                        text = str(line.get("text") or "")
                        if len(complete_date_matches(text)) != 1:
                            continue
                        normalized, _ = normalize_date_value(text)
                        if (pos is not None and normalized and line.get("page") == header.get("page")
                                and pos[0] > hx and abs(pos[1] - (hy + ly) / 2) <= 35
                                and abs(pos[0] - hx) <= 130):
                            dates.append((normalized, line))
            receipt_date = dates[0] if len(dates) == 1 else (None, None)

        item_headers = [line for line in lines
            if compact(str(line.get("text") or "")) in {"名称及规格", "货物名称", "品名"}
            and line_position(line) is not None]
        items = []
        for header in item_headers:
            hx, hy = line_position(header)
            for line in lines:
                pos = line_position(line)
                text = str(line.get("text") or "").strip()
                if (pos is not None and text and line.get("page") == header.get("page")
                        and hy < pos[1] <= hy + 65 and abs(pos[0] - hx) <= 70
                        and not re.fullmatch(r"[\d.,]+", text)):
                    items.append((text, line))
        distinct_items = {item[0] for item in items}
        item = (next((candidate for candidate in items if candidate[0] in distinct_items), (None, None))
                if len(distinct_items) == 1 else (None, None))
        return receipt_number, receipt_date, item

    def item_candidate(headers: tuple[str, ...]):
        header_lines = [line for line in lines
            if compact(str(line.get("text") or "")) in headers and line_position(line) is not None]
        row_headers = [line for line in lines
            if compact(str(line.get("text") or "")) in {"单位", "数量", "单价", "金额"}
            and line_position(line) is not None]

        def has_other_column_cell_at(row_y: float, page: int) -> bool:
            for row_header in row_headers:
                header_x, header_y = line_position(row_header)
                if row_header.get("page") != page:
                    continue
                label = compact(str(row_header.get("text") or ""))
                header_bbox = row_header.get("bbox")
                header_width = header_bbox[2][0] - header_bbox[0][0] if header_bbox and isinstance(header_bbox[0], (list, tuple)) else 40
                tolerance = max(55, header_width * 0.45)
                for cell in lines:
                    position = line_position(cell)
                    value = str(cell.get("text") or "").strip().replace(",", "").replace("，", "")
                    if (position is None or cell is row_header or cell.get("page") != page
                            or not header_y < position[1] <= header_y + 100
                            or abs(position[1] - row_y) > 10
                            or abs(position[0] - header_x) > tolerance):
                        continue
                    if label == "单位" and re.fullmatch(r"[^\s:：,，]{1,12}", value) and not re.fullmatch(r"\d+(?:\.\d+)?", value):
                        return True
                    if label == "数量" and re.fullmatch(r"\d+(?:\.\d+)?", value):
                        return True
                    if label in {"单价", "金额"} and re.fullmatch(r"[￥¥]?\s*\d+(?:\.\d{1,2})?", value):
                        return True
            return False

        def has_complete_invoice_row(row_y: float, page: int) -> bool:
            matched_columns = set()
            for row_header in row_headers:
                header_x, header_y = line_position(row_header)
                if row_header.get("page") != page:
                    continue
                label = compact(str(row_header.get("text") or ""))
                header_bbox = row_header.get("bbox")
                header_width = header_bbox[2][0] - header_bbox[0][0] if header_bbox and isinstance(header_bbox[0], (list, tuple)) else 40
                tolerance = max(55, header_width * 0.45)
                for cell in lines:
                    position = line_position(cell)
                    value = re.sub(r"\s+", "", str(cell.get("text") or "")).replace(",", "").replace("，", "")
                    if (position is None or cell is row_header or cell.get("page") != page
                            or not header_y < position[1] <= header_y + 100
                            or abs(position[1] - row_y) > 10
                            or abs(position[0] - header_x) > tolerance):
                        continue
                    if label == "数量" and re.fullmatch(r"\d+(?:\.\d+)?", value):
                        matched_columns.add(label)
                    elif label in {"单价", "金额"} and re.fullmatch(r"[￥¥]?\d+(?:\.\d{1,2})?", value):
                        matched_columns.add(label)
            return "数量" in matched_columns and bool(matched_columns & {"单价", "金额"})

        candidates = []
        excluded = {"项目名称", "名称及规格", "货物名称", "货物及应税劳务名称", "货物或应税劳务、服务名称", "品名", "单位", "数量", "单价", "金额", "税率", "税额"}
        for header in header_lines:
            header_x, header_y = line_position(header)
            header_bbox = header.get("bbox")
            header_width = (header_bbox[2][0] - header_bbox[0][0] if isinstance(header_bbox[0], (list, tuple))
                            else header_bbox[2] - header_bbox[0]) if header_bbox else 0
            column_tolerance = max(90, header_width * 0.55)
            values = []
            for line in lines:
                position = line_position(line)
                value = str(line.get("text") or "").strip()
                if (position is None or line.get("page") != header.get("page") or not value
                        or not header_y < position[1] <= header_y + 75
                        or abs(position[0] - header_x) > column_tolerance
                        or compact(value) in excluded or amount_pattern.fullmatch(value)
                        or re.fullmatch(r"\d+(?:\.\d+)?", value)):
                    continue
                values.append((position[1], value, line))
            values.sort(key=lambda item: item[0])
            if not values:
                continue
            if any(values[index + 1][0] - values[index][0] > 32 for index in range(len(values) - 1)):
                continue
            separate_invoice_rows = len(values) > 1 and all(
                has_complete_invoice_row(line_position(line)[1], line.get("page"))
                for _, _, line in values
            )
            if len(values) > 1 and not separate_invoice_rows:
                quantity, quantity_line = quantity_candidate(("数量",))
                quantity_position = line_position(quantity_line) if quantity_line else None
                first_item_position = line_position(values[0][2])
                if (not quantity or quantity_position is None or first_item_position is None
                        or abs(quantity_position[1] - first_item_position[1]) > 20):
                    continue
                if any(has_other_column_cell_at(line_position(line)[1], line.get("page"))
                       for _, _, line in values[1:]):
                    continue
                # A single quantity cannot distinguish a wrapped name from a second
                # row whose cells OCR missed. Join only when a categorized first
                # fragment ends with a continuation word such as "专用" or "配套".
                first_fragment = values[0][1]
                if (not re.search(r"\*[^*]+\*", first_fragment)
                        or not first_fragment.endswith(("专用", "配套"))):
                    continue
                if any(re.search(r"\*[^*]+\*", value) for _, value, _ in values[1:]):
                    continue
            joined_value = ("；" if separate_invoice_rows else " ").join(value for _, value, _ in values)
            evidence = dict(values[0][2])
            evidence["text"] = joined_value
            if len(values) > 1:
                evidence["_joined_lines"] = len(values)
            corners = []
            for _, _, line in values:
                bbox = line.get("bbox")
                if bbox and isinstance(bbox[0], (list, tuple)):
                    corners.extend((point[0], point[1]) for point in bbox if len(point) >= 2)
                elif bbox and len(bbox) == 4:
                    corners.extend(((bbox[0], bbox[1]), (bbox[2], bbox[3])))
            if corners:
                evidence["bbox"] = [min(point[0] for point in corners), min(point[1] for point in corners),
                    max(point[0] for point in corners), max(point[1] for point in corners)]
            candidates.append((joined_value, evidence))
        distinct = {value for value, _ in candidates}
        return candidates[0] if len(candidates) == 1 and len(distinct) == 1 else (None, None)

    def spatial_cell_candidate(headers: tuple[str, ...], pattern: str, max_distance: float):
        value_regex = re.compile(pattern)
        header_lines = [line for line in lines
            if compact(str(line.get("text") or "")) in headers and line_position(line) is not None]
        candidates = []
        for header in header_lines:
            header_x, header_y = line_position(header)
            bbox = header.get("bbox")
            header_width = bbox[2][0] - bbox[0][0] if bbox and isinstance(bbox[0], (list, tuple)) else 40
            tolerance = max(max_distance, header_width * 0.45)
            for line in lines:
                position = line_position(line)
                value = re.sub(r"\s+", "", str(line.get("text") or "")).replace(",", "").replace("，", "")
                if (position is None or line.get("page") != header.get("page")
                        or not header_y < position[1] <= header_y + 100
                        or abs(position[0] - header_x) > tolerance or not value_regex.fullmatch(value)):
                    continue
                candidates.append((value, line))
        distinct = {value for value, _ in candidates}
        return candidates[0] if len(candidates) == 1 and len(distinct) == 1 else (None, None)

    def acceptance_candidates():
        acceptance_date_labels = [line for line in lines
            if "验收日期" in compact(str(line.get("text") or "")) and line_position(line) is not None]
        dated_candidates = []
        for label in acceptance_date_labels:
            text = str(label.get("text") or "")
            date_values = complete_date_matches(text)
            normalized, _ = normalize_date_value(text)
            if len(date_values) == 1 and normalized:
                dated_candidates.append((normalized, label))
                continue
            label_x, label_y = line_position(label)
            for line in lines:
                position = line_position(line)
                value_text = str(line.get("text") or "")
                values = complete_date_matches(value_text)
                value, _ = normalize_date_value(value_text)
                if (position is not None and line is not label and value and len(values) == 1
                        and line.get("page") == label.get("page")
                        and position[0] > label_x and abs(position[1] - label_y) <= 35
                        and abs(position[0] - label_x) <= 450):
                    dated_candidates.append((value, line))
        date_values = {value for value, _ in dated_candidates}
        acceptance_date = (dated_candidates[0] if len(date_values) == 1 and dated_candidates else (None, None))

        checked_success = [line for line in lines
            if re.search(r"[■☑✓√]通过验收", compact(str(line.get("text") or "")))]
        checked_failure = [line for line in lines
            if re.search(r"[■☑✓√](?:未通过验收|不通过验收)", compact(str(line.get("text") or "")))]
        result = ("通过验收", checked_success[0]) if len(checked_success) == 1 and not checked_failure else (None, None)
        return acceptance_date, result

    def contract_number_candidate():
        candidates = []
        for line in lines:
            text = str(line.get("text") or "")
            matches = re.findall(r"合同编号\s*[:：]?\s*([A-Z0-9][A-Z0-9-]{4,})", text, re.IGNORECASE)
            if matches:
                if len(matches) == 1:
                    candidates.append((matches[0], line))
                continue
            if compact(text) != "合同编号" or line_position(line) is None:
                continue
            header_x, header_y = line_position(line)
            for value_line in lines:
                position = line_position(value_line)
                value = str(value_line.get("text") or "").strip()
                if (position is not None and value_line.get("page") == line.get("page")
                        and position[0] > header_x and abs(position[1] - header_y) <= 25
                        and abs(position[0] - header_x) <= 200
                        and re.fullmatch(r"[A-Z0-9][A-Z0-9-]{4,}", value, re.IGNORECASE)
                        and re.search(r"\d", value)):
                    candidates.append((value, value_line))
        distinct_values = {value for value, _ in candidates}
        return (candidates[0] if len(candidates) == 1 and len(distinct_values) == 1
                else (None, None))

    def bank_receipt_amount_candidate():
        headers = [line for line in lines
            if compact(str(line.get("text") or "")) == "金额" and line_position(line) is not None]
        candidates = []
        for header in headers:
            header_x, header_y = line_position(header)
            for line in lines:
                position = line_position(line)
                values = amount_pattern.findall(str(line.get("text") or ""))
                if (position is None or line.get("page") != header.get("page") or len(values) != 1
                        or abs(position[1] - header_y) > 30 or position[0] <= header_x):
                    continue
                candidates.append((values[0].replace("，", ""), line))
        if len(candidates) != 1:
            return None, None
        return candidates[0]

    def bank_party_candidates():
        role_headers = {
            "payer": [line for line in lines if "付款人" in compact(str(line.get("text") or ""))],
            "payee": [line for line in lines if "收款人" in compact(str(line.get("text") or ""))],
        }
        name_headers = [line for line in lines
            if compact(str(line.get("text") or "")) == "户名" and line_position(line) is not None]
        role_positions = {
            role: (headers[0], line_position(headers[0]))
            for role, headers in role_headers.items() if len(headers) == 1 and line_position(headers[0]) is not None
        }
        possible_assignments = []
        if role_positions:
            for selected_headers in permutations(name_headers, len(role_positions)):
                assignment = dict(zip(role_positions, selected_headers))
                costs = []
                for role, name_header in assignment.items():
                    role_header, role_position = role_positions[role]
                    if name_header.get("page") != role_header.get("page"):
                        costs.append(float("inf"))
                        continue
                    name_position = line_position(name_header)
                    distance = abs(name_position[0] - role_position[0]) + 2 * abs(name_position[1] - role_position[1])
                    costs.append(distance if distance <= 250 else float("inf"))
                if all(cost < float("inf") for cost in costs):
                    possible_assignments.append((sum(costs), assignment))
        possible_assignments.sort(key=lambda item: item[0])
        assigned_headers = {}
        if possible_assignments and (len(possible_assignments) == 1
                or possible_assignments[1][0] - possible_assignments[0][0] >= 40):
            assigned_headers = possible_assignments[0][1]

        output = {}
        for role in role_headers:
            name_header = assigned_headers.get(role)
            values = []
            if name_header is not None:
                name_position = line_position(name_header)
                name_bbox = name_header.get("bbox")
                name_height = (name_bbox[2][1] - name_bbox[0][1]) if isinstance(name_bbox[0], (list, tuple)) else 20
                same_row_values = []
                for line in lines:
                    position = line_position(line)
                    text = str(line.get("text") or "")
                    if (position is None or line.get("page") != name_header.get("page")
                            or position[0] <= name_position[0]
                            or abs(position[1] - name_position[1]) > max(20, name_height * 0.8)
                            or any(label in compact(text) for label in ("户名", "账号", "开户银行", "付款人", "收款人"))):
                        continue
                    if text.strip():
                        same_row_values.append((abs(position[0] - name_position[0]), text.strip(), line))
                same_row_values.sort(key=lambda item: item[0])
                if not same_row_values or (len(same_row_values) > 1
                        and same_row_values[1][0] - same_row_values[0][0] < 35):
                    values = []
                else:
                    _, value, evidence = same_row_values[0]
                    values.append((value, evidence))
            output[role] = values[0] if len(values) == 1 else (None, None)
        return output

    def bank_credit_notice_candidates():
        def row_value(header: dict, excluded_labels: tuple[str, ...] = ()):
            header_position = line_position(header)
            if header_position is None:
                return None, None
            candidates = []
            for line in lines:
                position = line_position(line)
                text = str(line.get("text") or "").strip()
                if (position is None or line.get("page") != header.get("page")
                        or position[0] <= header_position[0]
                        or abs(position[1] - header_position[1]) > 25
                        or not text or any(label in compact(text) for label in excluded_labels)
                        or compact(text) == compact(str(header.get("text") or ""))):
                    continue
                candidates.append((abs(position[0] - header_position[0]), text, line))
            candidates.sort(key=lambda item: item[0])
            if not candidates or (len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 35):
                return None, None
            _, value, evidence = candidates[0]
            return value, evidence

        output = {}
        name_headers = [line for line in lines
            if compact(str(line.get("text") or "")) == "全称" and line_position(line) is not None]
        if len(name_headers) == 2:
            name_headers.sort(key=lambda line: line_position(line)[0])
            excluded = ("全称", "出票人", "收款人", "开户银行", "账", "号", "票据", "人民币")
            for role, header in zip(("payer", "payee"), name_headers):
                output[role] = row_value(header, excluded)
        else:
            output["payer"] = (None, None)
            output["payee"] = (None, None)

        account_pattern = re.compile(r"[0-9Xx*][0-9Xx*·\- ]{5,32}")
        account_labels = [line for line in lines
            if compact(str(line.get("text") or "")) in {"账", "号"} and line_position(line) is not None]
        accounts = []
        for line in lines:
            text = str(line.get("text") or "").strip()
            position = line_position(line)
            if position is None or not account_pattern.fullmatch(text):
                continue
            matching_labels = [label for label in account_labels
                if label.get("page") == line.get("page")
                and line_position(label)[0] < position[0]
                and abs(line_position(label)[1] - position[1]) <= 35]
            if matching_labels:
                value = re.sub(r"\s*[-－]\s*", "-", text)
                value = re.sub(r"\s+", "", value)
                accounts.append((position[0], value, line))
        accounts.sort(key=lambda item: item[0])
        if len(accounts) == 2:
            output["payer_account_number"] = (accounts[0][1], accounts[0][2])
            output["payee_account_number"] = (accounts[1][1], accounts[1][2])
        else:
            output["payer_account_number"] = (None, None)
            output["payee_account_number"] = (None, None)

        bank_headers = [line for line in lines
            if compact(str(line.get("text") or "")) == "开户银行" and line_position(line) is not None]
        if len(bank_headers) == 2:
            bank_headers.sort(key=lambda line: line_position(line)[0])
            excluded = ("开户银行", "全称", "出票人", "收款人", "账", "号", "人民币", "票据")
            for role, header in zip(("payer_bank_name", "payee_bank_name"), bank_headers):
                output[role] = row_value(header, excluded)
        else:
            output["payer_bank_name"] = (None, None)
            output["payee_bank_name"] = (None, None)

        receipt_numbers = []
        for line in lines:
            match = re.search(r"第\s*([0-9]{3,})\s*号", str(line.get("text") or ""))
            if match:
                receipt_numbers.append((match.group(1), line))
        output["receipt_number"] = receipt_numbers[0] if len(receipt_numbers) == 1 else (None, None)

        date_line = next((line for line in lines
            if len(complete_date_matches(str(line.get("text") or ""))) == 1), None)
        output["settlement_date"] = (normalize_date_value(str(date_line.get("text") or ""))[0], date_line) \
            if date_line else (None, None)

        amount_value, amount_text = uppercase_rmb_amount(full_text)
        amount_line = next((line for line in lines
            if amount_text and amount_text in compact(str(line.get("text") or ""))), None)
        output["gross_amount"] = (amount_value, amount_line)

        instrument_headers = {
            "instrument_type": "票据种类",
            "instrument_count": "票据张数",
        }
        chinese_count = {"壹": "1", "贰": "2", "叁": "3", "肆": "4", "伍": "5",
                         "陆": "6", "柒": "7", "捌": "8", "玖": "9", "零": "0"}
        for key, label in instrument_headers.items():
            headers = [line for line in lines
                if compact(str(line.get("text") or "")) == label and line_position(line) is not None]
            if len(headers) != 1:
                output[key] = (None, None)
                continue
            value, evidence = row_value(headers[0], instrument_headers.values())
            if key == "instrument_count" and value in chinese_count:
                value = chinese_count[value]
            output[key] = (value, evidence)

        bank_title = next((line for line in lines
            if "工商银行进账单" in compact(str(line.get("text") or ""))), None)
        bank_match = re.search(r"中国(.+?银行)", str(bank_title.get("text") or "")) if bank_title else None
        output["bank_name"] = (bank_match.group(0), bank_title) if bank_match else (None, None)
        return output

    def tax_rate_candidate():
        rate_pattern = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?\s*%)")
        for line in lines:
            text = str(line.get("text") or "")
            if "税率" in text or "征收率" in text:
                matches = rate_pattern.findall(text)
                if len(matches) == 1:
                    return matches[0].replace(" ", ""), line
        header = next((line for line in lines
            if any(label in compact(str(line.get("text") or "")) for label in ("税率", "征收率"))
            and line_position(line) is not None), None)
        if header is None:
            return None, None
        header_x, header_y = line_position(header)
        boundary_y = page_boundary_y(header.get("page"), header_y)
        candidates = []
        for line in lines:
            position = line_position(line)
            rates = rate_pattern.findall(str(line.get("text") or ""))
            if position is None or line.get("page") != header.get("page") or len(rates) != 1 or position[1] <= header_y:
                continue
            if boundary_y is not None and position[1] >= boundary_y:
                continue
            candidates.append((abs(position[0] - header_x), position[1], rates[0].replace(" ", ""), line))
        if not candidates:
            return None, None
        closest_distance = min(item[0] for item in candidates)
        closest = [item for item in candidates if item[0] <= closest_distance + 20]
        distinct_rates = {item[2] for item in closest}
        if len(distinct_rates) != 1:
            return None, None
        _, _, value, evidence_line = max(closest, key=lambda item: item[1])
        return value, evidence_line

    def invoice_number_candidate():
        for line in lines:
            text = str(line.get("text") or "")
            match = re.search(r"发票号码\s*[:：]?\s*(\d{8,20})", text)
            if match:
                return match.group(1), line
        for line in lines:
            match = re.search(r"\bNo\.?\s*([0-9]{8,20})", str(line.get("text") or ""), re.IGNORECASE)
            if match:
                return match.group(1), line
        for label in lines:
            if not re.fullmatch(r"发票号码\s*[:：]?", str(label.get("text") or "").strip()):
                continue
            label_position = line_position(label)
            if label_position is None:
                continue
            candidates = []
            for line in lines:
                position = line_position(line)
                value = str(line.get("text") or "").strip()
                if (position is not None and line.get("page") == label.get("page")
                        and position[0] > label_position[0]
                        and abs(position[1] - label_position[1]) <= 30
                        and re.fullmatch(r"\d{8,20}", value)):
                    candidates.append((value, line))
            if len(candidates) == 1:
                return candidates[0]
        return None, None

    def invoice_code_candidate():
        for line in lines:
            match = re.search(r"发票代码\s*[:：]?\s*(\d{10,12})", str(line.get("text") or ""))
            if match:
                return match.group(1), line
        if selected_type != "VAT_SPECIAL_INVOICE":
            return None, None
        code_lines = [(str(line.get("text") or "").strip(), line) for line in lines
            if re.fullmatch(r"\d{10,12}", str(line.get("text") or "").strip())]
        distinct_codes = {value for value, _ in code_lines}
        return (code_lines[0] if len(distinct_codes) == 1 and code_lines else (None, None))

    def invoice_tax_id_candidates():
        labels = [line for line in lines
            if "纳税人识别号" in compact(str(line.get("text") or ""))
            and line_position(line) is not None]
        labels = invoice_party_order(labels)
        values = []
        tax_id_pattern = re.compile(r"[A-Z0-9]{12,24}", re.IGNORECASE)
        for label in labels:
            text = str(label.get("text") or "")
            direct = re.search(r"纳税人识别号\s*[:：]?\s*([A-Z0-9]{12,24})", text, re.IGNORECASE)
            if direct:
                values.append((direct.group(1), label))
                continue
            label_x, label_y = line_position(label)
            candidates = []
            for line in lines:
                position = line_position(line)
                value = str(line.get("text") or "").strip()
                if (position is None or line.get("page") != label.get("page")
                        or position[0] <= label_x or abs(position[1] - label_y) > 38
                        or not tax_id_pattern.fullmatch(value)):
                    continue
                candidates.append((position[0] - label_x, value, line))
            candidates.sort(key=lambda item: item[0])
            values.append((candidates[0][1], candidates[0][2]) if candidates else (None, None))
        if len(values) != 2 or any(value is None for value, _ in values):
            return {"buyer_tax_id": (None, None), "seller_tax_id": (None, None)}
        return {"buyer_tax_id": values[0], "seller_tax_id": values[1]}

    def quantity_candidate(labels: tuple[str, ...]):
        quantity_pattern = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)")
        direct_labels = tuple(label for label in labels if label != "数量")
        candidates = []
        for line in lines:
            text = str(line.get("text") or "")
            normalized = compact(text)
            direct_match = any(label in normalized for label in direct_labels) if direct_labels else any(
                label in normalized for label in labels)
            if not direct_match:
                continue
            matches = quantity_pattern.findall(text)
            if len(matches) == 1:
                candidates.append((matches[0], line))
        distinct = {candidate[0] for candidate in candidates}
        if len(candidates) == 1 and len(distinct) == 1:
            return candidates[0]
        if candidates:
            return None, None

        # OCR commonly emits a table header and its cell value as separate lines.
        # Accept a spatial match only when one unambiguous numeric cell sits below
        # a recognized quantity header; multiple rows are not silently summed.
        header_lines = [line for line in lines
            if compact(str(line.get("text") or "")) in labels and line_position(line) is not None]
        spatial_candidates = []
        for header in header_lines:
            header_x, header_y = line_position(header)
            page = header.get("page")
            header_bbox = header.get("bbox")
            header_height = (header_bbox[2][1] - header_bbox[0][1]) if header_bbox and isinstance(header_bbox[0], (list, tuple)) else 20
            for line in lines:
                position = line_position(line)
                values = quantity_pattern.findall(str(line.get("text") or ""))
                if (position is None or line.get("page") != page or len(values) != 1
                        or position[1] <= header_y
                        or abs(position[0] - header_x) > max(35.0, header_height * 1.5)):
                    continue
                normalized = values[0]
                if "." in normalized:
                    normalized = normalized.rstrip("0").rstrip(".")
                spatial_candidates.append((normalized, line))
        if len(spatial_candidates) != 1:
            return None, None
        return spatial_candidates[0]

    result = {}
    invoice_number, invoice_line = invoice_number_candidate()
    gross_amount, gross_line = gross_amount_candidate()
    bank_parties = {"payer": (None, None), "payee": (None, None)}
    credit_notice_fields = {key: (None, None) for key in (
        "payer", "payee", "payer_account_number", "payee_account_number", "payer_bank_name",
        "payee_bank_name", "receipt_number", "settlement_date", "gross_amount", "instrument_type",
        "instrument_count", "bank_name",
    )}
    if selected_type == "BANK_CREDIT_NOTICE":
        credit_notice_fields = bank_credit_notice_candidates()
        bank_parties = {role: credit_notice_fields[role] for role in ("payer", "payee")}
        gross_amount, gross_line = credit_notice_fields["gross_amount"]
    business_receipt_fields = {}
    if selected_type == "BUSINESS_RECEIPT" or (selected_type or "").startswith("BUSINESS_RECEIPT_"):
        labels = {
            "receipt_number": (("回单编号", "回单号"), r"[A-Za-z0-9\-_/]{3,50}"),
            "payer": (("付款人户名", "付款人名称", "付款人", "付款方"), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,80}"),
            "payer_account_number": (("付款人账号（卡号）", "付款人账号(卡号)", "付款人账号（卡号)", "付款人账号(卡号）", "付款方账号（卡号）", "付款方账号(卡号)", "付款方账号（卡号)", "付款方账号(卡号）", "付款账号", "付款人账号", "付款方账号"), r"[0-9Xx*·\- ]{6,32}"),
            "payer_bank_name": (("付款开户行", "付款人开户行", "付款方开户行"), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,60}"),
            "payee": (("收款人户名", "收款人名称", "收款人", "收款方"), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,80}"),
            "payee_account_number": (("收款人账号（卡号）", "收款人账号(卡号)", "收款人账号（卡号)", "收款人账号(卡号）", "收款方账号（卡号）", "收款方账号(卡号)", "收款方账号（卡号)", "收款方账号(卡号）", "收款账号", "收款人账号", "收款方账号"), r"[0-9Xx*·\- ]{6,32}"),
            "payee_bank_name": (("收款开户行", "收款人开户行", "收款方开户行"), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,60}"),
            "transaction_type": (("业务（产品）种类", "业务产品种类", "业务种类"), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{1,60}"),
            "instrument_type": (("凭证种类",), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{1,60}"),
            "instrument_number": (("凭证号码", "凭证编号"), r"[A-Za-z0-9\-_/]{1,50}"),
            "transaction_summary": (("摘要",), r"[^，,；;。]{1,120}"),
            "currency": (("币种",), r"[\u4e00-\u9fffA-Za-z]{1,20}"),
        }
        for key, (field_labels, pattern) in labels.items():
            business_receipt_fields[key] = labeled_value_candidate(field_labels, pattern)
        date_value, date_line = labeled_value_candidate(
            ("日期", "交易日期", "业务日期"),
            r"20\d{2}\s*(?:年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?|[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}|\d{4})",
        )
        business_receipt_fields["settlement_date"] = (
            normalize_date_value(date_value)[0], date_line) if date_value else (None, None)
        amount_line = next((line for line in lines if any(label in compact(str(line.get("text") or ""))
            for label in ("金额", "小写"))
            and amount_pattern.search(str(line.get("text") or ""))), None)
        if amount_line:
            amount_match = amount_pattern.search(str(amount_line.get("text") or ""))
            business_receipt_fields["gross_amount"] = (amount_match.group(1).replace(",", "").replace("，", ""), amount_line)
        else:
            business_receipt_fields["gross_amount"] = direct_labeled_amount(("金额",))[0]
            if business_receipt_fields["gross_amount"][0] is None:
                business_receipt_fields["gross_amount"] = column_amount("金额")
            if business_receipt_fields["gross_amount"][0] is None:
                business_receipt_fields["gross_amount"] = (gross_amount, gross_line)
    if selected_type == "BANK_RECEIPT":
        gross_amount, gross_line = bank_receipt_amount_candidate()
        bank_parties = bank_party_candidates()
    payment_purpose, payment_purpose_line = None, None
    payment_date, payment_date_evidence, payment_date_line = None, None, None
    settlement_date, settlement_date_line = None, None
    settled, settled_line = None, None
    receipt_number, receipt_date, receipt_item = (None, None), (None, None), (None, None)
    acceptance_date, acceptance_result = (None, None), (None, None)
    contract_number, contract_number_line = None, None
    if selected_type == "PAYMENT_PROOF":
        (gross_amount, gross_line), (payment_purpose, payment_purpose_line) = payment_proof_candidates()
        payment_date, payment_date_evidence, payment_date_line = payment_date_candidate()
    elif selected_type == "BANK_RECEIPT":
        settlement_date, settlement_date_line = bank_settlement_date_candidate()
        settled, settled_line = bank_settled_candidate()
    elif selected_type == "BANK_CREDIT_NOTICE":
        settlement_date, settlement_date_line = credit_notice_fields["settlement_date"]
        receipt_number = credit_notice_fields["receipt_number"]
    elif selected_type == "GOODS_RECEIPT":
        receipt_number, receipt_date, receipt_item = goods_receipt_candidates()
        gross_amount, gross_line = column_amount("金额")
        if gross_amount is None:
            gross_amount, gross_line = gross_amount_candidate()
    elif selected_type == "ACCEPTANCE_REPORT":
        acceptance_date, acceptance_result = acceptance_candidates()
    elif selected_type == "CONTRACT":
        contract_number, contract_number_line = contract_number_candidate()
    elif selected_type == "TOLL_RECEIPT":
        gross_amount, gross_line = toll_amount_candidate()
    type_value = selected_type
    issue_date_value = None if type_value in {"PAYMENT_PROOF", "BANK_RECEIPT", "BANK_CREDIT_NOTICE", "GOODS_RECEIPT", "ACCEPTANCE_REPORT"} else normalized_date
    issue_date_raw = None if type_value in {"PAYMENT_PROOF", "BANK_RECEIPT", "BANK_CREDIT_NOTICE", "GOODS_RECEIPT", "ACCEPTANCE_REPORT"} else date_evidence
    for key, value in (("gross_amount", gross_amount),
                       ("issue_date", issue_date_value),
                       ("invoice_number", invoice_number)):
        evidence_value = issue_date_raw if key == "issue_date" else value
        evidence_line = (gross_line if key == "gross_amount" and gross_line else
            invoice_line if key == "invoice_number" and invoice_line else
            next((line for line in lines if evidence_value and compact(str(evidence_value)) in compact(str(line.get("text") or ""))), {}))
        normalized_value = re.sub(r"\s+", "", value).replace(",", "").replace("，", "") if value and key == "gross_amount" else value
        result[key] = make_candidate(normalized_value, issue_date_raw if key == "issue_date" else value, evidence_line)
    net_direct, net_ambiguous = direct_labeled_amount(("不含税金额", "不含税价"))
    net_value, net_line = net_direct
    tax_direct, tax_ambiguous = direct_labeled_amount(("税额",), ("税率", "征收率"))
    tax_value, tax_line = tax_direct
    if net_value is None and not net_ambiguous:
        net_value, net_line = column_amount("金额", ("税额", "税率"))
    if tax_value is None and not tax_ambiguous:
        tax_value, tax_line = column_amount("税额", ("税率", "征收率"))
    result["net_amount"] = make_candidate(net_value, net_value, net_line)
    result["tax_amount"] = make_candidate(tax_value, tax_value, tax_line)
    tax_rate, tax_rate_line = tax_rate_candidate()
    result["tax_rate"] = make_candidate(tax_rate, tax_rate, tax_rate_line)
    result["purpose"] = make_candidate(payment_purpose, payment_purpose, payment_purpose_line)
    result["payment_date"] = make_candidate(payment_date, payment_date_evidence, payment_date_line)
    result["settlement_date"] = make_candidate(settlement_date, settlement_date, settlement_date_line)
    result["settled"] = make_candidate(settled, settled_line.get("text") if settled_line else None, settled_line)
    if settled == "UNKNOWN":
        result["settled"]["confidence"] = 0
    for key, labels in (
        ("payer_account_number", ("付款人账号", "付款方账号", "付款人账户", "付款方账户")),
        ("payee_account_number", ("收款人账号", "收款方账号", "收款人账户", "收款方账户")),
        ("bank_account_number", ("银行账号", "银行账户")),
    ):
        value, evidence_line = bank_auxiliary_candidate(labels, r"[0-9Xx*·\- ]{6,32}")
        result[key] = make_candidate(value, value, evidence_line)
    bank_name, bank_name_line = bank_auxiliary_candidate(
        ("付款人开户银行", "付款方开户银行", "收款人开户银行", "收款方开户银行", "开户银行"),
        r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,40}",
    )
    result["bank_name"] = make_candidate(bank_name, bank_name, bank_name_line)
    electronic_number, receipt_line = bank_auxiliary_candidate(
        ("电子回单号", "电子回单号码", "电子回单编号", "回单编号"), r"[A-Za-z0-9-]{6,40}",
    )
    result["electronic_receipt_number"] = make_candidate(electronic_number, electronic_number, receipt_line)
    transaction_id, transaction_line = bank_auxiliary_candidate(
        ("交易流水号", "流水号", "交易序号"), r"[A-Za-z0-9-]{6,40}",
    )
    result["transaction_id"] = make_candidate(transaction_id, transaction_id, transaction_line)
    for key in ("payer_account_number", "payee_account_number", "payer_bank_name", "payee_bank_name", "bank_name"):
        value, evidence = credit_notice_fields[key]
        if value:
            result[key] = make_candidate(value, value, evidence)
    for key in ("payer_bank_name", "payee_bank_name"):
        result.setdefault(key, make_candidate(None, None))
    if type_value == "BANK_RECEIPT":
        def paired_values(label: str, pattern: str):
            values = []
            for index, line in enumerate(lines[:-1]):
                if compact(str(line.get("text") or "")) != label:
                    continue
                value_line = lines[index + 1]
                value = str(value_line.get("text") or "").strip()
                position = line_position(line)
                if position and re.fullmatch(pattern, value):
                    values.append((position[0], value, value_line))
            return sorted(values, key=lambda item: item[0]) if len(values) == 2 else []

        accounts = paired_values("账号", r"[0-9Xx*·\- ]{6,32}")
        if accounts:
            result["payer_account_number"] = make_candidate(accounts[0][1], accounts[0][1], accounts[0][2])
            result["payee_account_number"] = make_candidate(accounts[1][1], accounts[1][1], accounts[1][2])
            both_accounts = f"付款方：{accounts[0][1]}；收款方：{accounts[1][1]}"
            result["bank_account_number"] = make_candidate(both_accounts, both_accounts)
        banks = paired_values("开户银行", r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,40}")
        if banks:
            both_banks = f"付款方：{banks[0][1]}；收款方：{banks[1][1]}"
            result["bank_name"] = make_candidate(both_banks, both_banks)
    receipt_number_value, receipt_number_line = receipt_number
    receipt_date_value, receipt_date_line = receipt_date
    receipt_item_value, receipt_item_line = receipt_item
    result["receipt_number"] = make_candidate(receipt_number_value, receipt_number_value, receipt_number_line)
    result["receipt_date"] = make_candidate(receipt_date_value, receipt_date_value, receipt_date_line)
    result["item"] = make_candidate(receipt_item_value, receipt_item_value, receipt_item_line)
    acceptance_date_value, acceptance_date_line = acceptance_date
    acceptance_result_value, acceptance_result_line = acceptance_result
    result["acceptance_date"] = make_candidate(acceptance_date_value, acceptance_date_value, acceptance_date_line)
    result["acceptance_result"] = make_candidate(acceptance_result_value, acceptance_result_value, acceptance_result_line)
    result["contract_number"] = make_candidate(contract_number, contract_number, contract_number_line)
    for key in ("instrument_type", "instrument_count"):
        value, evidence = credit_notice_fields[key]
        result[key] = make_candidate(value, value, evidence)
    party_values = match_party_names(lines)
    for role in ("buyer", "seller"):
        value, evidence_line = party_values[role]
        result[role] = make_candidate(value, value, evidence_line)
    for role in ("payer", "payee"):
        value, evidence_line = bank_parties[role]
        result[role] = make_candidate(value, value, evidence_line)
    is_receipt = "入库单" in compact(full_text) or "入库数量" in compact(full_text)
    is_contract = "合同" in compact(full_text) or "合同采购数量" in compact(full_text)
    ordered_labels = ("合同采购数量", "采购数量", "订购数量", "订单数量") + (("数量",) if is_contract and not is_receipt else ())
    received_labels = ("实际入库数量", "实收数量", "验收数量", "入库数量", "收货数量") + (("数量",) if is_receipt else ())
    ordered_quantity, ordered_line = quantity_candidate(ordered_labels)
    received_quantity, received_line = quantity_candidate(received_labels)
    result["ordered_quantity"] = make_candidate(ordered_quantity, ordered_quantity, ordered_line)
    quantity_value, quantity_line = received_quantity, received_line
    if type_value in {"VAT_INVOICE", "VAT_SPECIAL_INVOICE", "PAYMENT_PROOF"}:
        quantity_value, quantity_line = quantity_candidate(("数量",))
    result["quantity"] = make_candidate(quantity_value, quantity_value, quantity_line)
    item_value, item_line = (None, None)
    if type_value in {"VAT_INVOICE", "VAT_SPECIAL_INVOICE"}:
        item_value, item_line = item_candidate(("项目名称", "项目名称及规格", "货物名称", "货物及应税劳务名称", "货物或应税劳务、服务名称", "品名"))
    elif type_value == "GOODS_RECEIPT":
        item_value, item_line = receipt_item_value, receipt_item_line
    item_candidate = make_candidate(item_value, item_value, item_line)
    if item_line and item_line.get("_joined_lines"):
        item_candidate["confidence"] = 0.25
    result["item"] = item_candidate

    unit_value, unit_line = spatial_cell_candidate(("单位",), r"[^\s:：,，]{1,12}", 25)
    unit_price_value, unit_price_line = spatial_cell_candidate(
        ("单价",), r"[￥¥]?\s*\d+(?:[,，]\d{3})*(?:\.\d{1,2})?", 55)
    result["unit"] = make_candidate(unit_value, unit_value, unit_line)
    if unit_price_value:
        normalized_unit_price = unit_price_value.replace("￥", "").replace("¥", "").replace(",", "").replace("，", "").strip()
        if "." not in normalized_unit_price:
            normalized_unit_price += ".00"
        elif len(normalized_unit_price.rsplit(".", 1)[1]) == 1:
            normalized_unit_price += "0"
    else:
        normalized_unit_price = unit_price_value
    result["unit_price"] = make_candidate(normalized_unit_price, unit_price_value, unit_price_line)

    invoice_code, invoice_code_line = invoice_code_candidate()
    result["invoice_code"] = make_candidate(invoice_code, invoice_code, invoice_code_line)
    tax_ids = invoice_tax_id_candidates() if type_value == "VAT_SPECIAL_INVOICE" else {
        "buyer_tax_id": (None, None), "seller_tax_id": (None, None),
    }
    result["buyer_tax_id"] = make_candidate(tax_ids["buyer_tax_id"][0], tax_ids["buyer_tax_id"][0], tax_ids["buyer_tax_id"][1])
    result["seller_tax_id"] = make_candidate(tax_ids["seller_tax_id"][0], tax_ids["seller_tax_id"][0], tax_ids["seller_tax_id"][1])

    department, department_line = bank_auxiliary_candidate(
        ("入库单位及部门", "入库单位"), r"(?!(?:及部门)$)[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,40}")
    result["department"] = make_candidate(department, department, department_line)
    source_reference, source_reference_line = bank_auxiliary_candidate(
        ("发票号码或生产单号", "发票号码或", "生产单号", "来源单号"), r"[A-Za-z0-9-]{4,30}")
    result["source_reference_number"] = make_candidate(source_reference, source_reference, source_reference_line)
    if type_value == "GOODS_RECEIPT":
        status_value = None
        status_line = None
        negative_statuses = ("未入库", "待入库", "未收货", "待收货")
        positive_statuses = ("已收货", "已入库", "收货完成", "入库完成", "验收合格")
        negative_lines = [line for line in lines if any(label in compact(str(line.get("text") or ""))
            for label in negative_statuses)]
        positive_lines = [line for line in lines if any(label in compact(str(line.get("text") or ""))
            for label in positive_statuses)]
        if negative_lines and not positive_lines:
            status_value = "false"
            status_line = negative_lines[0]
        elif positive_lines and not negative_lines:
            status_value = "true"
            status_line = positive_lines[0]
        result["received"] = make_candidate(status_value, status_value, status_line)

    payment_proof_number = None
    payment_proof_number_line = None
    for line in lines:
        proof_match = re.search(r"(?:证明编号|支付证明单号|总号第)\s*[:：]?\s*([A-Z0-9][A-Z0-9-]{3,30})\s*(?:号)?", str(line.get("text") or ""), re.IGNORECASE)
        if proof_match:
            payment_proof_number = proof_match.group(1)
            payment_proof_number_line = line
            break
    result["proof_number"] = make_candidate(payment_proof_number, payment_proof_number, payment_proof_number_line)
    expense_subject, expense_subject_line = bank_auxiliary_candidate(("科目", "会计科目"), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,40}")
    result["expense_subject"] = make_candidate(expense_subject, expense_subject, expense_subject_line)
    approval_line = next((line for line in lines if re.search(
        r"(?:主管人|审批人|批准人)\s*[:：]?\s*[\u4e00-\u9fff]{2,4}", str(line.get("text") or ""))), None)
    result["approved"] = make_candidate("true" if approval_line else None,
        str(approval_line.get("text") or "") if approval_line else None, approval_line)

    summary_pattern = r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,60}"
    transaction_summary, transaction_summary_line = bank_auxiliary_candidate(("用途",), summary_pattern)
    if not transaction_summary:
        transaction_summary, transaction_summary_line = bank_auxiliary_candidate(("摘要", "交易摘要"), summary_pattern)
    transaction_type, transaction_type_line = bank_auxiliary_candidate(("业务种类", "交易类型"), r"[\u4e00-\u9fffA-Za-z0-9（）()· -]{2,30}")
    result["transaction_summary"] = make_candidate(transaction_summary, transaction_summary, transaction_summary_line)
    result["transaction_type"] = make_candidate(transaction_type, transaction_type, transaction_type_line)

    if selected_type in {
        "PAYMENT_APPLICATION", "ASSET_REGISTRATION", "OPENING_INVENTORY_INFO", "OPENING_PAYABLE",
        "TRAVELER_INFO", "TRAVEL_POLICY", "EMPLOYEE_DEDUCTION_SUMMARY", "BUSINESS_RECEIPT",
        "BUSINESS_RECEIPT_COLLECTION", "BUSINESS_RECEIPT_PAYMENT", "EXPENSE_REIMBURSEMENT",
    }:
        form_text_pattern = r"[\u4e00-\u9fffA-Za-z0-9（）()· .\-/]{1,80}"
        money_value_pattern = r"(?:[¥￥]\s*)?\d+(?:[,，]\d{3})*(?:\.\d{1,2})?(?:\s*元)?"
        numeric_value_pattern = r"\d+(?:\.\d+)?"
        date_value_pattern = r"(?:20\d{2}\s*(?:年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?|[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})|20\d{6})"

        def form_field(key: str, labels: tuple[str, ...], pattern: str = form_text_pattern,
                       normalize=None, table_distance: float = 80):
            value, evidence = labeled_value_candidate(labels, pattern)
            if value is None:
                value, evidence = spatial_cell_candidate(
                    tuple(compact(label) for label in labels), pattern, table_distance,
                )
            normalized = normalize(value) if value and normalize else value
            result[key] = make_candidate(normalized, value, evidence)
            return normalized, evidence

        def normalized_money(value: str) -> str:
            normalized = re.sub(r"[¥￥,，\s元]", "", value)
            if "." not in normalized:
                return f"{normalized}.00"
            whole, fraction = normalized.split(".", 1)
            return f"{whole}.{fraction.ljust(2, '0')[:2]}"

        def normalize_form_date(value: str) -> str | None:
            return normalize_date_value(value)[0]

        if selected_type == "PAYMENT_APPLICATION":
            form_field("application_date", ("申请日期",), date_value_pattern, normalize_form_date)
            form_field("payment_number", ("付款单号", "付款编号", "付字编号"), r"[\u4e00-\u9fffA-Za-z0-9\-/]{2,40}")
            form_field("payee", ("收款单位名称", "收款单位"))
            form_field("payee_account_number", ("收款账号", "收款人账号", "收款方账号"), r"[0-9Xx*·\- ]{6,32}")
            form_field("bank_name", ("开户行", "开户银行"))
            form_field("payee_bank_name", ("收款银行全称",), r"[\u4e00-\u9fffA-Za-z0-9（）()· .\-/]{2,80}")
            form_field("gross_amount", ("付款金额", "申请金额", "金额"), money_value_pattern, normalized_money)
            form_field("purpose", ("付款原因", "付款用途", "付款事由"))
            form_field("remittance_method", ("汇款方式", "付款方式"))
            form_field("remarks", ("备注", "说明"))
            form_field("attachment_count", ("附件张数", "附件数量"), numeric_value_pattern)
            approval, approval_line = labeled_value_candidate(
                ("审批状态",), r"(?:已批准|审批通过|已通过|待审批|审批中|未审批|已驳回|驳回|拒绝|未通过)",
            )
            result["approval_status"] = make_candidate(approval, approval, approval_line)

        elif selected_type == "ASSET_REGISTRATION":
            form_field("asset_category", ("资产类别", "资产分类", "类别"))
            form_field("asset_name", ("资产名称", "设备名称", "名称"))
            form_field("unit", ("单位",))
            form_field("acquisition_date", ("购入日期", "取得日期"), date_value_pattern, normalize_form_date)
            form_field("in_service_date", ("投入使用日期", "启用日期"), date_value_pattern, normalize_form_date)
            form_field("depreciation_years", ("折旧年限", "使用年限"), numeric_value_pattern)
            form_field("asset_location", ("资产位置", "存放地点", "使用地点"))
            form_field("supplier", ("供应商", "供货单位"))
            form_field("department", ("使用部门", "部门"))
            form_field("expense_subject", ("费用项目",))

        elif selected_type == "OPENING_INVENTORY_INFO":
            form_field("item", ("物料名称", "物料", "商品名称"))
            form_field("warehouse", ("仓库", "仓库名称"))
            form_field("opening_quantity", ("期初数量", "期初库存数量"), numeric_value_pattern)
            form_field("unit_price", ("期初单价", "单价"), money_value_pattern, normalized_money)
            form_field("gross_amount", ("期初金额", "金额"), money_value_pattern, normalized_money)

        elif selected_type == "OPENING_PAYABLE":
            form_field("supplier", ("供应商", "供货单位"))
            form_field("purchaser", ("采购员", "采购人员"))
            form_field("department", ("采购部门",))
            form_field("business_date", ("业务日期",), date_value_pattern, normalize_form_date)
            form_field("due_date", ("到期日", "到期日期"), date_value_pattern, normalize_form_date)
            form_field("item", ("物料名称", "物料", "商品名称"))
            form_field("unit_price", ("含税单价",), money_value_pattern, normalized_money)
            form_field("quantity", ("数量",), numeric_value_pattern)
            form_field("tax_rate", ("税率", "税率(%)", "税率（%）"), r"\d+(?:\.\d+)?\s*%")
            form_field("remarks", ("备注", "说明"))

        elif selected_type == "TRAVELER_INFO":
            form_field("employee_name", ("员工姓名", "姓名"))
            form_field("department", ("部门", "所属部门"))
            form_field("job_title", ("职级", "职务", "职称"))
            form_field("id_number", ("证件号码", "身份证号码", "证件号"), r"[0-9A-Za-z]{8,24}")
            form_field("phone", ("手机号", "手机号码", "联系电话"), r"1[3-9]\d{9}")

        elif selected_type == "TRAVEL_POLICY":
            form_field("travel_type", ("交通/住宿类型", "交通类型", "住宿类型", "交通住宿类型"))
            form_field("job_grade", ("职级", "职务级别"))
            form_field("reimbursement_standard", ("报销标准", "差旅标准"))
            form_field("city_level", ("城市等级", "城市级别"))
            form_field("amount_limit", ("金额上限", "最高限额", "限额"), money_value_pattern, normalized_money)

        elif selected_type == "EMPLOYEE_DEDUCTION_SUMMARY":
            form_field("employee_name", ("员工姓名", "员工", "姓名"))
            for key, labels in (
                ("basic_salary", ("基本工资",)),
                ("child_education", ("子女教育",)),
                ("continuing_education", ("继续教育",)),
                ("housing_loan_interest", ("住房贷款利息", "住房贷款")),
                ("housing_rent", ("住房租金",)),
                ("support_elderly", ("赡养老人",)),
                ("infant_care", ("婴幼儿照护",)),
                ("serious_illness_medical", ("大病医疗",)),
            ):
                form_field(key, labels, money_value_pattern, normalized_money)

        elif selected_type == "EXPENSE_REIMBURSEMENT":
            reimbursement_date, _ = form_field("reimbursement_date", ("填单日期", "报销日期", "日期"), date_value_pattern, normalize_form_date)
            if not reimbursement_date and isinstance(normalized_date, str):
                date_line = next((line for line in lines if date_evidence and date_evidence in compact(str(line.get("text") or ""))), None)
                result["reimbursement_date"] = make_candidate(normalized_date, date_evidence, date_line)
            form_field("department", ("报销部门", "报销部门："))
            payee, _ = form_field("payee", ("领款人", "收款人"))
            if not payee:
                form_field("payee", ("报销人", "经办人", "申请人"))
            purpose, purpose_line = labeled_value_candidate(("报销事由", "事由", "用途"), form_text_pattern)
            if purpose is None:
                purpose_headers = [line for line in lines
                    if compact(str(line.get("text") or "")) in {"用途", "报销内容", "事由"}
                    and line_position(line) is not None]
                split_use = [line for line in lines if compact(str(line.get("text") or "")) == "用"
                    and line_position(line) is not None]
                split_usage = [line for line in lines if compact(str(line.get("text") or "")) == "途"
                    and line_position(line) is not None]
                for first in split_use:
                    fx, fy = line_position(first)
                    for second in split_usage:
                        sx, sy = line_position(second)
                        if first.get("page") == second.get("page") and abs(fx - sx) <= 180 and abs(fy - sy) <= 30:
                            purpose_headers.append({**first, "_position": ((fx + sx) / 2, (fy + sy) / 2)})
                purpose_values = []
                for header in purpose_headers:
                    hx, hy = line_position(header)
                    for line in lines:
                        pos = line_position(line)
                        value = str(line.get("text") or "").strip()
                        if (pos is not None and line is not header and line.get("page") == header.get("page")
                                and hy < pos[1] <= hy + 85 and abs(pos[0] - hx) <= 180
                                and value and not amount_pattern.fullmatch(value)
                                and compact(value) not in {"用", "途", "金额", "备注", "合计"}):
                            purpose_values.append((value, line))
                if len({value for value, _ in purpose_values}) == 1 and purpose_values:
                    purpose, purpose_line = purpose_values[0]
            result["purpose"] = make_candidate(purpose, purpose, purpose_line)
            # The total row is the most reliable amount on a reimbursement form;
            # itemized amounts remain available in the evidence and can be reviewed.
            # Reimbursement forms commonly print a line-item amount without a
            # currency mark and the actual total as “¥ 389.00” beside a split
            # OCR header (“合” / “计”). Prefer that unambiguous total marker.
            currency_totals = []
            for line in lines:
                text = str(line.get("text") or "")
                values = amount_pattern.findall(text)
                if re.search(r"[¥￥]", text) and len(values) == 1:
                    currency_totals.append((values[0].replace("，", ""), line))
            if len({value for value, _ in currency_totals}) == 1 and currency_totals:
                total_value, total_line = currency_totals[0]
            else:
                total_value, total_line = direct_labeled_amount(("合计", "总计"))
            if total_value is None:
                total_value, total_line = gross_amount_candidate()
            if total_value:
                result["gross_amount"] = make_candidate(normalized_money(total_value), total_value, total_line)

        elif selected_type == "BUSINESS_RECEIPT" or (selected_type or "").startswith("BUSINESS_RECEIPT_"):
            for key, (value, evidence) in business_receipt_fields.items():
                result[key] = make_candidate(value, value, evidence)
            for role in ("payer", "payee"):
                value, evidence = business_receipt_fields.get(role, (None, None))
                result[role] = make_candidate(value, value, evidence)

    result["document_type_candidate"] = type_candidate
    return result
