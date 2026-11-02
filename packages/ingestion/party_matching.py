"""Match buyer and seller names to the party labels that anchor their regions."""

from __future__ import annotations

import re


PARTY_LABELS = {
    "buyer": ("购买方", "购方", "买方", "购货单位", "采购方", "采购单位", "需方"),
    "seller": (
        "销售方", "銷售方", "销方", "卖方", "賣方", "销货单位", "销售单位",
        "供货方", "供方", "供货单位",
    ),
}
PARTY_METADATA_LABELS = ("纳税人识别号", "地址", "电话", "开户行", "账号")
NON_PARTY_MARKERS = ("盖章", "记账联", "记账凭证")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _edit_distance(left: str, right: str) -> int:
    """Return a small edit distance, stopping once a label cannot be a near match."""
    if abs(len(left) - len(right)) > 1:
        return 2
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        if min(current) > 1:
            return 2
        previous = current
    return previous[-1]


def _label_prefix(text: str) -> str:
    compact = _compact(text).strip(":：,，;；。()（）")
    name_marker = re.search(r"(?:名称|称)[:：]", compact)
    if name_marker:
        return compact[:name_marker.start()].strip(":：,，;；。()（）")
    if ":" in compact or "：" in compact:
        return re.split(r"[:：]", compact, maxsplit=1)[0].strip(":：,，;；。()（）")
    return compact


def _party_role(text: str) -> str | None:
    compact = _compact(text)
    if any(marker in compact for marker in NON_PARTY_MARKERS):
        return None

    exact_roles = {
        role for role, labels in PARTY_LABELS.items()
        if any(label in compact for label in labels)
    }
    if len(exact_roles) == 1:
        return next(iter(exact_roles))
    if exact_roles:
        return None

    # OCR may drop a character from a short heading. Fuzzy matching is limited
    # to the heading portion and accepted only when one canonical role wins.
    prefix = _label_prefix(text)
    if not prefix:
        return None
    distances = {
        role: min(_edit_distance(prefix, label) for label in labels)
        for role, labels in PARTY_LABELS.items()
    }
    best_distance = min(distances.values())
    winners = [role for role, distance in distances.items() if distance == best_distance]
    return winners[0] if best_distance <= 1 and len(winners) == 1 else None


def _field_value(text: str) -> str | None:
    match = re.match(r"\s*(?:名\s*称|称)\s*[:：]\s*(.+)$", text)
    if not match:
        return None
    value = match.group(1)
    for label in PARTY_METADATA_LABELS:
        value = value.split(label, 1)[0]
    value = value.strip(":：,，;；。()（）")
    return value or None


def _explicit_role_value(text: str, role: str) -> str | None:
    separator = re.search(r"[:：]", text)
    if not separator or _party_role(text[:separator.start()]) != role:
        return None
    value = text[separator.end():].strip()
    name_match = re.match(r"(?:名\s*称|称)\s*[:：]?\s*(.*)$", value)
    if name_match:
        value = name_match.group(1)
    for label in PARTY_METADATA_LABELS:
        value = value.split(label, 1)[0]
    value = value.strip(":：,，;；。()（）")
    return value or None


def _position(line: dict) -> tuple[float, float] | None:
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


def _one_unambiguous_value(values: list[tuple[str, dict]]) -> tuple[str | None, dict | None]:
    distinct = {value for value, _ in values}
    if len(distinct) != 1:
        return None, None
    value = next(iter(distinct))
    evidence = next(line for candidate, line in values if candidate == value)
    return value, evidence


def match_party_names(lines: list[dict]) -> dict[str, tuple[str | None, dict | None]]:
    """Map OCR party labels and nearby name values to canonical buyer/seller fields.

    Exact or one-character-near party labels are interpreted only in their
    OCR line and nearby page region. Ambiguous names remain empty. Results keep
    the OCR line that supplied each name as evidence.
    """
    headings: dict[str, list[tuple[int, dict]]] = {"buyer": [], "seller": []}
    assigned: dict[str, list[tuple[str, dict]]] = {"buyer": [], "seller": []}
    unassigned_names: list[tuple[int, str, dict]] = []

    for index, line in enumerate(lines):
        text = str(line.get("text") or "")
        role = _party_role(text)
        value = _field_value(text)
        if value:
            if role:
                assigned[role].append((value, line))
            else:
                unassigned_names.append((index, value, line))
            continue

        if role:
            explicit_value = _explicit_role_value(text, role)
            if explicit_value:
                assigned[role].append((explicit_value, line))
            else:
                headings[role].append((index, line))

    for _, value, line in unassigned_names:
        position = _position(line)
        if position is None:
            continue
        distances = {}
        for role, role_headings in headings.items():
            candidates = [
                (abs(position[0] - heading_position[0])
                 + 2 * abs(position[1] - heading_position[1]), heading_position)
                for _, heading in role_headings
                if heading.get("page") == line.get("page")
                and (heading_position := _position(heading)) is not None
            ]
            if candidates:
                distances[role] = min(candidates, key=lambda candidate: candidate[0])
        if len(distances) != 2:
            continue
        ordered = sorted(distances.items(), key=lambda item: item[1])
        nearest_role, (nearest_distance, nearest_heading) = ordered[0]
        _, (second_distance, second_heading) = ordered[1]
        separation = abs(nearest_heading[0] - second_heading[0]) \
            + 2 * abs(nearest_heading[1] - second_heading[1])
        if second_distance - nearest_distance < max(12, separation * 0.15):
            continue
        assigned[nearest_role].append((value, line))

    # Text-only OCR still has an ordering. Use it only when both party headings
    # and exactly two names are present, so unrelated names are not guessed.
    if len(unassigned_names) == 2 and not any(assigned.values()):
        role_order = sorted(
            (entries[0] for entries in headings.values() if len(entries) == 1),
            key=lambda entry: entry[0],
        )
        if len(role_order) == 2 and {
                _party_role(str(line.get("text") or "")) for _, line in role_order
        } == {"buyer", "seller"}:
            name_order = sorted(unassigned_names, key=lambda entry: entry[0])
            for (_, role_line), (_, value, name_line) in zip(role_order, name_order):
                role = _party_role(str(role_line.get("text") or ""))
                if role:
                    assigned[role].append((value, name_line))

    return {role: _one_unambiguous_value(values) for role, values in assigned.items()}
