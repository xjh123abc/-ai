"""Structured accounting suggestion boundary. OCR and suggestions never assert accounting facts."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class SuggestedLine:
    account_code: str
    account_name: str
    debit: Decimal
    credit: Decimal
    description: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class VoucherSuggestion:
    provider: str
    lines: tuple[SuggestedLine, ...]
    policy_ref: str
    warnings: tuple[str, ...]


class AccountingSuggestionProvider(Protocol):
    def suggest(self, *, net: Decimal, tax: Decimal, gross: Decimal,
                evidence_refs: tuple[str, ...], policy: dict) -> VoucherSuggestion: ...


class FixtureRuleSuggestionProvider:
    """Only supports the explicitly approved 45,200 CNY demonstration fixture."""

    def suggest(self, *, net: Decimal, tax: Decimal, gross: Decimal,
                evidence_refs: tuple[str, ...], policy: dict) -> VoucherSuggestion:
        if policy.get("fixture_only") is not True or policy.get("business_use") != "resale_inventory":
            raise ValueError("本地规则 provider 只接受显式 fixture 政策")
        if gross != Decimal("45200.00") or net != Decimal("40000.00") or tax != Decimal("5200.00"):
            raise ValueError("测试规则只支持已标注的 45,200 元样例金额")
        refs=tuple(sorted(set(evidence_refs)))
        return VoucherSuggestion(provider="LOCAL_FIXTURE_RULES", policy_ref="fixture-purchase-v1",
            warnings=("规则候选，不是 AI 意见；仅用于测试企业与测试会计政策。",), lines=(
                SuggestedLine("1405","库存商品",net,Decimal("0.00"),"采购入库付款（测试规则候选）",refs),
                SuggestedLine("222101","应交税费—应交增值税（进项税额）",tax,Decimal("0.00"),"采购入库付款（测试规则候选）",refs),
                SuggestedLine("1002","银行存款",Decimal("0.00"),gross,"采购入库付款（测试规则候选）",refs),
            ))
