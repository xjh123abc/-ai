from datetime import date

from domain.settlement import normalize_bank_settlement_state
from itertools import permutations
import re


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
    rules = (
        ("TAXI_RECEIPT", ("出租汽车", "出租汽军", "出租车客运")),
        ("TOLL_RECEIPT", ("通行费票据", "车辆通行费")),
        ("BANK_RECEIPT", ("网上银行电子回单", "银行电子回单", "电子回单号码")),
        ("PAYMENT_PROOF", ("支付证明单", "支付证明")),
        ("ACCEPTANCE_REPORT", ("验收报告", "系统验收结论")),
        ("GOODS_RECEIPT", ("入库单",)),
        ("CONTRACT", ("购销合同", "合同编号")),
        ("VAT_INVOICE", ("发票号码", "增值税专用发票", "增值税普通发票", "普通发票", "发票")),
    )
    matches = [(document_type, phrase) for document_type, phrases in rules
        for phrase in phrases if phrase in compact_text]
    strong_matches = [(document_type, phrase) for document_type, phrase in matches
        if document_type != "VAT_INVOICE"]
    strong_types = {document_type for document_type, _ in strong_matches}
    if len(strong_types) > 1:
        return {"raw_value": None, "normalized_value": None, "confidence": 0,
            "status": "NEEDS_REVIEW", "page": None, "bbox": None,
            "evidence_text": ""}
    if strong_matches:
        document_type, phrase = strong_matches[0]
    elif matches:
        document_type, phrase = matches[0]
    else:
        return {"raw_value": None, "normalized_value": None, "confidence": 0,
            "status": "NEEDS_REVIEW", "page": None, "bbox": None,
            "evidence_text": ""}
    evidence = next((line for line in (lines or [])
        if phrase in re.sub(r"\s+", "", str(line.get("text") or ""))), {})
    candidate = {"raw_value": document_type, "normalized_value": document_type,
        "confidence": 0.5, "status": "NEEDS_REVIEW", "page": evidence.get("page"),
        "bbox": evidence.get("bbox"), "coordinate_space": evidence.get("coordinate_space"),
        "evidence_text": str(evidence.get("text") or phrase)}
    if evidence.get("confidence") is not None:
        candidate["evidence_confidence"] = float(evidence["confidence"])
    return candidate


def field_candidates(full_text: str, lines: list[dict] | None = None) -> dict:
    # Candidates are intentionally conservative. They remain unconfirmed until edited/accepted by a user.
    lines = lines or []
    amount_pattern = re.compile(r"(?<![\d-])[￥¥]?\s*(\d+(?:[,，]\d{3})*\.\d{2})(?![\d-])")
    normalized_date, date_evidence = normalize_date_value(full_text)
    type_candidate = document_type_candidate(full_text, lines)
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
        label_pattern = "|".join(re.escape(label) for label in labels)
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
                            or value_position[0] <= label_position[0]
                            or abs(value_position[1] - label_position[1]) > 25
                            or not value_regex.fullmatch(value_text)):
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
                if value_regex.fullmatch(next_text):
                    candidates.append((next_text, {**next_line, "text": f"{text.strip()} {next_text}"}))
        distinct_values = {value for value, _ in candidates}
        if len(distinct_values) != 1:
            return None, None
        return candidates[0]

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

    def party_candidates():
        role_labels = {
            "buyer": ("购买方", "购方", "买方"),
            "seller": ("销售方", "銷售方", "销方", "賣方", "卖方"),
        }
        headings = {role: next((line for line in lines
            if any(label in compact(str(line.get("text") or "")) for label in labels)), None)
            for role, labels in role_labels.items()}
        name_lines = []
        explicit_names = {"buyer": [], "seller": []}
        for line in lines:
            text = str(line.get("text") or "")
            role_match = re.search(r"(购买方|购方|买方|销售方|銷售方|销方|賣方|卖方)\s*[:：]\s*(\S.+)$", text)
            if role_match:
                role = "buyer" if role_match.group(1) in {"购买方", "购方", "买方"} else "seller"
                explicit_names[role].append((role_match.group(2).strip(), line))
                continue
            match = re.search(r"名称\s*[:：]\s*(.+)$", text)
            if match:
                name_lines.append((line, match.group(1).strip()))
        assignments = explicit_names
        for line, name in name_lines:
            text = compact(str(line.get("text") or ""))
            direct_role = next((role for role, labels in role_labels.items()
                if any(label in text for label in labels)), None)
            if direct_role:
                assignments[direct_role].append((name, line))
                continue
            buyer_heading, seller_heading = headings["buyer"], headings["seller"]
            position = line_position(line)
            if (position is None or buyer_heading is None or seller_heading is None
                    or line.get("page") != buyer_heading.get("page")
                    or line.get("page") != seller_heading.get("page")):
                continue
            buyer_position, seller_position = line_position(buyer_heading), line_position(seller_heading)
            if buyer_position is None or seller_position is None:
                continue
            buyer_distance = abs(position[0] - buyer_position[0]) + 2 * abs(position[1] - buyer_position[1])
            seller_distance = abs(position[0] - seller_position[0]) + 2 * abs(position[1] - seller_position[1])
            if abs(buyer_distance - seller_distance) < 80:
                continue
            assigned_role = "buyer" if buyer_distance < seller_distance else "seller"
            assignments[assigned_role].append((name, line))
        return {role: values[0] if len(values) == 1 else (None, None)
                for role, values in assignments.items()}

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
        return None, None

    def quantity_candidate(labels: tuple[str, ...]):
        quantity_pattern = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)")
        direct_labels = tuple(label for label in labels if label != "数量")
        candidates = []
        for line in lines:
            text = str(line.get("text") or "")
            normalized = compact(text)
            if not any(label in normalized for label in direct_labels):
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
    if type_candidate.get("normalized_value") == "BANK_RECEIPT":
        gross_amount, gross_line = bank_receipt_amount_candidate()
        bank_parties = bank_party_candidates()
    payment_purpose, payment_purpose_line = None, None
    payment_date, payment_date_evidence, payment_date_line = None, None, None
    settlement_date, settlement_date_line = None, None
    settled, settled_line = None, None
    receipt_number, receipt_date, receipt_item = (None, None), (None, None), (None, None)
    acceptance_date, acceptance_result = (None, None), (None, None)
    contract_number, contract_number_line = None, None
    if type_candidate.get("normalized_value") == "PAYMENT_PROOF":
        (gross_amount, gross_line), (payment_purpose, payment_purpose_line) = payment_proof_candidates()
        payment_date, payment_date_evidence, payment_date_line = payment_date_candidate()
    elif type_candidate.get("normalized_value") == "BANK_RECEIPT":
        settlement_date, settlement_date_line = bank_settlement_date_candidate()
        settled, settled_line = bank_settled_candidate()
    elif type_candidate.get("normalized_value") == "GOODS_RECEIPT":
        receipt_number, receipt_date, receipt_item = goods_receipt_candidates()
        gross_amount, gross_line = column_amount("金额")
        if gross_amount is None:
            gross_amount, gross_line = gross_amount_candidate()
    elif type_candidate.get("normalized_value") == "ACCEPTANCE_REPORT":
        acceptance_date, acceptance_result = acceptance_candidates()
    elif type_candidate.get("normalized_value") == "CONTRACT":
        contract_number, contract_number_line = contract_number_candidate()
    elif type_candidate.get("normalized_value") == "TOLL_RECEIPT":
        gross_amount, gross_line = toll_amount_candidate()
    type_value = type_candidate.get("normalized_value")
    issue_date_value = None if type_value in {"PAYMENT_PROOF", "BANK_RECEIPT", "GOODS_RECEIPT", "ACCEPTANCE_REPORT"} else normalized_date
    issue_date_raw = None if type_value in {"PAYMENT_PROOF", "BANK_RECEIPT", "GOODS_RECEIPT", "ACCEPTANCE_REPORT"} else date_evidence
    for key, value in (("gross_amount", gross_amount),
                       ("issue_date", issue_date_value),
                       ("invoice_number", invoice_number)):
        evidence_value = issue_date_raw if key == "issue_date" else value
        evidence_line = (gross_line if key == "gross_amount" and gross_line else
            invoice_line if key == "invoice_number" and invoice_line else
            next((line for line in lines if evidence_value and compact(str(evidence_value)) in compact(str(line.get("text") or ""))), {}))
        normalized_value = value.replace(",", "").replace("，", "") if value and key == "gross_amount" else value
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
    party_values = party_candidates()
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
    result["quantity"] = make_candidate(received_quantity, received_quantity, received_line)
    result["document_type_candidate"] = type_candidate
    return result
