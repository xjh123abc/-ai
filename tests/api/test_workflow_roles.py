from uuid import uuid4
from decimal import Decimal
from io import BytesIO

from fastapi.testclient import TestClient
import pytest
import pymupdf
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AccountSet,
    AccountingPeriod,
    AppUser,
    BusinessEvent,
    ChartAccount,
    DocumentAsset,
    DocumentExtraction,
    Tenant,
    TenantMembership,
)
from app.db.session import db_session
from app.main import app
from app.core.config import settings
from app.routers import workflow
from app.security import hash_password
from ai.suggestions import (ConfiguredAccountingSuggestionProvider, FixtureRuleSuggestionProvider,
    SuggestedLine, SuggestionProviderError, SuggestionTrace, VoucherSuggestion)


class InjectedWorkflowSuggestionProvider:
    """Explicit test-only provider; never used by the application default path."""
    def suggest(self, *, net, tax, gross, evidence_refs, policy, context=None):
        if context.business_type == "PURCHASE":
            result = FixtureRuleSuggestionProvider().suggest(net=net, tax=tax, gross=gross,
                evidence_refs=evidence_refs, policy={"fixture_only": True, "business_use": "resale_inventory"})
        else:
            result = ConfiguredAccountingSuggestionProvider().suggest(net=net, tax=tax, gross=gross,
                evidence_refs=evidence_refs, policy={**context.policy_summary,
                    "debit_account_code": context.policy_summary["debit_account_code"],
                    "bank_account_code": context.policy_summary["bank_account_code"],
                    "input_tax_account_code": context.policy_summary.get("input_tax_account_code", ""),
                    "policy_ref": context.policy_summary["policy_ref"]})
        return VoucherSuggestion(provider="TEST_ONLY_INJECTED", policy_ref=result.policy_ref,
            warnings=("仅测试注入 provider",), policy_explanation="仅用于自动化测试",
            trace=SuggestionTrace(provider="TEST_ONLY_INJECTED", model_version="test-model",
                prompt_version="test-prompt-v1", input_digest="a" * 64,
                output_digest="b" * 64, call_status="SUCCEEDED", validation_status="PASSED"),
            lines=tuple(SuggestedLine(line.account_code, line.account_name, line.debit, line.credit,
                line.description, line.evidence_refs, "自动化测试注入建议") for line in result.lines))


@pytest.fixture(params=[1, 2])
def workflow_client(request):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db_session():
        with TestSession() as db:
            yield db

    app.dependency_overrides[db_session] = override_db_session
    app.dependency_overrides[workflow.get_accounting_suggestion_provider] = InjectedWorkflowSuggestionProvider
    with TestSession.begin() as db:
        tenant = Tenant(name="workflow test", is_demo=False)
        db.add(tenant)
        db.flush()
        account_set = AccountSet(tenant_id=tenant.id, name="test account", currency="CNY")
        db.add(account_set)
        db.flush()
        db.add(AccountingPeriod(
            account_set_id=account_set.id,
            period="2024-12",
            status="OPEN",
            updated_by="workflow-test",
        ))
        db.add_all([
            ChartAccount(account_set_id=account_set.id, code="1002", name="银行存款"),
            ChartAccount(account_set_id=account_set.id, code="1405", name="库存商品"),
            ChartAccount(account_set_id=account_set.id, code="222101", name="进项税额"),
        ])
        user = AppUser(
            email=f"workflow-{uuid4().hex}@example.test",
            display_name="Workflow test",
            password_hash=hash_password("Test-password-for-workflow-2026"),
        )
        business_user = AppUser(
            email=f"business-{uuid4().hex}@example.test",
            display_name="Business user",
            password_hash=hash_password("Test-password-for-workflow-2026"),
        )
        db.add_all([user, business_user])
        db.flush()
        db.add(TenantMembership(
            user_id=user.id,
            tenant_id=tenant.id,
            account_set_id=account_set.id,
            role="TENANT_ADMIN",
        ))
        db.add(TenantMembership(
            user_id=business_user.id,
            tenant_id=tenant.id,
            account_set_id=account_set.id,
            role="BUSINESS_USER",
        ))
        documents = []
        invoice_count = request.param
        document_types = ("VAT_INVOICE",) * invoice_count + ("GOODS_RECEIPT", "BANK_RECEIPT")
        for index, document_type in enumerate(document_types, start=1):
            document = DocumentAsset(
                tenant_id=tenant.id,
                account_set_id=account_set.id,
                sha256=f"{index:064x}",
                original_filename=f"document-{index}.jpg",
                mime_type="image/jpeg",
                byte_size=10,
                storage_key=f"/tmp/document-{index}.jpg",
                uploaded_by=user.email,
                processing_status="CONFIRMED",
            )
            db.add(document)
            db.flush()
            if document_type == "VAT_INVOICE":
                net_amount = Decimal("40000.00") / invoice_count
                tax_amount = Decimal("5200.00") / invoice_count
                gross_amount = net_amount + tax_amount
                fields = {
                    "invoice_number": {"normalized_value": f"INV-{index}"},
                    "issue_date": {"normalized_value": "2024-12-01"},
                    "buyer": {"normalized_value": "Buyer Ltd"},
                    "seller": {"normalized_value": "Seller Ltd"},
                    "net_amount": {"normalized_value": f"{net_amount:.2f}"},
                    "tax_amount": {"normalized_value": f"{tax_amount:.2f}"},
                    "gross_amount": {"normalized_value": f"{gross_amount:.2f}"},
                }
            elif document_type == "GOODS_RECEIPT":
                fields = {
                    "receipt_date": {"normalized_value": "2024-12-01"},
                    "received": {"normalized_value": True},
                    "gross_amount": {"normalized_value": "45200.00"},
                }
            else:
                fields = {
                    "settlement_date": {"normalized_value": "2024-12-01"},
                    "settled": {"normalized_value": True},
                    "payer": {"normalized_value": "Buyer Ltd"},
                    "payee": {"normalized_value": "Seller Ltd"},
                    "gross_amount": {"normalized_value": "45200.00"},
                }
            db.add(DocumentExtraction(
                document_id=document.id,
                version=1,
                document_type=document_type,
                full_text="confirmed fixture text",
                fields=fields,
                lines=[],
                provider="fixture",
                model_version="fixture-v1",
                status="CONFIRMED",
                created_by=user.email,
            ))
            documents.append(document.id)
        email = user.email
        business_email = business_user.email

    client = TestClient(app)
    login = client.post("/api/v1/auth/login", json={
        "email": email,
        "password": "Test-password-for-workflow-2026",
    })
    assert login.status_code == 200
    client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
    try:
        yield client, documents, invoice_count, business_email
    finally:
        client.close()
        app.dependency_overrides.pop(db_session, None)
        app.dependency_overrides.pop(workflow.get_accounting_suggestion_provider, None)
        Base.metadata.drop_all(engine)
        engine.dispose()


def _matched_purchase_event(client, document_ids, invoice_count):
    created = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": ["INVOICE"] * invoice_count + ["GOODS_RECEIPT", "BANK_RECEIPT"],
        "reason": "准备验证凭证安全门槛",
    })
    assert created.status_code == 200
    event_id = created.json()["id"]
    reconciled = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "MATCHED"
    return event_id


def test_business_event_reconciles_and_drafts_multiple_invoices(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    document_page = client.get("/api/v1/documents?page=2&page_size=1").json()
    assert document_page["page"] == 2
    assert document_page["page_size"] == 1
    assert document_page["total"] == len(document_ids)
    assert document_page["unlinked_total"] == len(document_ids)
    assert document_page["status_counts"] == {"CONFIRMED": len(document_ids)}
    assert len(document_page["items"]) == 1
    document_summaries = client.get("/api/v1/documents?page=1&page_size=100").json()["items"]
    invoice_summary = next(item for item in document_summaries if item["id"] == document_ids[0])
    assert invoice_summary["document_type"] == "VAT_INVOICE"
    assert invoice_summary["uploaded_by"].endswith("@example.test")
    assert invoice_summary["key_amount"] in {"22600.00", "45200.00"}
    assert "gross_amount" not in invoice_summary["missing_fields"]
    assert invoice_summary["business_events"] == []

    created = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": ["INVOICE"] * invoice_count + ["GOODS_RECEIPT", "BANK_RECEIPT"],
        "reason": "多张发票属于同一笔采购业务",
    })
    assert created.status_code == 200

    event_id = created.json()["id"]
    reconciled = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "MATCHED", reconciled.json()["findings"]
    assert reconciled.json()["amount"] == "45200.00"
    assert reconciled.json()["rule_version"] == "fixture-purchase-v1"
    linked_invoice = next(item for item in client.get("/api/v1/documents?page=1&page_size=100").json()["items"]
                          if item["id"] == document_ids[0])
    assert {item["id"] for item in linked_invoice["business_events"]} == {event_id}
    assert linked_invoice["business_events"][0]["role"] == "INVOICE"

    draft = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert draft.status_code == 200
    assert draft.json()["debit_total"] == "45200.00"
    assert draft.json()["credit_total"] == "45200.00"
    assert draft.json()["policy"]["kind"] == "AI_CANDIDATE"
    assert draft.json()["policy"]["provider"] == "TEST_ONLY_INJECTED"
    assert draft.json()["policy"]["model"] == "test-model"
    assert draft.json()["policy"]["input_snapshot_hash"] == "a" * 64
    assert draft.json()["policy"]["output_hash"] == "b" * 64
    assert draft.json()["policy"]["source_snapshot_hash"]
    assert {item["document_id"] for item in draft.json()["policy"]["source_snapshot"]} == set(document_ids)
    lines_by_account = {line["account_code"]: line for line in draft.json()["lines"]}
    assert lines_by_account["1405"]["debit"] == "40000.00"
    assert lines_by_account["222101"]["debit"] == "5200.00"
    assert lines_by_account["1002"]["credit"] == "45200.00"
    assert lines_by_account["1405"]["rationale"] == "自动化测试注入建议"
    assert lines_by_account["1405"]["evidence_refs"]
    audit_rows = client.get(f"/api/v1/voucher-drafts/{draft.json()['id']}/audit").json()
    assert audit_rows[-1]["event_type"] == "VOUCHER_AI_CANDIDATE_CREATED"
    assert set(audit_rows[-1]["source_refs"]) == set(document_ids)
    assert audit_rows[-1]["after_hash"]
    session_generator = app.dependency_overrides[db_session]()
    db = next(session_generator)
    try:
        invoice = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_ids[0]))
        next_fields = dict(invoice.fields)
        next_fields["gross_amount"] = {"normalized_value": "99999.00"}
        db.add(DocumentExtraction(document_id=invoice.document_id, version=invoice.version + 1,
            document_type=invoice.document_type, full_text=invoice.full_text, fields=next_fields,
            lines=invoice.lines, provider=invoice.provider, model_version=invoice.model_version,
            status="CONFIRMED", created_by=invoice.created_by))
        db.commit()
    finally:
        session_generator.close()
    historical_evidence = client.get(f"/api/v1/documents/{document_ids[0]}?version=1")
    assert historical_evidence.status_code == 200
    assert historical_evidence.json()["extraction"]["version"] == 1
    assert historical_evidence.json()["extraction"]["fields"]["gross_amount"]["normalized_value"] in {"45200.00", "22600.00"}
    voucher_page = client.get("/api/v1/voucher-drafts?page=1&page_size=1").json()
    assert voucher_page["total"] == 1
    assert voucher_page["status_counts"] == [{"period": "2024-12", "status": "VALIDATED", "count": 1}]


@pytest.mark.parametrize(("failure_message", "call_status"), [
    ("DeepSeek 请求超时", "TIMEOUT"),
    ("DeepSeek 返回凭证候选结构无效", "SUCCEEDED"),
])
def test_ai_provider_failure_is_audited_and_never_creates_draft(workflow_client, failure_message, call_status):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)

    class FailingProvider:
        def suggest(self, **kwargs):
            raise SuggestionProviderError(failure_message, trace=SuggestionTrace(
                provider="DEEPSEEK", model_version="test-model", prompt_version="voucher-suggestion-v1",
                input_digest="a" * 64, output_digest=None, call_status=call_status, validation_status="FAILED"))

    try:
        app.dependency_overrides[workflow.get_accounting_suggestion_provider] = FailingProvider
        response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
        assert response.status_code == 503
        assert response.json()["detail"] == failure_message
        assert client.get("/api/v1/voucher-drafts?page=1&page_size=10").json()["total"] == 0
        session_generator = app.dependency_overrides[db_session]()
        db = next(session_generator)
        try:
            failure = db.scalar(select(workflow.AuditEvent).where(
                workflow.AuditEvent.entity_ref == event_id,
                workflow.AuditEvent.event_type == "VOUCHER_AI_GENERATION_FAILED"))
            assert failure is not None
            assert failure.after_hash
        finally:
            session_generator.close()
    finally:
        app.dependency_overrides[workflow.get_accounting_suggestion_provider] = InjectedWorkflowSuggestionProvider


def test_unconfirmed_linked_evidence_version_blocks_ai_draft(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    session_generator = app.dependency_overrides[db_session]()
    db = next(session_generator)
    try:
        linked_proof = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_ids[-1]))
        linked_proof.status = "NEEDS_REVIEW"
        db.commit()
    finally:
        session_generator.close()
    response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert response.status_code == 409
    assert "确认版本不可用" in response.json()["detail"]
    assert client.get("/api/v1/voucher-drafts?page=1&page_size=10").json()["total"] == 0


def test_upload_validates_magic_mime_size_and_deduplicates_content(workflow_client, monkeypatch, tmp_path):
    client, _, _, _ = workflow_client
    monkeypatch.setattr(workflow.settings, "storage_dir", tmp_path)
    monkeypatch.setattr(workflow.settings, "max_upload_bytes", 64)
    extraction_calls = []

    def fake_extract(path):
        extraction_calls.append(path)
        return {"full_text": "网上银行电子回单", "lines": [
            {"text": "网上银行电子回单", "confidence": 0.0, "page": 1, "bbox": [1, 2, 30, 12]},
        ], "fields": {
            "document_type_candidate": {"normalized_value": "BANK_RECEIPT", "confidence": 0.5,
                "evidence_confidence": 0.0, "status": "NEEDS_REVIEW"},
            "invoice_number": {"normalized_value": "12345678", "confidence": 0.45},
            "net_amount": {"normalized_value": "45200.00", "confidence": 0.45},
            "gross_amount": {"normalized_value": "45200.00", "confidence": 0.45},
        }, "provider": "fixture", "model_version": "fixture-v1"}

    monkeypatch.setattr(workflow, "extract_image", fake_extract)
    png_bytes = b"\x89PNG\r\n\x1a\nfixture image"

    invalid_signature = client.post("/api/v1/documents", files={
        "file": ("not-image.png", b"not an image", "image/png"),
    })
    wrong_declared_type = client.post("/api/v1/documents", files={
        "file": ("mismatch.png", png_bytes, "image/jpeg"),
    })
    oversized = client.post("/api/v1/documents", files={
        "file": ("large.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64, "image/png"),
    })
    assert invalid_signature.status_code == 415
    assert "UNSUPPORTED_FORMAT" in invalid_signature.json()["detail"]
    assert wrong_declared_type.status_code == 415
    assert oversized.status_code == 413

    first = client.post("/api/v1/documents", files={
        "file": ("first.png", png_bytes, "image/png"),
    })
    duplicate = client.post("/api/v1/documents", files={
        "file": ("renamed-copy.png", png_bytes, "image/png"),
    })

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    extraction = first.json()["extraction"]
    assert extraction["document_type"] == "UNCLASSIFIED"
    assert "document_type_candidate" not in extraction["fields"]
    assert extraction["document_type_candidate"]["normalized_value"] == "BANK_RECEIPT"
    assert extraction["document_type_candidate"]["confidence"] == 0.5
    assert extraction["document_type_candidate"]["evidence_confidence"] == 0.0
    document_id = first.json()["id"]
    detail = client.get(f"/api/v1/documents/{document_id}").json()
    assert "document_type_candidate" not in detail["extraction"]["fields"]
    assert detail["extraction"]["document_type_candidate"]["normalized_value"] == "BANK_RECEIPT"
    assert "invoice_number" in detail["extraction"]["fields"]
    assert "net_amount" in detail["extraction"]["fields"]
    invalid_confirmation = client.patch(f"/api/v1/documents/{document_id}/extraction", json={
        "expected_version": 1, "document_type": "BANK_RECEIPT",
        "fields": {"invoice_number": "12345678"}, "reason": "拒绝跨类型字段",
    })
    assert invalid_confirmation.status_code == 422
    confirmed = client.patch(f"/api/v1/documents/{document_id}/extraction", json={
        "expected_version": 1, "document_type": "BANK_RECEIPT", "fields": {}, "reason": "人工确认单据类型",
    })
    assert confirmed.status_code == 200
    assert "document_type_candidate" not in confirmed.json()["fields"]
    assert "invoice_number" not in confirmed.json()["fields"]
    assert "net_amount" not in confirmed.json()["fields"]
    assert "gross_amount" in confirmed.json()["fields"]
    confirmed_detail = client.get(f"/api/v1/documents/{document_id}").json()
    assert "document_type_candidate" not in confirmed_detail["extraction_history"][0]["fields"]
    assert "document_type_candidate" in confirmed_detail["extraction_history"][1]
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "id": first.json()["id"], "duplicate": True, "processing_status": "NEEDS_REVIEW",
    }
    assert len(extraction_calls) == 1
    assert len(list(tmp_path.rglob("*.png"))) == 1


def test_confirmed_acceptance_date_is_normalized_to_iso(workflow_client, monkeypatch, tmp_path):
    client, _, _, _ = workflow_client
    monkeypatch.setattr(workflow.settings, "storage_dir", tmp_path)

    def fake_extract(path):
        return {"full_text": "验收报告", "lines": [], "fields": {
            "document_type_candidate": {"normalized_value": "ACCEPTANCE_REPORT", "status": "NEEDS_REVIEW"},
            "acceptance_date": {"normalized_value": None, "status": "NEEDS_REVIEW"},
        }, "provider": "fixture", "model_version": "fixture-v1"}

    monkeypatch.setattr(workflow, "extract_image", fake_extract)
    uploaded = client.post("/api/v1/documents", files={
        "file": ("acceptance.png", b"\x89PNG\r\n\x1a\nfixture image", "image/png"),
    })
    assert uploaded.status_code == 200

    confirmed = client.patch(f"/api/v1/documents/{uploaded.json()['id']}/extraction", json={
        "expected_version": 1,
        "document_type": "ACCEPTANCE_REPORT",
        "fields": {"acceptance_date": "2024年12月11日"},
        "reason": "按验收日期原文确认",
    })

    assert confirmed.status_code == 200
    assert confirmed.json()["fields"]["acceptance_date"]["normalized_value"] == "2024-12-11"


def test_oversized_pixel_upload_keeps_original_and_returns_clear_failure(
    workflow_client, monkeypatch, tmp_path
):
    from PIL import Image
    import app.ocr as ocr

    client, _, _, _ = workflow_client
    monkeypatch.setattr(workflow.settings, "storage_dir", tmp_path)
    monkeypatch.setattr(ocr, "MAX_OCR_IMAGE_PIXELS", 100)
    output = BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    payload = output.getvalue()

    uploaded = client.post("/api/v1/documents", files={
        "file": ("large-pixel-count.png", payload, "image/png"),
    })

    assert uploaded.status_code == 200
    assert uploaded.json()["processing_status"] == "FAILED"
    error = uploaded.json()["extraction"]["fields"]["error"]
    assert "像素总量超过 OCR 安全上限" in error["message"]
    original = client.get(f"/api/v1/documents/{uploaded.json()['id']}/content")
    assert original.status_code == 200
    assert original.content == payload


def test_closed_period_blocks_voucher_creation(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        period = db.scalar(select(AccountingPeriod))
        period.status = "CLOSED"
        db.commit()
    finally:
        dependency.close()

    response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")

    assert response.status_code == 422
    assert "未开放" in response.json()["detail"]
    assert client.get("/api/v1/voucher-drafts").json()["total"] == 0


def test_inactive_account_blocks_voucher_creation_without_partial_draft(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        inventory = db.scalar(select(ChartAccount).where(ChartAccount.code == "1405"))
        inventory.is_active = False
        db.commit()
    finally:
        dependency.close()

    response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")

    assert response.status_code == 422
    assert "科目不可用" in response.json()["detail"]
    assert client.get("/api/v1/voucher-drafts").json()["total"] == 0


def test_required_auxiliary_rule_candidate_stays_needs_review_until_corrected(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    invalid_configuration = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "1405", "name": "库存商品", "requires_auxiliary": True,
    }]})
    assert invalid_configuration.status_code == 422
    duplicate_dimensions = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "1405", "name": "库存商品", "requires_auxiliary": True,
        "required_auxiliary_fields": ["department", " department "],
    }]})
    assert duplicate_dimensions.status_code == 422
    configured = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "1405", "name": "库存商品", "requires_auxiliary": True,
        "required_auxiliary_fields": ["department"],
    }]})
    assert configured.status_code == 200
    account_list = client.get("/api/v1/auth/chart-accounts")
    assert account_list.status_code == 200
    assert next(row for row in account_list.json()["accounts"] if row["code"] == "1405")["required_auxiliary_fields"] == ["department"]

    response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")

    assert response.status_code == 200
    assert response.json()["status"] == "NEEDS_REVIEW"
    auxiliary_check = next(item for item in response.json()["validations"] if item["code"] == "AUXILIARY_COMPLETE")
    assert auxiliary_check["passed"] is False
    assert response.json()["lines"][0]["required_auxiliary_fields"] == ["department"]
    changed_policy = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "1405", "name": "库存商品", "requires_auxiliary": True,
        "required_auxiliary_fields": ["project"],
    }]})
    assert changed_policy.status_code == 409
    deactivated = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "1405", "name": "库存商品", "is_active": False,
        "requires_auxiliary": True, "required_auxiliary_fields": ["department"],
    }]})
    assert deactivated.status_code == 409
    made_parent = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "1405", "name": "库存商品", "is_leaf": False,
        "requires_auxiliary": True, "required_auxiliary_fields": ["department"],
    }]})
    assert made_parent.status_code == 409
    assert client.post(f"/api/v1/voucher-drafts/{response.json()['id']}/submit-review").status_code == 409
    assert client.get("/api/v1/voucher-drafts").json()["total"] == 1


@pytest.mark.parametrize(("tax_treatment", "tax_account_code", "expected_lines"), [
    ("DEDUCTIBLE", "222101", [
        ("6601", "40000.00", "0.00"), ("222101", "5200.00", "0.00"),
        ("1002", "0.00", "45200.00"),
    ]),
    ("NON_DEDUCTIBLE", None, [
        ("6601", "45200.00", "0.00"), ("1002", "0.00", "45200.00"),
    ]),
])
def test_confirmed_expense_policy_is_versioned_and_drives_a_balanced_rule_candidate(
    workflow_client, tax_treatment, tax_account_code, expected_lines,
):
    client, document_ids, invoice_count, business_email = workflow_client
    unconfirmed = client.put("/api/v1/auth/accounting-policies/EXPENSE", json={
        "debit_account_code": "6601", "bank_account_code": "1002", "tax_treatment": tax_treatment,
        "input_tax_account_code": tax_account_code, "accounting_treatment": "EXPENSE", "confirmed": False,
    })
    assert unconfirmed.status_code == 422
    accounts = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "6601", "name": "管理费用", "is_active": True, "is_leaf": True,
    }]})
    assert accounts.status_code == 200
    policy_response = client.put("/api/v1/auth/accounting-policies/EXPENSE", json={
        "debit_account_code": "6601", "bank_account_code": "1002", "tax_treatment": tax_treatment,
        "input_tax_account_code": tax_account_code, "accounting_treatment": "EXPENSE", "confirmed": True,
    })
    assert policy_response.status_code == 200
    assert policy_response.json()["version"] == 1
    replay = client.put("/api/v1/auth/accounting-policies/EXPENSE", json={
        "debit_account_code": "6601", "bank_account_code": "1002", "tax_treatment": tax_treatment,
        "input_tax_account_code": tax_account_code, "accounting_treatment": "EXPENSE", "confirmed": True,
    })
    assert replay.status_code == 200
    assert replay.json()["version"] == 1
    assert replay.json()["unchanged"] is True
    business_client = TestClient(app)
    login = business_client.post("/api/v1/auth/login", json={
        "email": business_email, "password": "Test-password-for-workflow-2026",
    })
    business_client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    denied = business_client.put("/api/v1/auth/accounting-policies/EXPENSE", json={
        "debit_account_code": "6601", "bank_account_code": "1002", "tax_treatment": tax_treatment,
        "input_tax_account_code": tax_account_code, "accounting_treatment": "EXPENSE", "confirmed": True,
    })
    assert denied.status_code == 403

    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        proof = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_ids[invoice_count]))
        proof.document_type = "PAYMENT_PROOF"
        proof.fields = {
            "payment_date": {"normalized_value": "2024-12-01"},
            "payer": {"normalized_value": "Buyer Ltd"}, "payee": {"normalized_value": "Seller Ltd"},
            "purpose": {"normalized_value": "办公费用"}, "approved": {"normalized_value": True},
            "gross_amount": {"normalized_value": "45200.00"},
        }
        bank = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_ids[invoice_count + 1]))
        bank.fields["settled"] = {"normalized_value": True}
        db.commit()
    finally:
        dependency.close()

    event_response = client.post("/api/v1/business-events", json={
        "event_type": "EXPENSE", "document_ids": document_ids,
        "roles": ["INVOICE"] * invoice_count + ["PAYMENT_PROOF", "BANK_RECEIPT"],
        "reason": "验收已确认的费用政策规则",
    })
    assert event_response.status_code == 200
    event_id = event_response.json()["id"]
    reconciliation = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert reconciliation.status_code == 200
    assert reconciliation.json()["status"] == "MATCHED"
    assert reconciliation.json()["rule_version"] == "accounting-policy:EXPENSE:v1"

    added_account = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "6602", "name": "销售费用", "is_active": True, "is_leaf": True,
    }]})
    assert added_account.status_code == 200
    revised_policy = client.put("/api/v1/auth/accounting-policies/EXPENSE", json={
        "debit_account_code": "6602", "bank_account_code": "1002", "tax_treatment": tax_treatment,
        "input_tax_account_code": tax_account_code, "accounting_treatment": "EXPENSE", "confirmed": True,
    })
    assert revised_policy.status_code == 200
    assert revised_policy.json()["version"] == 2
    stale_candidate = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert stale_candidate.status_code == 409
    rerun = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert rerun.status_code == 200
    assert rerun.json()["rule_version"] == "accounting-policy:EXPENSE:v2"

    candidate = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert candidate.status_code == 200
    payload = candidate.json()
    assert payload["status"] == "VALIDATED"
    assert payload["policy"]["fixture_only"] is False
    assert payload["policy"]["policy_ref"] == "accounting-policy:EXPENSE:v2"
    assert payload["debit_total"] == payload["credit_total"] == "45200.00"
    expected_lines[0] = ("6602", expected_lines[0][1], expected_lines[0][2])
    assert [(line["account_code"], line["debit"], line["credit"]) for line in payload["lines"]] == expected_lines
    replay_after_draft = client.put("/api/v1/auth/accounting-policies/EXPENSE", json={
        "debit_account_code": "6602", "bank_account_code": "1002", "tax_treatment": tax_treatment,
        "input_tax_account_code": tax_account_code, "accounting_treatment": "EXPENSE", "confirmed": True,
    })
    assert replay_after_draft.status_code == 200
    assert replay_after_draft.json()["version"] == 2
    assert replay_after_draft.json()["unchanged"] is True
    blocked_policy_change = client.put("/api/v1/auth/accounting-policies/EXPENSE", json={
        "debit_account_code": "6601", "bank_account_code": "1002", "tax_treatment": tax_treatment,
        "input_tax_account_code": tax_account_code, "accounting_treatment": "EXPENSE", "confirmed": True,
    })
    assert blocked_policy_change.status_code == 409


@pytest.mark.parametrize(("business_type", "treatment", "debit_code", "account_name"), [
    ("ASSET_PURCHASE", "CAPITALIZE", "1601", "固定资产"),
    ("RENTAL", "DIRECT_EXPENSE", "6601", "管理费用"),
])
def test_confirmed_asset_and_rental_policies_generate_balanced_candidates(
    workflow_client, business_type, treatment, debit_code, account_name,
):
    client, document_ids, invoice_count, _ = workflow_client
    assert client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": debit_code, "name": account_name, "is_active": True, "is_leaf": True,
    }]}).status_code == 200
    saved = client.put(f"/api/v1/auth/accounting-policies/{business_type}", json={
        "debit_account_code": debit_code, "bank_account_code": "1002", "tax_treatment": "DEDUCTIBLE",
        "input_tax_account_code": "222101", "accounting_treatment": treatment, "confirmed": True,
    })
    assert saved.status_code == 200
    ids = list(document_ids[:invoice_count])
    roles = ["INVOICE"] * invoice_count
    if business_type == "RENTAL":
        dependency = app.dependency_overrides[db_session]()
        db = next(dependency)
        try:
            tenant = db.scalar(select(Tenant))
            account = db.scalar(select(AccountSet))
            contract = DocumentAsset(
                tenant_id=tenant.id, account_set_id=account.id, sha256=f"{uuid4().hex:0<64}",
                original_filename="confirmed-rental-contract.pdf", mime_type="application/pdf", byte_size=1,
                storage_key="/tmp/confirmed-rental-contract.pdf", uploaded_by="workflow-test",
                processing_status="CONFIRMED",
            )
            db.add(contract)
            db.flush()
            db.add(DocumentExtraction(
                document_id=contract.id, version=1, document_type="CONTRACT", full_text="已确认租赁合同",
                fields={"contract_number": {"normalized_value": "LEASE-001"}}, lines=[],
                provider="fixture", model_version="fixture-contract-v1", status="CONFIRMED",
                created_by="workflow-test",
            ))
            db.commit()
            ids.append(contract.id)
            roles.append("CONTRACT")
        finally:
            dependency.close()
    ids.append(document_ids[-1])
    roles.append("BANK_RECEIPT")
    event = client.post("/api/v1/business-events", json={
        "event_type": business_type, "document_ids": ids, "roles": roles,
        "reason": "验收财务负责人确认的业务政策",
        "rental_direct_expense_confirmed": business_type == "RENTAL",
    })
    assert event.status_code == 200
    event_id = event.json()["id"]
    replayed_event = client.post("/api/v1/business-events", json={
        "event_type": business_type, "document_ids": ids, "roles": roles,
        "reason": "验收财务负责人确认的业务政策",
        "rental_direct_expense_confirmed": business_type == "RENTAL",
    })
    assert replayed_event.status_code == 200
    assert replayed_event.json()["id"] == event_id
    assert replayed_event.json()["idempotent_replay"] is True
    reconciliation = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert reconciliation.status_code == 200
    if business_type == "RENTAL":
        detail = client.get(f"/api/v1/business-events/{event_id}")
        assert detail.json()["rental_direct_expense_confirmed"] is True
        assert detail.json()["rental_direct_expense_confirmed_by"]
    assert reconciliation.json()["status"] == "MATCHED"
    candidate = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert candidate.status_code == 200
    payload = candidate.json()
    assert payload["policy"]["policy_ref"] == f"accounting-policy:{business_type}:v1"
    assert payload["debit_total"] == payload["credit_total"] == "45200.00"
    assert payload["lines"][0]["account_code"] == debit_code
    if business_type == "RENTAL":
        unconfirmed = client.post("/api/v1/business-events", json={
            "event_type": business_type, "document_ids": ids, "roles": roles,
            "reason": "验证未逐笔确认租赁不得制证",
        })
        assert unconfirmed.status_code == 200
        unconfirmed_id = unconfirmed.json()["id"]
        review = client.post(f"/api/v1/business-events/{unconfirmed_id}/reconcile")
        assert review.status_code == 200
        assert review.json()["status"] == "NEEDS_REVIEW"
        assert any(finding["code"] == "RENTAL_DIRECT_EXPENSE_NOT_CONFIRMED" for finding in review.json()["findings"])
        dependency = app.dependency_overrides[db_session]()
        db = next(dependency)
        try:
            unconfirmed_event = db.get(BusinessEvent, unconfirmed_id)
            unconfirmed_event.status = "MATCHED"
            unconfirmed_event.amount = Decimal("45200.00")
            db.commit()
        finally:
            dependency.close()
        blocked = client.post(f"/api/v1/business-events/{unconfirmed_id}/voucher-drafts")
        assert blocked.status_code == 422
        assert "逐笔确认" in blocked.json()["detail"]


def test_submit_blocks_legacy_auxiliary_flag_without_configured_dimensions(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    draft = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts").json()
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        inventory = db.scalar(select(ChartAccount).where(ChartAccount.code == "1405"))
        inventory.requires_auxiliary = True
        inventory.required_auxiliary_fields = []
        db.commit()
    finally:
        dependency.close()

    response = client.post(f"/api/v1/voucher-drafts/{draft['id']}/submit-review")

    assert response.status_code == 422
    assert "尚未配置辅助核算维度" in response.json()["detail"]
    assert client.get("/api/v1/voucher-drafts").json()["status_counts"] == [
        {"period": "2024-12", "status": "VALIDATED", "count": 1},
    ]


@pytest.mark.parametrize("property_name", ["is_active", "is_leaf"])
def test_submit_rechecks_chart_account_availability(workflow_client, property_name):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    draft = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts").json()
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        inventory = db.scalar(select(ChartAccount).where(ChartAccount.code == "1405"))
        setattr(inventory, property_name, False)
        db.commit()
    finally:
        dependency.close()

    response = client.post(f"/api/v1/voucher-drafts/{draft['id']}/submit-review")

    assert response.status_code == 422
    assert "不可用或不是末级科目" in response.json()["detail"]


def test_unbalanced_provider_output_cannot_create_voucher(workflow_client, monkeypatch):
    import ai.suggestions as suggestions

    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)

    def unbalanced_suggestion(self, *, net, tax, gross, evidence_refs, policy):
        return suggestions.VoucherSuggestion(
            provider="test-provider",
            policy_ref="test-unbalanced",
            warnings=(),
            lines=(
                suggestions.SuggestedLine("1405", "库存商品", Decimal("40000.00"), Decimal("0.00"), "库存", evidence_refs),
                suggestions.SuggestedLine("222101", "进项税额", Decimal("5200.00"), Decimal("0.00"), "税额", evidence_refs),
                suggestions.SuggestedLine("1002", "银行存款", Decimal("0.00"), Decimal("45201.00"), "付款", evidence_refs),
            ),
        )

    monkeypatch.setattr(suggestions.FixtureRuleSuggestionProvider, "suggest", unbalanced_suggestion)

    response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")

    assert response.status_code == 422
    assert response.json()["detail"] == "AI 凭证金额与已确认发票金额或会计政策不一致"
    assert client.get("/api/v1/voucher-drafts").json()["total"] == 0


@pytest.mark.parametrize("violation, expected_detail", [
    ("unknown_account", "科目不可用"),
    ("unrelated_evidence", "未关联的来源单据或缺少证据引用"),
    ("amount_mismatch", "AI 凭证金额与已确认发票金额或会计政策不一致"),
])
def test_untrusted_ai_candidate_is_blocked_before_draft_persistence(workflow_client, violation, expected_detail):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)

    class UntrustedCandidateProvider:
        def suggest(self, *, net, tax, gross, evidence_refs, policy, context=None):
            lines = [
                SuggestedLine("1405", "库存商品", Decimal("40000.00"), Decimal("0.00"), "库存", evidence_refs),
                SuggestedLine("222101", "进项税额", Decimal("5200.00"), Decimal("0.00"), "税额", evidence_refs),
                SuggestedLine("1002", "银行存款", Decimal("0.00"), Decimal("45200.00"), "付款", evidence_refs),
            ]
            if violation == "unknown_account":
                lines[0] = SuggestedLine("9999", "未知科目", Decimal("40000.00"), Decimal("0.00"), "库存", evidence_refs)
            elif violation == "unrelated_evidence":
                lines[0] = SuggestedLine("1405", "库存商品", Decimal("40000.00"), Decimal("0.00"), "库存", ("unrelated-document",))
            elif violation == "amount_mismatch":
                lines[0] = SuggestedLine("1405", "库存商品", Decimal("40001.00"), Decimal("0.00"), "库存", evidence_refs)
            return VoucherSuggestion(
                provider="UNTRUSTED_TEST_PROVIDER", lines=tuple(lines), policy_ref="fixture-purchase-v1",
                warnings=(), policy_explanation="测试候选",
            )

    try:
        app.dependency_overrides[workflow.get_accounting_suggestion_provider] = UntrustedCandidateProvider
        response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    finally:
        app.dependency_overrides[workflow.get_accounting_suggestion_provider] = InjectedWorkflowSuggestionProvider

    assert response.status_code == 422
    assert expected_detail in response.json()["detail"]
    assert client.get("/api/v1/voucher-drafts").json()["total"] == 0


def test_duplicate_voucher_submission_is_rejected_and_audited(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    first = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert first.status_code == 200

    duplicate = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")

    assert duplicate.status_code == 409
    assert client.get("/api/v1/voucher-drafts").json()["total"] == 1
    audit = client.get(f"/api/v1/voucher-drafts/{first.json()['id']}/audit").json()
    assert any(entry["event_type"] == "VOUCHER_DUPLICATE_CREATION_BLOCKED" for entry in audit)


def test_self_approval_and_low_privilege_approval_are_rejected(workflow_client):
    client, document_ids, invoice_count, business_email = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)
    draft_response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert draft_response.status_code == 200
    draft_id = draft_response.json()["id"]
    assert client.post(f"/api/v1/voucher-drafts/{draft_id}/submit-review").status_code == 200
    decision = {"decision": "APPROVE", "reason": "API 权限验收"}

    self_approval = client.post(f"/api/v1/voucher-drafts/{draft_id}/approve", json=decision)
    assert self_approval.status_code == 403
    assert "制单人与审批人必须不同" in self_approval.json()["detail"]

    business_client = TestClient(app)
    business_login = business_client.post("/api/v1/auth/login", json={
        "email": business_email,
        "password": "Test-password-for-workflow-2026",
    })
    assert business_login.status_code == 200
    business_client.headers.update({"Authorization": f"Bearer {business_login.json()['access_token']}"})
    try:
        low_privilege = business_client.post(f"/api/v1/voucher-drafts/{draft_id}/approve", json=decision)
        assert low_privilege.status_code == 403
    finally:
        business_client.close()

    approver_email = f"approver-{uuid4().hex}@example.test"
    invited = client.post("/api/v1/auth/memberships", json={
        "email": approver_email,
        "display_name": "Workflow approver",
        "initial_password": "Initial-approver-password-2026!",
        "role": "APPROVER",
    })
    assert invited.status_code == 201
    approver_client = TestClient(app)
    approver_login = approver_client.post("/api/v1/auth/login", json={
        "email": approver_email,
        "password": "Initial-approver-password-2026!",
    })
    assert approver_login.status_code == 200
    approver_client.headers.update({"Authorization": f"Bearer {approver_login.json()['access_token']}"})
    try:
        approved = approver_client.post(f"/api/v1/voucher-drafts/{draft_id}/approve", json=decision)
        assert approved.status_code == 200
        assert approved.json()["status"] == "APPROVED"
        rental_confirmation = approver_client.post("/api/v1/business-events", json={
            "event_type": "RENTAL", "document_ids": [document_ids[0], document_ids[-1]],
            "roles": ["INVOICE", "BANK_RECEIPT"], "reason": "测试审批角色不能确认会计处理",
            "rental_direct_expense_confirmed": True,
        })
        assert rental_confirmation.status_code == 403
    finally:
        approver_client.close()


def test_payment_proof_does_not_replace_a_bank_receipt(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    payment_proof_id = document_ids[-1]
    confirmed_as_proof = client.patch(f"/api/v1/documents/{payment_proof_id}/extraction", json={
        "expected_version": 1,
        "document_type": "PAYMENT_PROOF",
        "fields": {"proof_number": "PAY-2024-12-001", "payment_date": "2024-12-01", "gross_amount": "45200.00"},
        "reason": "验收付款证明不能作为银行清算凭据",
    })
    assert confirmed_as_proof.status_code == 200

    created = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": ["INVOICE"] * invoice_count + ["GOODS_RECEIPT", "PAYMENT_PROOF"],
        "reason": "付款证明不能替代银行回单",
    })
    assert created.status_code == 200

    result = client.post(f"/api/v1/business-events/{created.json()['id']}/reconcile")
    assert result.status_code == 200
    role_finding = next(
        finding for finding in result.json()["findings"]
        if finding["code"] == "REQUIRED_DOCUMENT_ROLES_MISSING_OR_DUPLICATED"
    )
    assert role_finding["missing_roles"] == ["BANK_RECEIPT"]
    assert role_finding["duplicated_roles"] == []


def test_asset_purchase_waits_for_capitalization_policy(workflow_client):
    client, document_ids, _, _ = workflow_client
    created = client.post("/api/v1/business-events", json={
        "document_ids": [document_ids[0], document_ids[-1]],
        "roles": ["INVOICE", "BANK_RECEIPT"],
        "event_type": "ASSET_PURCHASE",
        "reason": "核对设备购置发票与付款回单",
    })
    assert created.status_code == 200
    event_id = created.json()["id"]

    result = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert result.status_code == 200
    assert result.json()["rule_version"] == "unconfigured-asset-v1"
    assert result.json()["status"] == "NEEDS_REVIEW"
    assert "ASSET_CAPITALIZATION_POLICY_REVIEW_REQUIRED" in {
        finding["code"] for finding in result.json()["findings"]
    }

    voucher = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert voucher.status_code == 422
    assert "尚未配置并确认账套会计政策" in voucher.json()["detail"]


def test_rental_needs_a_contract_and_configured_accounting_policy(workflow_client):
    client, document_ids, _, _ = workflow_client
    created = client.post("/api/v1/business-events", json={
        "document_ids": [document_ids[0], document_ids[-1]],
        "roles": ["INVOICE", "BANK_RECEIPT"],
        "event_type": "RENTAL",
        "reason": "核对办公室租赁发票和付款回单",
    })
    assert created.status_code == 200
    premature_confirmation = client.post("/api/v1/business-events", json={
        "document_ids": [document_ids[0], document_ids[-1]],
        "roles": ["INVOICE", "BANK_RECEIPT"],
        "event_type": "RENTAL",
        "reason": "未配置租赁直接费用政策时不得确认",
        "rental_direct_expense_confirmed": True,
    })
    assert premature_confirmation.status_code == 422
    event_id = created.json()["id"]

    result = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert result.status_code == 200
    assert result.json()["rule_version"] == "unconfigured-rental-v1"
    assert result.json()["status"] == "NEEDS_REVIEW"
    findings = {finding["code"] for finding in result.json()["findings"]}
    assert findings >= {"RENTAL_ACCOUNT_POLICY_REVIEW_REQUIRED", "RENTAL_CONTRACT_MISSING"}

    voucher = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert voucher.status_code == 422
    assert "尚未配置并确认账套会计政策" in voucher.json()["detail"]


def test_insights_summary_uses_visible_business_data_and_disclaims_unavailable_financials(workflow_client):
    client, document_ids, invoice_count, business_email = workflow_client
    event_id = _matched_purchase_event(client, document_ids, invoice_count)

    summary_response = client.get("/api/v1/insights/summary")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["event_count"] == 1
    assert summary["matched_event_count"] == 1
    assert summary["matched_event_amount"] == "45200.00"
    assert summary["open_case_count"] == 0
    assert summary["period_scope"].startswith("全部时间")
    assert summary["source_events"][0]["id"] == event_id
    assert "不等同收入" in summary["limitations"]
    assert "银行余额" in summary["limitations"]

    business_login = client.post("/api/v1/auth/login", json={
        "email": business_email,
        "password": "Test-password-for-workflow-2026",
    })
    assert business_login.status_code == 200
    restricted = client.get("/api/v1/insights/summary", headers={
        "Authorization": f"Bearer {business_login.json()['access_token']}",
    })
    assert restricted.status_code == 200
    assert restricted.json()["event_count"] == 0


def test_login_throttles_repeated_failures_for_the_same_email_and_source(workflow_client):
    client, _, _, _ = workflow_client
    email = f"throttle-{uuid4().hex}@example.test"
    payload = {"email": email, "password": "incorrect-password"}

    for _ in range(5):
        assert client.post("/api/v1/auth/login", json=payload).status_code == 401

    with TestClient(app) as second_client:
        limited = second_client.post("/api/v1/auth/login", json=payload)
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 15 * 60
    assert client.post("/api/v1/auth/login", json={**payload, "email": f"other-{uuid4().hex}@example.test"}).status_code == 401


def test_business_event_blocks_duplicate_invoice_with_case(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    if invoice_count < 2:
        pytest.skip("requires two invoice documents")
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        tenant = db.scalar(select(Tenant))
        account = db.scalar(select(AccountSet))
        source_extraction = db.scalar(select(DocumentExtraction).where(
            DocumentExtraction.document_id == document_ids[0],
        ))
        second_extraction = db.scalar(select(DocumentExtraction).where(
            DocumentExtraction.document_id == document_ids[1],
        ))
        duplicate_document = DocumentAsset(
            tenant_id=tenant.id, account_set_id=account.id, sha256=f"{uuid4().hex:0<64}",
            original_filename="duplicate-invoice-copy.pdf", mime_type="application/pdf", byte_size=1,
            storage_key="/tmp/duplicate-invoice-copy.pdf", uploaded_by="workflow-test",
            processing_status="CONFIRMED",
        )
        duplicate_document_two = DocumentAsset(
            tenant_id=tenant.id, account_set_id=account.id, sha256=f"{uuid4().hex:0<64}",
            original_filename="duplicate-invoice-second-copy.pdf", mime_type="application/pdf", byte_size=1,
            storage_key="/tmp/duplicate-invoice-second-copy.pdf", uploaded_by="workflow-test",
            processing_status="CONFIRMED",
        )
        db.add_all([duplicate_document, duplicate_document_two])
        db.flush()
        db.add(DocumentExtraction(
            document_id=duplicate_document.id, version=1, document_type="VAT_INVOICE",
            full_text=source_extraction.full_text, fields=source_extraction.fields,
            lines=source_extraction.lines, provider="fixture", model_version="fixture-copy-v1",
            status="CONFIRMED", created_by="workflow-test",
        ))
        db.add(DocumentExtraction(
            document_id=duplicate_document_two.id, version=1, document_type="VAT_INVOICE",
            full_text=second_extraction.full_text, fields=second_extraction.fields,
            lines=second_extraction.lines, provider="fixture", model_version="fixture-copy-v1",
            status="CONFIRMED", created_by="workflow-test",
        ))
        db.commit()
        duplicate_document_id = duplicate_document.id
        duplicate_document_two_id = duplicate_document_two.id
    finally:
        dependency.close()

    first_event = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": ["INVOICE", "INVOICE", "GOODS_RECEIPT", "BANK_RECEIPT"],
        "reason": "建立已经关联的原发票业务",
    })
    assert first_event.status_code == 200
    assert client.post(f"/api/v1/business-events/{first_event.json()['id']}/reconcile").json()["status"] == "MATCHED"

    created = client.post("/api/v1/business-events", json={
        "document_ids": [duplicate_document_id, duplicate_document_two_id, document_ids[-2], document_ids[-1]],
        "roles": ["INVOICE", "INVOICE", "GOODS_RECEIPT", "BANK_RECEIPT"],
        "reason": "核验另一笔业务重复使用原发票",
    })
    assert created.status_code == 200
    event_id = created.json()["id"]
    assert client.post(f"/api/v1/business-events/{event_id}/reconcile").json()["status"] == "MATCHED"

    response = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts")
    assert response.status_code == 409
    cases = client.get("/api/v1/exception-cases").json()["items"]
    duplicate_case = next(case for case in cases if case["event_id"] == event_id and case["type"] == "DUPLICATE_INVOICE")
    detail = client.get(f"/api/v1/exception-cases/{duplicate_case['id']}")
    assert detail.status_code == 200
    source_ids = {source["id"] for source in detail.json()["source_documents"]}
    assert set(document_ids[:2]) & source_ids
    assert duplicate_document_id in source_ids
    assert duplicate_document_two_id in source_ids
    assert detail.json()["unavailable_source_count"] == 0


def test_ordered_100_received_90_is_blocked_from_voucher_drafting(workflow_client):
    client, document_ids, _, _ = workflow_client
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        tenant = db.scalar(select(Tenant))
        account = db.scalar(select(AccountSet))
        contract = DocumentAsset(
            tenant_id=tenant.id, account_set_id=account.id, sha256=f"{uuid4().hex:0<64}",
            original_filename="purchase-contract.pdf", mime_type="application/pdf", byte_size=1,
            storage_key="/tmp/purchase-contract.pdf", uploaded_by="workflow-test",
            processing_status="CONFIRMED",
        )
        db.add(contract)
        db.flush()
        db.add(DocumentExtraction(
            document_id=contract.id, version=1, document_type="CONTRACT", full_text="合同采购数量：100 件",
            fields={"ordered_quantity": {"normalized_value": "100", "status": "CONFIRMED"}},
            lines=[], provider="fixture", model_version="fixture-contract-v1", status="CONFIRMED",
            created_by="workflow-test",
        ))
        receipt_extraction = db.scalar(select(DocumentExtraction).where(
            DocumentExtraction.document_id == document_ids[-2],
        ))
        receipt_extraction.fields = {
            **receipt_extraction.fields,
            "quantity": {"normalized_value": "90", "status": "CONFIRMED"},
        }
        db.commit()
        contract_id = contract.id
    finally:
        dependency.close()

    event = client.post("/api/v1/business-events", json={
        "document_ids": [document_ids[0], contract_id, document_ids[-2], document_ids[-1]],
        "roles": ["INVOICE", "CONTRACT", "GOODS_RECEIPT", "BANK_RECEIPT"],
        "reason": "检查合同数量与实际入库数量",
    })
    assert event.status_code == 200

    reconciled = client.post(f"/api/v1/business-events/{event.json()['id']}/reconcile")

    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "NEEDS_REVIEW"
    shortfall = next(finding for finding in reconciled.json()["findings"]
                     if finding["code"] == "GOODS_RECEIPT_QUANTITY_SHORTFALL")
    assert shortfall["ordered_quantity"] == "100"
    assert shortfall["received_quantity"] == "90"
    cases = client.get("/api/v1/exception-cases").json()["items"]
    case = next(item for item in cases if item["event_id"] == event.json()["id"])
    assert case["status"] == "OPEN"
    assert "GOODS_RECEIPT_QUANTITY_SHORTFALL" in str(case["details"])
    case_detail = client.get(f"/api/v1/exception-cases/{case['id']}")
    assert case_detail.status_code == 200
    assert "GOODS_RECEIPT_QUANTITY_SHORTFALL" in [finding["code"] for finding in case_detail.json()["findings"]]
    assert {source["role"] for source in case_detail.json()["source_documents"]} == {
        "INVOICE", "CONTRACT", "GOODS_RECEIPT", "BANK_RECEIPT",
    }
    assert any(entry["event_type"] == "BUSINESS_EVENT_RECONCILED"
               for entry in case_detail.json()["audit_trail"])
    draft = client.post(f"/api/v1/business-events/{event.json()['id']}/voucher-drafts")
    assert draft.status_code == 422
    assert "业务核对未通过" in draft.json()["detail"]


@pytest.mark.parametrize(("second_quantity", "expected_status"), [("60", "MATCHED"), ("50", "NEEDS_REVIEW")])
def test_partial_receipts_are_explicitly_linked_and_summed_against_contract(workflow_client, second_quantity, expected_status):
    client, document_ids, _, _ = workflow_client
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        tenant = db.scalar(select(Tenant))
        account = db.scalar(select(AccountSet))
        contract = DocumentAsset(
            tenant_id=tenant.id, account_set_id=account.id, sha256=f"{uuid4().hex:0<64}",
            original_filename="partial-delivery-contract.pdf", mime_type="application/pdf", byte_size=1,
            storage_key="/tmp/partial-delivery-contract.pdf", uploaded_by="workflow-test",
            processing_status="CONFIRMED",
        )
        second_receipt = DocumentAsset(
            tenant_id=tenant.id, account_set_id=account.id, sha256=f"{uuid4().hex:0<64}",
            original_filename="second-goods-receipt.pdf", mime_type="application/pdf", byte_size=1,
            storage_key="/tmp/second-goods-receipt.pdf", uploaded_by="workflow-test",
            processing_status="CONFIRMED",
        )
        db.add_all([contract, second_receipt])
        db.flush()
        db.add_all([
            DocumentExtraction(
                document_id=contract.id, version=1, document_type="CONTRACT", full_text="合同采购数量：100 件",
                fields={"ordered_quantity": {"normalized_value": "100", "status": "CONFIRMED"}},
                lines=[], provider="fixture", model_version="fixture-contract-v1", status="CONFIRMED",
                created_by="workflow-test",
            ),
            DocumentExtraction(
                document_id=second_receipt.id, version=1, document_type="GOODS_RECEIPT",
                full_text=f"第二批入库 {second_quantity} 件",
                fields={"receipt_date": {"normalized_value": "2024-12-01"},
                        "received": {"normalized_value": True},
                        "quantity": {"normalized_value": second_quantity, "status": "CONFIRMED"},
                        "gross_amount": {"normalized_value": "25200.00"}},
                lines=[], provider="fixture", model_version="fixture-receipt-v1", status="CONFIRMED",
                created_by="workflow-test",
            ),
        ])
        first_receipt = db.scalar(select(DocumentExtraction).where(
            DocumentExtraction.document_id == document_ids[-2],
        ))
        first_receipt.fields = {
            **first_receipt.fields,
            "quantity": {"normalized_value": "40", "status": "CONFIRMED"},
            "gross_amount": {"normalized_value": "20000.00"},
        }
        db.commit()
        contract_id = contract.id
        second_receipt_id = second_receipt.id
    finally:
        dependency.close()

    # 这三张证据由用户明确关联；系统不根据上传批次自动合并。
    invoice_count = len(document_ids) - 2
    event = client.post("/api/v1/business-events", json={
        "document_ids": [*document_ids[:invoice_count], contract_id, document_ids[-2], second_receipt_id, document_ids[-1]],
        "roles": [*["INVOICE"] * invoice_count, "CONTRACT", "GOODS_RECEIPT", "GOODS_RECEIPT", "BANK_RECEIPT"],
        "reason": "人工确认合同及两批入库单属于同一采购",
    })
    assert event.status_code == 200

    reconciled = client.post(f"/api/v1/business-events/{event.json()['id']}/reconcile")

    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == expected_status, reconciled.json()["findings"]
    if expected_status == "MATCHED":
        assert reconciled.json()["amount"] == "45200.00"
    else:
        shortfall = next(finding for finding in reconciled.json()["findings"]
                         if finding["code"] == "GOODS_RECEIPT_QUANTITY_SHORTFALL")
        assert shortfall["ordered_quantity"] == "100"
        assert shortfall["received_quantity"] == "90"
        assert client.post(f"/api/v1/business-events/{event.json()['id']}/voucher-drafts").status_code == 422


@pytest.mark.parametrize(("second_payment", "second_settled", "second_payer", "expected_status", "expected_finding"), [
    ("15200.00", True, "Buyer Ltd", "MATCHED", None),
    ("15000.00", True, "Buyer Ltd", "NEEDS_REVIEW", "GROSS_AMOUNT_MISMATCH"),
    ("15200.00", False, "Buyer Ltd", "NEEDS_REVIEW", "BANK_SETTLEMENT_NOT_CONFIRMED"),
    ("15200.00", True, "Other Buyer Ltd", "NEEDS_REVIEW", "PAYER_BUYER_MISMATCH"),
])
def test_multiple_bank_receipts_are_explicitly_linked_and_summed(
    workflow_client, second_payment, second_settled, second_payer, expected_status, expected_finding,
):
    client, document_ids, invoice_count, _ = workflow_client
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        tenant = db.scalar(select(Tenant))
        account = db.scalar(select(AccountSet))
        second_receipt = DocumentAsset(
            tenant_id=tenant.id, account_set_id=account.id, sha256=f"{uuid4().hex:0<64}",
            original_filename="second-bank-receipt.pdf", mime_type="application/pdf", byte_size=1,
            storage_key="/tmp/second-bank-receipt.pdf", uploaded_by="workflow-test",
            processing_status="CONFIRMED",
        )
        db.add(second_receipt)
        db.flush()
        db.add(DocumentExtraction(
            document_id=second_receipt.id, version=1, document_type="BANK_RECEIPT",
            full_text=f"第二笔银行回单 {second_payment}",
            fields={"settlement_date": {"normalized_value": "2024-12-02"},
                    "settled": {"normalized_value": second_settled},
                    "payer": {"normalized_value": second_payer},
                    "payee": {"normalized_value": "Seller Ltd"},
                    "gross_amount": {"normalized_value": second_payment}},
            lines=[], provider="fixture", model_version="fixture-bank-v1", status="CONFIRMED",
            created_by="workflow-test",
        ))
        original = db.scalar(select(DocumentExtraction).where(
            DocumentExtraction.document_id == document_ids[-1],
        ))
        original.fields = {**original.fields, "gross_amount": {"normalized_value": "30000.00"}}
        db.commit()
        second_receipt_id = second_receipt.id
    finally:
        dependency.close()

    event = client.post("/api/v1/business-events", json={
        "document_ids": [*document_ids[:invoice_count], document_ids[-2], document_ids[-1], second_receipt_id],
        "roles": [*["INVOICE"] * invoice_count, "GOODS_RECEIPT", "BANK_RECEIPT", "BANK_RECEIPT"],
        "reason": "人工确认两笔银行付款属于同一采购",
    })
    assert event.status_code == 200
    reconciled = client.post(f"/api/v1/business-events/{event.json()['id']}/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == expected_status, reconciled.json()["findings"]
    if expected_status == "MATCHED":
        assert reconciled.json()["amount"] == "45200.00"
    else:
        assert expected_finding in [finding["code"] for finding in reconciled.json()["findings"]]


def test_upload_field_candidate_keeps_ocr_page_and_bounding_box(
    workflow_client, tmp_path, monkeypatch
):
    client, _, _, _ = workflow_client
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(workflow, "extract_image", lambda _: {
        "full_text": "价税合计 45,200.00",
        "lines": [{
            "text": "价税合计 45,200.00",
            "confidence": 0.98,
            "bbox": [10, 20, 210, 48],
            "page": 2,
            "coordinate_space": "pixels",
            "source": "OCR",
        }],
        "provider": "fixture-ocr",
        "model_version": "fixture-ocr-v1",
    })

    response = client.post("/api/v1/documents", files={
        "file": ("invoice.jpg", b"\xff\xd8\xfffixture-image", "image/jpeg"),
    })

    assert response.status_code == 200
    field = response.json()["extraction"]["fields"]["gross_amount"]
    assert field["page"] == 2
    assert field["bbox"] == [10, 20, 210, 48]
    assert field["evidence_text"] == "价税合计 45,200.00"


def test_reconciliation_replays_newly_confirmed_extraction_version(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    roles = ["INVOICE"] * invoice_count + ["GOODS_RECEIPT", "BANK_RECEIPT"]
    created = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": roles,
        "reason": "验证修正字段后重跑核对",
    })
    event_id = created.json()["id"]
    assert client.post(f"/api/v1/business-events/{event_id}/reconcile").json()["status"] == "MATCHED"

    correction = client.patch(f"/api/v1/documents/{document_ids[0]}/extraction", json={
        "expected_version": 1,
        "document_type": "VAT_INVOICE",
        "fields": {"gross_amount": "23000.00"},
        "reason": "模拟人工发现发票金额与其他证据不一致",
    })
    assert correction.status_code == 200

    document_history = client.get(f"/api/v1/documents/{document_ids[0]}").json()["extraction_history"]
    assert [version["version"] for version in document_history] == [2, 1]
    assert document_history[0]["reason"] == "模拟人工发现发票金额与其他证据不一致"
    expected_original_amount = "45200.00" if invoice_count == 1 else "22600.00"
    assert document_history[1]["fields"]["gross_amount"]["normalized_value"] == expected_original_amount
    correction_stats = client.get("/api/v1/documents/correction-stats")
    assert correction_stats.status_code == 200
    assert correction_stats.json()["corrected_documents"] == 1
    assert correction_stats.json()["correction_versions"] == 1
    assert {item["field"]: item["count"] for item in correction_stats.json()["field_changes"]}["gross_amount"] == 1
    assert "23000.00" not in str(correction_stats.json())

    replayed = client.post(f"/api/v1/business-events/{event_id}/reconcile")
    assert replayed.status_code == 200
    assert replayed.json()["status"] == "NEEDS_REVIEW"
    assert "GROSS_AMOUNT_MISMATCH" in [finding["code"] for finding in replayed.json()["findings"]]
    detail = client.get(f"/api/v1/business-events/{event_id}").json()
    invoice_versions = [doc["extraction_version"] for doc in detail["documents"] if doc["role"] == "INVOICE"]
    assert invoice_versions.count(2) == 1


def test_pdf_preview_renders_requested_page(workflow_client, tmp_path, monkeypatch):
    client, _, _, _ = workflow_client
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    document = pymupdf.open()
    document.new_page(width=300, height=400).insert_text((30, 40), "First page")
    document.new_page(width=300, height=400).insert_text((30, 40), "Second page")
    output = BytesIO()
    document.save(output)
    document.close()
    uploaded = client.post("/api/v1/documents", files={
        "file": ("invoice.pdf", output.getvalue(), "application/pdf"),
    })
    assert uploaded.status_code == 200

    preview = client.get(f"/api/v1/documents/{uploaded.json()['id']}/preview?page=2")

    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert preview.headers["x-preview-page"] == "2"
    assert preview.headers["x-preview-page-count"] == "2"


def test_business_user_reads_only_assigned_events_and_documents(
    workflow_client, tmp_path, monkeypatch
):
    client, document_ids, invoice_count, business_email = workflow_client
    roles = ["INVOICE"] * invoice_count + ["GOODS_RECEIPT", "BANK_RECEIPT"]
    first = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": roles,
        "reason": "准备指派范围测试一",
    })
    second = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": roles,
        "reason": "准备指派范围测试二",
    })
    assert first.status_code == second.status_code == 200
    assigned_event_id = first.json()["id"]
    unassigned_event_id = second.json()["id"]
    members = client.get("/api/v1/auth/memberships?role=BUSINESS_USER")
    assert members.status_code == 200
    assert any(member["email"] == business_email for member in members.json())

    assignment = client.put(f"/api/v1/business-events/{assigned_event_id}/assignments", json={
        "email": business_email,
    })
    assert assignment.status_code == 200

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(workflow, "extract_image", lambda _: {
        "full_text": "unassigned standalone document",
        "lines": [],
        "provider": "fixture-ocr",
        "model_version": "fixture-ocr-v1",
    })
    unassigned_document = client.post("/api/v1/documents", files={
        "file": ("unassigned.jpg", b"\xff\xd8\xffunassigned", "image/jpeg"),
    }).json()["id"]

    business_client = TestClient(app)
    login = business_client.post("/api/v1/auth/login", json={
        "email": business_email,
        "password": "Test-password-for-workflow-2026",
    })
    assert login.status_code == 200
    business_client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
    try:
        events = business_client.get("/api/v1/business-events")
        assert events.status_code == 200
        assert [event["id"] for event in events.json()["items"]] == [assigned_event_id]
        assert events.json()["total"] == 1
        assert business_client.get(f"/api/v1/business-events/{unassigned_event_id}").status_code == 404
        visible_cases = business_client.get("/api/v1/exception-cases")
        assert visible_cases.status_code == 200
        assert visible_cases.json()["total"] == 0
        visible_documents = business_client.get("/api/v1/documents")
        assert visible_documents.status_code == 200
        visible_ids = {document["id"] for document in visible_documents.json()["items"]}
        assert set(document_ids).issubset(visible_ids)
        assert unassigned_document not in visible_ids
        invoice_summary = next(document for document in visible_documents.json()["items"]
                               if document["id"] == document_ids[0])
        assert [business["id"] for business in invoice_summary["business_events"]] == [assigned_event_id]
        assert unassigned_event_id not in [business["id"] for business in invoice_summary["business_events"]]
    finally:
        business_client.close()


def test_voucher_correction_creates_a_balanced_new_version(workflow_client):
    client, document_ids, invoice_count, _ = workflow_client
    created = client.post("/api/v1/business-events", json={
        "document_ids": document_ids,
        "roles": ["INVOICE"] * invoice_count + ["GOODS_RECEIPT", "BANK_RECEIPT"],
        "reason": "建立可修正凭证",
    })
    event_id = created.json()["id"]
    assert client.post(f"/api/v1/business-events/{event_id}/reconcile").json()["status"] == "MATCHED"
    configured = client.put("/api/v1/auth/chart-accounts", json={"accounts": [{
        "code": "1405", "name": "库存商品", "requires_auxiliary": True,
        "required_auxiliary_fields": ["department"],
    }]})
    assert configured.status_code == 200
    original = client.post(f"/api/v1/business-events/{event_id}/voucher-drafts").json()
    assert original["status"] == "NEEDS_REVIEW"

    valid_lines = [
        {"account_code": "1405", "debit": "40000.00", "credit": "0.00", "description": "库存商品", "evidence_refs": document_ids},
        {"account_code": "222101", "debit": "5200.00", "credit": "0.00", "description": "进项税额", "evidence_refs": document_ids},
        {"account_code": "1002", "debit": "0.00", "credit": "45200.00", "description": "银行存款", "evidence_refs": document_ids},
    ]
    url = f"/api/v1/voucher-drafts/{original['id']}/versions"
    body = {"expected_version": original["version"], "reason": "按财务科目要求修正摘要", "lines": valid_lines}

    wrong_amount = {**body, "lines": [dict(line) for line in valid_lines]}
    wrong_amount["lines"][0]["debit"] = "40001.00"
    wrong_amount["lines"][2]["credit"] = "45201.00"
    assert client.post(url, json=wrong_amount).status_code == 422

    unrelated_evidence = {**body, "lines": [dict(line) for line in valid_lines]}
    unrelated_evidence["lines"][0]["evidence_refs"] = ["unrelated-document"]
    assert client.post(url, json=unrelated_evidence).status_code == 422

    missing_auxiliary = client.post(url, json=body)
    assert missing_auxiliary.status_code == 422
    assert "缺少：department" in missing_auxiliary.json()["detail"]
    for invalid_auxiliary in (
        {"department": " "}, {" ": "Operations"},
        {"department": "Operations", " department ": "Finance"},
    ):
        invalid_lines = [dict(line) for line in valid_lines]
        invalid_lines[0]["auxiliary"] = invalid_auxiliary
        assert client.post(url, json={**body, "lines": invalid_lines}).status_code == 422
    unexpected_auxiliary = [dict(line) for line in valid_lines]
    unexpected_auxiliary[0]["auxiliary"] = {"project": "Research"}
    assert client.post(url, json={**body, "lines": unexpected_auxiliary}).status_code == 422
    lines_with_auxiliary = [dict(line) for line in valid_lines]
    lines_with_auxiliary[0]["auxiliary"] = {"department": "Operations"}
    body_with_auxiliary = {**body, "lines": lines_with_auxiliary}
    response = client.post(url, json=body_with_auxiliary)
    assert response.status_code == 200, response.text
    assert response.json()["id"] != original["id"]
    assert response.json()["version"] == original["version"] + 1
    assert response.json()["status"] == "VALIDATED"
    assert response.json()["debit_total"] == response.json()["credit_total"] == "45200.00"
    corrected_inventory = next(line for line in response.json()["lines"] if line["account_code"] == "1405")
    assert corrected_inventory["auxiliary"] == {"department": "Operations"}
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        from app.db.models import VoucherDraft

        corrected_draft = db.scalar(select(VoucherDraft).where(VoucherDraft.id == response.json()["id"]))
        inventory_line = next(line for line in corrected_draft.lines if line.account_code == "1405")
        inventory_line.auxiliary = {}
        db.commit()
    finally:
        dependency.close()
    assert client.post(f"/api/v1/voucher-drafts/{response.json()['id']}/submit-review").status_code == 422
    dependency = app.dependency_overrides[db_session]()
    db = next(dependency)
    try:
        corrected_draft = db.scalar(select(VoucherDraft).where(VoucherDraft.id == response.json()["id"]))
        inventory_line = next(line for line in corrected_draft.lines if line.account_code == "1405")
        inventory_line.auxiliary = {"department": "Operations"}
        db.commit()
    finally:
        dependency.close()
    submitted = client.post(f"/api/v1/voucher-drafts/{response.json()['id']}/submit-review")
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "PENDING_APPROVAL"
    latest_summary = client.get("/api/v1/voucher-drafts").json()
    assert latest_summary["total"] == 2
    assert latest_summary["status_counts"] == [{"period": "2024-12", "status": "PENDING_APPROVAL", "count": 1}]

    stale_parent = client.post(url, json=body)
    assert stale_parent.status_code == 409



def test_upload_keeps_party_candidates_and_rejects_conflicting_net_and_tax_pages(
    workflow_client, tmp_path, monkeypatch
):
    client, _, _, _ = workflow_client
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    evidence_lines = [
        {"text": "购买方信息", "bbox": [20, 10, 60, 20], "page": 1, "coordinate_space": "pixels"},
        {"text": "名称：Buyer Ltd", "bbox": [80, 35, 240, 48], "page": 1, "coordinate_space": "pixels"},
        {"text": "销售方信息", "bbox": [500, 10, 540, 20], "page": 1, "coordinate_space": "pixels"},
        {"text": "名称：Seller Ltd", "bbox": [560, 35, 740, 48], "page": 1, "coordinate_space": "pixels"},
        {"text": "金额", "bbox": [300, 200, 350, 215], "page": 1, "coordinate_space": "pixels"},
        {"text": "税额", "bbox": [700, 200, 750, 215], "page": 1, "coordinate_space": "pixels"},
        {"text": "40000.00", "bbox": [300, 250, 360, 265], "page": 1, "coordinate_space": "pixels"},
        {"text": "5200.00", "bbox": [700, 250, 755, 265], "page": 1, "coordinate_space": "pixels"},
        {"text": "40000.00", "bbox": [300, 350, 360, 365], "page": 1, "coordinate_space": "pixels"},
        {"text": "5200.00", "bbox": [700, 350, 755, 365], "page": 1, "coordinate_space": "pixels"},
        {"text": "价税合计", "bbox": [800, 390, 870, 405], "page": 1, "coordinate_space": "pixels"},
        {"text": "（小写）￥45200.00", "bbox": [875, 390, 990, 405], "page": 1, "coordinate_space": "pixels"},
        {"text": "金额", "bbox": [300, 200, 350, 215], "page": 2, "coordinate_space": "pixels"},
        {"text": "税额", "bbox": [700, 200, 750, 215], "page": 2, "coordinate_space": "pixels"},
        {"text": "99999.00", "bbox": [300, 350, 360, 365], "page": 2, "coordinate_space": "pixels"},
        {"text": "9999.00", "bbox": [700, 350, 755, 365], "page": 2, "coordinate_space": "pixels"},
    ]
    monkeypatch.setattr(workflow, "extract_image", lambda _: {
        "full_text": "购买方信息 名称：Buyer Ltd 销售方信息 名称：Seller Ltd 金额 税额 40000.00 5200.00 40000.00 5200.00 价税合计（小写）￥45200.00",
        "lines": evidence_lines,
        "provider": "fixture-ocr",
        "model_version": "fixture-ocr-v1",
    })

    response = client.post("/api/v1/documents", files={
        "file": ("invoice-parties.jpg", b"\xff\xd8\xffparty-image", "image/jpeg"),
    })

    fields = response.json()["extraction"]["fields"]
    assert fields["buyer"]["normalized_value"] == "Buyer Ltd"
    assert fields["buyer"]["bbox"] == [80, 35, 240, 48]
    assert fields["seller"]["normalized_value"] == "Seller Ltd"
    assert fields["seller"]["bbox"] == [560, 35, 740, 48]
    assert fields["net_amount"]["normalized_value"] is None
    assert fields["net_amount"]["bbox"] is None
    assert fields["tax_amount"]["normalized_value"] is None
    assert fields["tax_amount"]["bbox"] is None


def test_invoice_number_candidate_prefers_number_over_old_invoice_code(workflow_client, tmp_path, monkeypatch):
    client, _, _, _ = workflow_client
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(workflow, "extract_image", lambda _: {
        "full_text": "发票代码：1100162130\nNo 09750785",
        "lines": [
            {"text": "发票代码：1100162130", "bbox": [10, 10, 140, 30], "page": 1, "coordinate_space": "pixels"},
            {"text": "No 09750785", "bbox": [10, 35, 120, 55], "page": 1, "coordinate_space": "pixels"},
        ],
        "provider": "fixture-ocr",
        "model_version": "fixture-ocr-v1",
    })

    response = client.post("/api/v1/documents", files={
        "file": ("old-invoice.jpg", b"\xff\xd8\xffold-invoice", "image/jpeg"),
    })

    invoice_number = response.json()["extraction"]["fields"]["invoice_number"]
    assert invoice_number["normalized_value"] == "09750785"
    assert invoice_number["evidence_text"] == "No 09750785"
