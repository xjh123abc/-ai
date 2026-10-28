"""Canonical values for the bank settlement state."""

BANK_SETTLEMENT_STATES = frozenset({"SETTLED", "NOT_SETTLED", "UNKNOWN"})


def normalize_bank_settlement_state(value: object) -> str | None:
    """Normalize current and legacy settlement values to the three-state model."""
    if value is True:
        return "SETTLED"
    if value is False:
        return "NOT_SETTLED"
    if value is None:
        return "UNKNOWN"
    if not isinstance(value, str):
        return None

    normalized = value.strip().upper()
    if normalized in BANK_SETTLEMENT_STATES:
        return normalized
    if normalized == "TRUE":
        return "SETTLED"
    if normalized == "FALSE":
        return "NOT_SETTLED"
    if not normalized:
        return "UNKNOWN"
    return None
