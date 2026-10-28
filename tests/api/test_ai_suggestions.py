import json
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.error import HTTPError, URLError
from io import BytesIO

import pytest

from ai.suggestions import (
    DeepSeekAccountingSuggestionProvider,
    SuggestionProviderError,
    VoucherSuggestionContext,
    SuggestedAccount,
)

INVOICE_ID = "11111111-1111-4111-8111-111111111111"
RECEIPT_ID = "22222222-2222-4222-8222-222222222222"
UNRELATED_ID = "33333333-3333-4333-8333-333333333333"


def context():
    return VoucherSuggestionContext(
        business_type="PURCHASE",
        confirmed_facts={
            "business_type": "PURCHASE",
            "currency": "CNY",
            "net_amount": "40000.00",
            "tax_amount": "5200.00",
            "gross_amount": "45200.00",
            "item_category": "resale_inventory",
        },
        reconciliation_codes=("AMOUNT_MATCHED", "PAYMENT_CLEARED"),
        available_accounts=(
            SuggestedAccount("1405", "库存商品"),
            SuggestedAccount("222101", "进项税额"),
            SuggestedAccount("1002", "银行存款"),
        ),
        policy_summary={"policy_ref": "fixture-purchase-v1", "tax_treatment": "DEDUCTIBLE"},
    )


def valid_completion():
    content = {
        "lines": [
            {"account_code": "1405", "debit": "40000.00", "credit": "0.00",
             "description": "采购入库", "rationale": "商品用于销售", "evidence_refs": [INVOICE_ID]},
            {"account_code": "222101", "debit": "5200.00", "credit": "0.00",
             "description": "可抵扣进项税", "rationale": "按已确认税务政策", "evidence_refs": [INVOICE_ID]},
            {"account_code": "1002", "debit": "0.00", "credit": "45200.00",
             "description": "银行付款", "rationale": "已核对付款证据", "evidence_refs": [RECEIPT_ID]},
        ],
        "policy_explanation": "依据已确认的可抵扣政策，库存净额与进项税分别列示。",
        "warnings": [],
    }
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}


def test_deepseek_provider_sends_only_allowlisted_structured_data_and_parses_suggestion():
    requests = []

    def post_json(url, headers, payload, timeout):
        requests.append((url, headers, payload, timeout))
        return valid_completion()

    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret",
        base_url="https://deepseek.test/v1",
        model="test-model",
        http_post=post_json,
    )
    suggestion = provider.suggest(
        net=Decimal("40000.00"),
        tax=Decimal("5200.00"),
        gross=Decimal("45200.00"),
        evidence_refs=(INVOICE_ID, RECEIPT_ID),
        policy={"policy_ref": "fixture-purchase-v1"},
        context=context(),
    )

    assert suggestion.provider == "DEEPSEEK:test-model"
    assert suggestion.policy_ref == "fixture-purchase-v1"
    assert [line.account_code for line in suggestion.lines] == ["1405", "222101", "1002"]
    assert suggestion.lines[0].rationale == "商品用于销售"
    assert suggestion.policy_explanation.startswith("依据已确认")
    assert suggestion.trace is not None
    assert suggestion.trace.prompt_version == "voucher-suggestion-v1"
    assert suggestion.trace.call_status == "SUCCEEDED"
    assert suggestion.trace.validation_status == "PASSED"
    assert suggestion.trace.input_digest and suggestion.trace.output_digest
    assert suggestion.trace.output_summary["line_count"] == 3
    assert requests[0][0] == "https://deepseek.test/v1/chat/completions"
    assert requests[0][1]["Authorization"] == "Bearer test-secret"
    sent_prompt = requests[0][2]["messages"][1]["content"]
    assert INVOICE_ID in sent_prompt
    assert "bank_account_number" not in sent_prompt
    assert requests[0][2]["response_format"] == {"type": "json_object"}
    assert requests[0][3] > 0


def test_deepseek_provider_requires_credentials_and_context():
    provider = DeepSeekAccountingSuggestionProvider(api_key="", base_url="", http_post=lambda *_a, **_k: {})

    with pytest.raises(SuggestionProviderError, match="未配置") as unconfigured:
        provider.suggest(
            net=Decimal("1.00"), tax=Decimal("0.00"), gross=Decimal("1.00"),
            evidence_refs=(), policy={}, context=context(),
        )
    assert unconfigured.value.trace.call_status == "NOT_STARTED"
    assert unconfigured.value.trace.input_digest is None

    configured = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url="https://deepseek.test", http_post=lambda *_a, **_k: {}
    )
    with pytest.raises(SuggestionProviderError, match="输入上下文") as missing_context:
        configured.suggest(
            net=Decimal("1.00"), tax=Decimal("0.00"), gross=Decimal("1.00"),
            evidence_refs=(), policy={},
        )
    assert missing_context.value.trace.call_status == "NOT_STARTED"
    assert missing_context.value.trace.input_digest is None


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda value: value["lines"][0].update(account_code="9999"), "不可用科目"),
        (lambda value: value["lines"][0].update(evidence_refs=[UNRELATED_ID]), "未授权证据引用"),
        (lambda value: value["lines"][0].update(debit="NaN"), "十进制金额"),
    ],
)
def test_deepseek_provider_rejects_untrusted_or_invalid_suggestion(mutate, message):
    response = valid_completion()
    parsed = json.loads(response["choices"][0]["message"]["content"])
    mutate(parsed)
    response["choices"][0]["message"]["content"] = json.dumps(parsed)
    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url="https://deepseek.test", http_post=lambda *_a, **_k: response
    )

    with pytest.raises(SuggestionProviderError, match=message):
        provider.suggest(
            net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
            evidence_refs=(INVOICE_ID, RECEIPT_ID), policy={"policy_ref": "fixture-purchase-v1"}, context=context(),
        )


def test_deepseek_provider_maps_transport_and_invalid_json_failures():
    arguments = dict(
        net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
        evidence_refs=(INVOICE_ID, RECEIPT_ID), policy={"policy_ref": "fixture-purchase-v1"}, context=context(),
    )

    for transport_error in (TimeoutError("private transport detail"), URLError("private connection detail")):
        def fail_transport(*_args, **_kwargs):
            raise transport_error

        provider = DeepSeekAccountingSuggestionProvider(
            api_key="test-secret", base_url="https://deepseek.test",
            http_post=fail_transport,
        )
        with pytest.raises(SuggestionProviderError, match="暂不可用") as unavailable_error:
            provider.suggest(**arguments)
        assert "private" not in str(unavailable_error.value)
        assert unavailable_error.value.trace.call_status == "FAILED"

    invalid_json_provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret",
        base_url="https://deepseek.test",
        http_post=lambda *_args, **_kwargs: {
            "choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]
        },
    )
    with pytest.raises(SuggestionProviderError, match="结构无效"):
        invalid_json_provider.suggest(**arguments)


def test_deepseek_provider_refuses_fields_outside_the_minimized_input_contract():
    calls = []
    unsafe_context = context()
    unsafe_context.confirmed_facts["bank_account_number"] = "6222000000000000"
    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret",
        base_url="https://deepseek.test",
        http_post=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(SuggestionProviderError, match="未允许的字段") as invalid_context:
        provider.suggest(
            net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
            evidence_refs=(INVOICE_ID, RECEIPT_ID), policy={"policy_ref": "fixture-purchase-v1"},
            context=unsafe_context,
        )
    assert calls == []
    assert invalid_context.value.trace.call_status == "NOT_STARTED"
    assert invalid_context.value.trace.input_digest is None


def test_deepseek_provider_rejects_non_opaque_evidence_ids_before_sending():
    calls = []
    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url="https://deepseek.test", http_post=lambda *args: calls.append(args)
    )

    with pytest.raises(SuggestionProviderError, match="来源 ID"):
        provider.suggest(
            net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
            evidence_refs=("raw contract text with sensitive data",),
            policy={"policy_ref": "fixture-purchase-v1"}, context=context(),
        )
    assert calls == []


def test_deepseek_provider_requires_https_for_remote_hosts_and_string_amounts():
    response = valid_completion()
    parsed = json.loads(response["choices"][0]["message"]["content"])
    parsed["lines"][0]["debit"] = 40000.0
    response["choices"][0]["message"]["content"] = json.dumps(parsed)
    arguments = dict(
        net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
        evidence_refs=(INVOICE_ID, RECEIPT_ID), policy={"policy_ref": "fixture-purchase-v1"}, context=context(),
    )

    insecure = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url="http://deepseek.test", http_post=lambda *_a, **_k: valid_completion()
    )
    with pytest.raises(SuggestionProviderError, match="必须使用 HTTPS") as insecure_failure:
        insecure.suggest(**arguments)
    assert insecure_failure.value.trace.call_status == "NOT_STARTED"
    assert insecure_failure.value.trace.input_digest

    numeric_output = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url="https://deepseek.test", http_post=lambda *_a, **_k: response
    )
    with pytest.raises(SuggestionProviderError, match="金额必须是十进制字符串") as invalid_amount:
        numeric_output.suggest(**arguments)
    assert invalid_amount.value.trace.validation_status == "FAILED"


def test_deepseek_context_rejects_non_string_reconciliation_codes():
    invalid_context = replace(context(), reconciliation_codes=(None,))
    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url="https://deepseek.test", http_post=lambda *_a, **_k: valid_completion()
    )

    with pytest.raises(SuggestionProviderError, match="核对代码"):
        provider.suggest(
            net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
            evidence_refs=(INVOICE_ID, RECEIPT_ID), policy={"policy_ref": "fixture-purchase-v1"},
            context=invalid_context,
        )


@pytest.mark.parametrize(
    "status, message",
    [(401, "API Key"), (429, "限流")],
)
def test_deepseek_provider_reports_http_rejections_without_response_body(status, message):
    def reject(*_args, **_kwargs):
        raise HTTPError("https://deepseek.test", status, "private response", {}, BytesIO(b"secret body"))

    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url="https://deepseek.test", http_post=reject
    )

    with pytest.raises(SuggestionProviderError, match=message) as failure:
        provider.suggest(
            net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
            evidence_refs=(INVOICE_ID, RECEIPT_ID),
            policy={"policy_ref": "fixture-purchase-v1"}, context=context(),
        )
    assert "secret body" not in str(failure.value)
    assert failure.value.trace.call_status == "FAILED"


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "stop", "message": {"refusal": "private refusal details", "content": None}},
        {"finish_reason": "content_filter", "message": {"content": None}},
    ],
)
def test_deepseek_provider_reports_model_refusal_distinctly(choice):
    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret",
        base_url="https://deepseek.test",
        http_post=lambda *_args, **_kwargs: {"choices": [choice]},
    )

    with pytest.raises(SuggestionProviderError, match="拒绝了本次凭证建议") as failure:
        provider.suggest(
            net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
            evidence_refs=(INVOICE_ID, RECEIPT_ID),
            policy={"policy_ref": "fixture-purchase-v1"}, context=context(),
        )
    assert "private refusal details" not in str(failure.value)
    assert failure.value.trace.validation_status == "FAILED"


def test_deepseek_transport_does_not_forward_api_key_through_redirects():
    class TargetHandler(BaseHTTPRequestHandler):
        called = False

        def do_POST(self):
            type(self).called = True
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = threading.Thread(target=target_server.serve_forever, daemon=True)
    target_thread.start()
    target_url = f"http://127.0.0.1:{target_server.server_port}/capture"

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, *_args):
            pass

    redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect_server.serve_forever, daemon=True)
    redirect_thread.start()
    provider = DeepSeekAccountingSuggestionProvider(
        api_key="test-secret", base_url=f"http://127.0.0.1:{redirect_server.server_port}"
    )
    try:
        with pytest.raises(SuggestionProviderError, match="HTTP 302"):
            provider.suggest(
                net=Decimal("40000.00"), tax=Decimal("5200.00"), gross=Decimal("45200.00"),
                evidence_refs=(INVOICE_ID, RECEIPT_ID),
                policy={"policy_ref": "fixture-purchase-v1"}, context=context(),
            )
        assert not TargetHandler.called
    finally:
        redirect_server.shutdown()
        target_server.shutdown()
        redirect_server.server_close()
        target_server.server_close()
        redirect_thread.join(timeout=2)
        target_thread.join(timeout=2)
