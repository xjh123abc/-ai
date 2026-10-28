import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (AccountSet, AccountingPolicy, AppUser, AuditEvent, BusinessEvent, BusinessEventAssignment, BusinessEventDocument,
                           AccountingPeriod, DocumentAsset, DocumentExtraction, ExceptionCase, Tenant,
                           TenantMembership, VoucherApproval, VoucherDraft, VoucherDraftLine, ChartAccount, PostingAttempt)
from app.db.session import db_session
from app.ocr import OcrImageTooLargeError, extract_image, extract_pdf
from ai.suggestions import (AccountingSuggestionProvider, DeepSeekAccountingSuggestionProvider,
    SOURCE_FACT_FIELDS, SuggestedAccount, SuggestionProviderError, VoucherSuggestionContext)
from app.security import Principal, current_principal, require
from domain.settlement import normalize_bank_settlement_state
from app.structured_files import extract_csv, extract_xlsx
from ingestion.candidates import field_candidates, normalize_date_value
from connectors.csv_export import GenericCsvVoucherExporter, VoucherExportData, VoucherExportLine

router = APIRouter(prefix="/api/v1")
ALLOWED = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png", b"%PDF-": "application/pdf"}

def actor(principal: Principal = Depends(current_principal)) -> str:
    return principal.actor

def get_accounting_suggestion_provider() -> AccountingSuggestionProvider:
    return DeepSeekAccountingSuggestionProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_seconds=settings.deepseek_timeout_seconds,
    )

def require_document_read(principal: Principal) -> None:
    permission = "read_assigned" if principal.role == "BUSINESS_USER" and not principal.demo else "read"
    require(principal, permission)

def active_scope(db: Session, principal: Principal):
    tenant = db.get(Tenant, principal.tenant_id)
    account = db.scalar(select(AccountSet).where(AccountSet.id == principal.account_set_id,
                                                  AccountSet.tenant_id == principal.tenant_id))
    if tenant is None or account is None:
        raise HTTPException(403, "当前登录用户未绑定有效企业和账套")
    return tenant, account


def latest_accounting_policy(db: Session, account_set_id: str, business_type: str):
    return db.scalar(select(AccountingPolicy).where(
        AccountingPolicy.account_set_id == account_set_id,
        AccountingPolicy.business_type == business_type,
    ).order_by(AccountingPolicy.version.desc()).limit(1))


def accounting_policy_ref(business_type: str, version: int) -> str:
    return f"accounting-policy:{business_type}:v{version}"


def validate_voucher_amounts_and_accounts(lines, configuration: dict | None,
                                          net: Decimal, tax: Decimal, gross: Decimal) -> None:
    allowed_codes = set(configuration["allowed_account_codes"]) if configuration else None
    if allowed_codes is not None and any(line.account_code not in allowed_codes for line in lines):
        raise HTTPException(422, "分录科目不在当前会计政策允许范围内")
    if any((line.debit == 0) == (line.credit == 0) or line.debit < 0 or line.credit < 0
           for line in lines):
        raise HTTPException(422, "每条分录必须只填写借方或贷方正金额")
    debit = sum((line.debit for line in lines), Decimal("0.00"))
    credit = sum((line.credit for line in lines), Decimal("0.00"))
    if debit != credit:
        raise HTTPException(422, "借贷不平")
    if net + tax != gross or debit != gross:
        raise HTTPException(422, "凭证金额与已确认发票金额不一致")
    if not configuration:
        return
    treatment = configuration["tax_treatment"]
    tax_codes = set(configuration["input_tax_account_codes"])
    if treatment == "DEDUCTIBLE":
        tax_debit = sum((line.debit for line in lines if line.account_code in tax_codes), Decimal("0.00"))
        tax_credit = sum((line.credit for line in lines if line.account_code in tax_codes), Decimal("0.00"))
        if tax_debit != tax or tax_credit or debit - tax_debit != net:
            raise HTTPException(422, "进项税分录与已确认税额及税务政策不一致")
    elif any(line.account_code in tax_codes for line in lines):
        raise HTTPException(422, "不可抵扣政策不能使用进项税科目")


def validate_voucher_evidence(lines, linked_documents, tax_codes: set[str]) -> None:
    linked_ids = {link.document_id for link in linked_documents}
    invoice_ids = {link.document_id for link in linked_documents if link.role == "INVOICE"}
    for line in lines:
        refs = set(line.evidence_refs)
        if not refs or not refs.issubset(linked_ids):
            raise HTTPException(422, "分录引用了未关联的来源单据或缺少证据引用")
        if line.account_code in tax_codes and line.debit and not refs.intersection(invoice_ids):
            raise HTTPException(422, "进项税分录必须引用关联发票")


def rental_direct_expense_confirmation_is_current(event: BusinessEvent, policy: AccountingPolicy) -> bool:
    return (event.rental_direct_expense_confirmed
            and event.rental_direct_expense_policy_version == accounting_policy_ref("RENTAL", policy.version))


def assigned_event_ids(principal: Principal):
    return select(BusinessEventAssignment.event_id).where(
        BusinessEventAssignment.tenant_id==principal.tenant_id,
        BusinessEventAssignment.account_set_id==principal.account_set_id,
        BusinessEventAssignment.user_id==principal.user_id)

def apply_event_read_scope(query, principal: Principal):
    if principal.role=="BUSINESS_USER" and not principal.demo:
        query=query.where(or_(BusinessEvent.created_by==principal.actor,
                              BusinessEvent.id.in_(assigned_event_ids(principal))))
    return query

def apply_document_read_scope(query, principal: Principal):
    if principal.role=="BUSINESS_USER" and not principal.demo:
        assigned_document_ids=(select(BusinessEventDocument.document_id)
            .join(BusinessEventAssignment,BusinessEventAssignment.event_id==BusinessEventDocument.event_id)
            .where(BusinessEventAssignment.tenant_id==principal.tenant_id,
                   BusinessEventAssignment.account_set_id==principal.account_set_id,
                   BusinessEventAssignment.user_id==principal.user_id))
        query=query.where(or_(DocumentAsset.uploaded_by==principal.actor,
                              DocumentAsset.id.in_(assigned_document_ids)))
    return query

def audit(db: Session, tenant_id: str, who: str, kind: str, ref: str, refs=None, before=None, after=None):
    def digest(value):
        if value is None: return None
        encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(",",":")).encode()
        return hashlib.sha256(encoded).hexdigest()
    db.add(AuditEvent(tenant_id=tenant_id, actor=who, event_type=kind, entity_ref=ref,
                      source_refs=refs or [],before_hash=digest(before),after_hash=digest(after)))

def public_extraction_fields(fields: dict) -> tuple[dict, dict | None]:
    visible = dict(fields)
    type_candidate = visible.pop("document_type_candidate", None)
    return visible, type_candidate

DOCUMENT_TYPE_FIELDS = {
    "VAT_INVOICE": {"invoice_number", "issue_date", "buyer", "seller", "net_amount", "tax_rate", "tax_amount", "gross_amount"},
    "GOODS_RECEIPT": {"receipt_number", "receipt_date", "item", "quantity", "gross_amount", "received"},
    "BANK_RECEIPT": {"payer", "payee", "settlement_date", "gross_amount", "settled",
                     "payer_account_number", "payee_account_number", "bank_account_number", "bank_name",
                     "electronic_receipt_number", "transaction_id"},
    "PAYMENT_PROOF": {"proof_number", "payment_date", "payer", "payee", "purpose", "gross_amount", "approved"},
    "CONTRACT": {"contract_number", "contract_date", "buyer", "seller", "item", "ordered_quantity", "gross_amount"},
    "ACCEPTANCE_REPORT": {"acceptance_date", "acceptance_result"},
    "TAXI_RECEIPT": {"invoice_number", "issue_date", "gross_amount"},
    "TOLL_RECEIPT": {"receipt_number", "issue_date", "gross_amount"},
}
AUXILIARY_FIELD_KEYS = {"payer_account_number", "payee_account_number", "bank_account_number",
                        "bank_name", "electronic_receipt_number", "transaction_id"}

def fill_ocr_auxiliary_fields(fields: dict, full_text: str, lines: list[dict]) -> dict:
    result = dict(fields)
    candidates = field_candidates(full_text, lines)
    for key in AUXILIARY_FIELD_KEYS:
        if not result.get(key, {}).get("normalized_value") and candidates[key].get("normalized_value"):
            result[key] = candidates[key]
    return result

@router.post("/documents")
async def upload_document(file: UploadFile = File(...), db: Session = Depends(db_session), who: str = Depends(actor), principal: Principal = Depends(current_principal)):
    require(principal, "upload")
    tenant, account = active_scope(db, principal)
    data = await file.read(settings.max_upload_bytes + 1)
    if not data or len(data) > settings.max_upload_bytes:
        raise HTTPException(413, "文件为空或超过上传大小限制")
    filename=Path(file.filename or "upload").name
    extension=Path(filename).suffix.lower()
    if extension==".csv": mime="text/csv"
    elif extension==".xlsx" and data.startswith(b"PK\x03\x04"): mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else: mime = next((kind for magic, kind in ALLOWED.items() if data.startswith(magic)), None)
    if mime is None:
        raise HTTPException(415, "UNSUPPORTED_FORMAT：支持 JPG/PNG/PDF/CSV/XLSX")
    allowed_declared={mime,"application/octet-stream"}
    if mime=="text/csv":allowed_declared.update({"text/plain","application/vnd.ms-excel"})
    if file.content_type and file.content_type not in allowed_declared:
        raise HTTPException(415, "声明类型与文件内容不匹配")
    digest = hashlib.sha256(data).hexdigest()
    duplicate = db.scalar(select(DocumentAsset).where(DocumentAsset.tenant_id == tenant.id,
        DocumentAsset.account_set_id == account.id, DocumentAsset.sha256 == digest))
    if duplicate:
        return {"id": duplicate.id, "duplicate": True, "processing_status": duplicate.processing_status}
    doc_id = str(uuid4())
    suffix = {"image/jpeg":".jpg","image/png":".png","application/pdf":".pdf","text/csv":".csv","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":".xlsx"}[mime]
    root = settings.storage_dir.resolve()
    path = root / tenant.id / (doc_id + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = DocumentAsset(id=doc_id, tenant_id=tenant.id, account_set_id=account.id, sha256=digest,
                        original_filename=Path(file.filename or "upload" ).name[:255], mime_type=mime,
                        byte_size=len(data), storage_key=str(path), uploaded_by=who, processing_status="PROCESSING")
    db.add(row); db.flush()
    try:
        if mime=="application/pdf": ocr=extract_pdf(path)
        elif mime=="image/jpeg" or mime=="image/png": ocr=extract_image(path)
        elif mime=="text/csv": ocr=extract_csv(data)
        else: ocr=extract_xlsx(data)
        extraction = DocumentExtraction(document_id=doc_id, version=1, document_type=ocr.get("document_type","UNCLASSIFIED"),
            full_text=ocr["full_text"], lines=ocr["lines"], fields=ocr.get("fields",field_candidates(ocr["full_text"],ocr["lines"])),
            provider=ocr["provider"], model_version=ocr["model_version"], status="NEEDS_REVIEW", created_by=who)
        db.add(extraction); row.processing_status = "NEEDS_REVIEW"
    except Exception as exc:
        row.processing_status = "FAILED"
        failure_fields = {"raw_value": type(exc).__name__, "status": "FAILED"}
        if isinstance(exc, OcrImageTooLargeError):
            failure_fields["message"] = str(exc)
        extraction = DocumentExtraction(document_id=doc_id, version=1, document_type="UNCLASSIFIED", full_text="",
            lines=[], fields={"error": failure_fields},
            provider="RapidOCR", model_version="PP-OCRv6-Tiny/CPU", status="FAILED", created_by=who)
        db.add(extraction)
    audit(db, tenant.id, who, "DOCUMENT_UPLOADED", doc_id, [doc_id],
          after={"sha256":digest,"mime_type":mime,"byte_size":len(data),"filename":row.original_filename})
    db.commit()
    visible_fields, type_candidate = public_extraction_fields(extraction.fields)
    return {"id": doc_id, "duplicate": False, "processing_status": row.processing_status,
            "extraction": {"id": extraction.id, "version": extraction.version, "document_type": extraction.document_type,
                           "fields": visible_fields, "document_type_candidate": type_candidate,
                           "full_text": extraction.full_text, "lines": extraction.lines,
                           "provider": extraction.provider, "model_version": extraction.model_version}}

@router.get("/documents")
def list_documents(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                   work_queue: bool = False,
                   db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require_document_read(principal)
    tenant, account = active_scope(db, principal)
    query=apply_document_read_scope(select(DocumentAsset).where(
        DocumentAsset.tenant_id == tenant.id, DocumentAsset.account_set_id == account.id),principal)
    if work_queue:
        archived_document_ids = (select(BusinessEventDocument.document_id)
            .join(BusinessEvent, BusinessEvent.id == BusinessEventDocument.event_id)
            .join(VoucherDraft, VoucherDraft.event_id == BusinessEvent.id)
            .where(BusinessEvent.account_set_id == account.id, VoucherDraft.status != "REJECTED"))
        query = query.where(DocumentAsset.id.not_in(archived_document_ids))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(DocumentAsset.uploaded_at.desc())
        .offset((page - 1) * page_size).limit(page_size)).all()
    row_ids = [document.id for document in rows]
    latest_version = (select(DocumentExtraction.document_id,
        func.max(DocumentExtraction.version).label("version"))
        .where(DocumentExtraction.document_id.in_(row_ids)).group_by(DocumentExtraction.document_id).subquery())
    latest_extractions = db.scalars(select(DocumentExtraction).join(latest_version,
        (DocumentExtraction.document_id == latest_version.c.document_id)
        & (DocumentExtraction.version == latest_version.c.version))).all() if row_ids else []
    extraction_by_document = {item.document_id: item for item in latest_extractions}
    event_link_query = apply_event_read_scope(select(BusinessEventDocument, BusinessEvent).join(
        BusinessEvent, BusinessEvent.id == BusinessEventDocument.event_id).where(
            BusinessEventDocument.document_id.in_(row_ids), BusinessEvent.tenant_id == tenant.id,
            BusinessEvent.account_set_id == account.id), principal)
    event_links = db.execute(event_link_query.order_by(
        BusinessEvent.created_at.desc(), BusinessEventDocument.id.desc())).all() if row_ids else []
    events_by_document: dict[str, list[dict]] = {}
    for link, event in event_links:
        if link.document_id in events_by_document:
            continue
        events_by_document[link.document_id] = [{
            "id": event.id, "status": event.status, "role": link.role,
        }]
    required_fields = {
        "VAT_INVOICE": ("invoice_number", "issue_date", "buyer", "seller", "net_amount", "tax_amount", "gross_amount"),
        "GOODS_RECEIPT": ("receipt_number", "receipt_date", "item", "quantity", "gross_amount", "received"),
        "BANK_RECEIPT": ("payer", "payee", "settlement_date", "gross_amount", "settled"),
        "CONTRACT": ("contract_number", "contract_date", "ordered_quantity"),
        "PAYMENT_PROOF": ("proof_number", "payment_date", "payer", "payee", "purpose", "gross_amount", "approved"),
        "ACCEPTANCE_REPORT": ("acceptance_date", "acceptance_result"),
        "TAXI_RECEIPT": ("invoice_number", "issue_date", "gross_amount"),
        "TOLL_RECEIPT": ("receipt_number", "gross_amount"),
    }
    def candidate_value(fields: dict, key: str):
        value = fields.get(key)
        return value.get("normalized_value") if isinstance(value, dict) else value
    def document_summary(document: DocumentAsset):
        extraction = extraction_by_document.get(document.id)
        fields = dict(extraction.fields) if extraction else {}
        if extraction and extraction.status == "NEEDS_REVIEW" and extraction.full_text:
            fresh_amount = field_candidates(extraction.full_text, extraction.lines).get("gross_amount")
            saved_amount = fields.get("gross_amount")
            if (fresh_amount and fresh_amount.get("normalized_value")
                    and isinstance(saved_amount, dict) and not saved_amount.get("bbox")
                    and fresh_amount.get("bbox")):
                fields["gross_amount"] = fresh_amount
        document_type = extraction.document_type if extraction else "UNCLASSIFIED"
        required = required_fields.get(document_type, ())
        missing_fields = [key for key in required if candidate_value(fields, key) in (None, "")]
        if not extraction or document_type == "UNCLASSIFIED":
            missing_fields.insert(0, "document_type")
        return {"id": document.id, "filename": document.original_filename, "mime_type": document.mime_type,
            "sha256": document.sha256, "processing_status": document.processing_status,
            "uploaded_at": document.uploaded_at, "uploaded_by": document.uploaded_by,
            "document_type": document_type, "key_amount": candidate_value(fields, "gross_amount"),
            "business_events": events_by_document.get(document.id, []), "missing_fields": missing_fields}
    linked_ids = select(BusinessEventDocument.document_id).join(BusinessEvent,
        BusinessEvent.id == BusinessEventDocument.event_id).where(
            BusinessEvent.tenant_id == tenant.id, BusinessEvent.account_set_id == account.id)
    unlinked_query = apply_document_read_scope(select(DocumentAsset.id).where(
        DocumentAsset.tenant_id == tenant.id, DocumentAsset.account_set_id == account.id,
        DocumentAsset.id.not_in(linked_ids)), principal)
    unlinked_total = db.scalar(select(func.count()).select_from(unlinked_query.subquery())) or 0
    visible_document_ids = query.with_only_columns(DocumentAsset.id).order_by(None)
    status_counts = dict(db.execute(select(DocumentAsset.processing_status, func.count())
        .where(DocumentAsset.id.in_(visible_document_ids))
        .group_by(DocumentAsset.processing_status)).all())
    return {"items": [document_summary(document) for document in rows],
            "page": page, "page_size": page_size, "total": total,
            "unlinked_total": unlinked_total, "status_counts": status_counts}

@router.get("/documents/correction-stats")
def document_correction_stats(db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require_document_read(principal)
    tenant, account = active_scope(db, principal)
    scoped_documents = apply_document_read_scope(select(DocumentAsset.id).where(
        DocumentAsset.tenant_id == tenant.id, DocumentAsset.account_set_id == account.id), principal).subquery()
    versions_query = select(DocumentExtraction).where(DocumentExtraction.document_id.in_(
        select(scoped_documents.c.id))).order_by(DocumentExtraction.document_id, DocumentExtraction.version)
    versions = db.scalars(versions_query).yield_per(500)
    field_counts: dict[str, int] = {}
    corrected_documents = 0
    correction_versions = 0
    current_document_id = None
    current_document_corrected = False
    previous = None
    def field_value(fields: dict, key: str):
        value = fields.get(key)
        return value.get("normalized_value") if isinstance(value, dict) else value
    for current in versions:
        if current.document_id != current_document_id:
            if current_document_corrected:
                corrected_documents += 1
            current_document_id = current.document_id
            current_document_corrected = False
            previous = None
        if current.correction_reason:
            correction_versions += 1
            current_document_corrected = True
            if previous is not None:
                changed_keys = {key for key in set(previous.fields) | set(current.fields)
                    if field_value(previous.fields, key) != field_value(current.fields, key)}
                if previous.document_type != current.document_type:
                    changed_keys.add("document_type")
                for key in changed_keys:
                    field_counts[key] = field_counts.get(key, 0) + 1
        previous = current
    if current_document_corrected:
        corrected_documents += 1
    return {"corrected_documents": corrected_documents,
        "correction_versions": correction_versions,
        "field_changes": [{"field": key, "count": count} for key, count in sorted(
            field_counts.items(), key=lambda item: (-item[1], item[0]))]}

@router.get("/documents/{document_id}")
def document_detail(document_id: str, version: int | None = Query(default=None, ge=1),
                    db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require_document_read(principal)
    tenant, account = active_scope(db, principal)
    query=apply_document_read_scope(select(DocumentAsset).where(
        DocumentAsset.id == document_id, DocumentAsset.tenant_id == tenant.id,
        DocumentAsset.account_set_id == account.id),principal)
    d = db.scalar(query)
    if not d: raise HTTPException(404, "单据不存在")
    extractions = db.scalars(select(DocumentExtraction).where(
        DocumentExtraction.document_id == d.id).order_by(DocumentExtraction.version.desc())).all()
    if version is not None:
        e = next((item for item in extractions if item.version == version), None)
        if e is None:
            raise HTTPException(404, "请求的单据确认版本不存在")
    else:
        e = extractions[0] if extractions else None
    current_fields, current_type_candidate = public_extraction_fields(e.fields) if e else ({}, None)
    if e and version is None:
        current_fields = fill_ocr_auxiliary_fields(current_fields, e.full_text, e.lines)
        if e.status == "NEEDS_REVIEW" and e.full_text:
            recovered = field_candidates(e.full_text, e.lines)
            for key, candidate in recovered.items():
                if key == "document_type_candidate":
                    if not current_type_candidate or not current_type_candidate.get("normalized_value"):
                        current_type_candidate = candidate
                    continue
                current = current_fields.get(key)
                if (candidate.get("normalized_value") not in (None, "")
                        and (not isinstance(current, dict) or current.get("normalized_value") in (None, "")
                             or (key == "gross_amount" and not current.get("bbox") and candidate.get("bbox")))):
                    current_fields[key] = candidate
    return {"id": d.id, "filename": d.original_filename, "status": d.processing_status,
            "extraction": None if not e else {"version": e.version, "document_type": e.document_type,
                "fields": current_fields, "document_type_candidate": current_type_candidate,
                "full_text": e.full_text, "lines": e.lines, "provider": e.provider, "model_version": e.model_version},
            "extraction_history": [{"version": item.version, "document_type": item.document_type,
                "status": item.status, "created_by": item.created_by, "created_at": item.created_at,
                "reason": item.correction_reason,
                "fields": public_extraction_fields(item.fields)[0],
                "document_type_candidate": public_extraction_fields(item.fields)[1]}
                for item in extractions]}

@router.get("/documents/{document_id}/content")
def document_content(document_id: str, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require_document_read(principal)
    tenant, account = active_scope(db, principal)
    query=apply_document_read_scope(select(DocumentAsset).where(
        DocumentAsset.id == document_id, DocumentAsset.tenant_id == tenant.id,
        DocumentAsset.account_set_id == account.id),principal)
    d = db.scalar(query)
    if not d or not Path(d.storage_key).is_file(): raise HTTPException(404, "原件不存在")
    return FileResponse(d.storage_key, media_type=d.mime_type, filename=d.original_filename, content_disposition_type="inline")

@router.get("/documents/{document_id}/preview")
def document_preview(document_id: str, page: int = Query(default=1, ge=1),
                     db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require_document_read(principal)
    tenant, account = active_scope(db, principal)
    query=apply_document_read_scope(select(DocumentAsset).where(DocumentAsset.id==document_id,
        DocumentAsset.tenant_id==tenant.id,DocumentAsset.account_set_id==account.id),principal)
    document=db.scalar(query)
    if not document or not Path(document.storage_key).is_file():
        raise HTTPException(404,"单据不存在")
    if document.mime_type.startswith("image/"):
        if page!=1:
            raise HTTPException(422,"图片单据只有第 1 页")
        return FileResponse(document.storage_key,media_type=document.mime_type,
            headers={"X-Preview-Page":"1","X-Preview-Page-Count":"1"},content_disposition_type="inline")
    if document.mime_type!="application/pdf":
        raise HTTPException(415,"只有图片和 PDF 支持页面预览")
    import pymupdf
    source=pymupdf.open(document.storage_key)
    try:
        if source.needs_pass:
            raise HTTPException(422,"加密 PDF 暂不支持页面预览")
        if page>source.page_count:
            raise HTTPException(422,f"PDF 只有 {source.page_count} 页")
        pdf_page=source.load_page(page-1)
        pixmap=pdf_page.get_pixmap(dpi=144,alpha=False)
        return Response(content=pixmap.tobytes("png"),media_type="image/png",headers={
            "X-Preview-Page":str(page),
            "X-Preview-Page-Count":str(source.page_count),
            "X-Preview-Page-Width-Points":str(pdf_page.rect.width),
            "X-Preview-Page-Height-Points":str(pdf_page.rect.height),
        })
    finally:
        source.close()

class ConfirmFields(BaseModel):
    expected_version: int
    document_type: str
    fields: dict
    reason: str = Field(min_length=1, max_length=500)

@router.patch("/documents/{document_id}/extraction")
def confirm_fields(document_id: str, body: ConfirmFields, db: Session = Depends(db_session), who: str = Depends(actor), principal: Principal = Depends(current_principal)):
    require(principal, "correct")
    tenant, account = active_scope(db, principal)
    query=apply_document_read_scope(select(DocumentAsset).where(
        DocumentAsset.id == document_id, DocumentAsset.tenant_id == tenant.id,
        DocumentAsset.account_set_id == account.id),principal)
    d = db.scalar(query.with_for_update())
    if not d: raise HTTPException(404, "单据不存在")
    prev = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == d.id).order_by(DocumentExtraction.version.desc()))
    if not prev or prev.version != body.expected_version: raise HTTPException(409, "字段版本冲突，请刷新后重试")
    allowed_fields = DOCUMENT_TYPE_FIELDS.get(body.document_type)
    if allowed_fields is not None:
        allowed_fields = allowed_fields | AUXILIARY_FIELD_KEYS
    if allowed_fields is not None:
        unexpected_fields = set(body.fields) - allowed_fields
        if unexpected_fields:
            raise HTTPException(422, "字段与所选单据类型不匹配，请检查字段类型")
    fields = fill_ocr_auxiliary_fields(prev.fields, prev.full_text, prev.lines)
    fields.pop("document_type_candidate", None)
    if allowed_fields is not None:
        fields = {key: value for key, value in fields.items() if key in allowed_fields}
    for key, value in body.fields.items():
        normalized_value = value
        if key == "settled":
            normalized_value = normalize_bank_settlement_state(value)
            if normalized_value is None:
                raise HTTPException(422, "银行清算状态只能是已清算、未清算或无法确认")
        if key in {"issue_date", "receipt_date", "settlement_date", "payment_date", "acceptance_date"}:
            normalized_value, _ = normalize_date_value(str(value))
            normalized_value = normalized_value or value
        fields[key] = {"raw_value": value, "normalized_value": normalized_value, "confidence": 1.0,
                       "status": "USER_CONFIRMED", "page": fields.get(key, {}).get("page"),
                       "bbox": fields.get(key, {}).get("bbox"), "evidence_text": fields.get(key, {}).get("evidence_text", ""),
                       "coordinate_space": fields.get(key, {}).get("coordinate_space"),
                       "page_size_points": fields.get(key, {}).get("page_size_points"),
                       "previous_value": fields.get(key, {}).get("normalized_value"), "reason": body.reason}
    canonical_date_key = {"VAT_INVOICE": "issue_date", "GOODS_RECEIPT": "receipt_date",
                          "BANK_RECEIPT": "settlement_date", "PAYMENT_PROOF": "payment_date",
                          "ACCEPTANCE_REPORT": "acceptance_date"}.get(body.document_type)
    if canonical_date_key and not fields.get(canonical_date_key, {}).get("normalized_value"):
        candidate = fields.get("issue_date", {})
        candidate_value = candidate.get("normalized_value")
        if candidate_value:
            normalized_value, _ = normalize_date_value(str(candidate_value))
            fields[canonical_date_key] = {**candidate,
                "normalized_value": normalized_value or candidate_value,
                "status": candidate.get("status", "NEEDS_REVIEW")}
    new = DocumentExtraction(document_id=d.id, version=prev.version+1, document_type=body.document_type,
        full_text=prev.full_text, lines=prev.lines, fields=fields, provider=prev.provider, model_version=prev.model_version,
        status="CONFIRMED", created_by=who, correction_reason=body.reason)
    db.add(new); d.processing_status="CONFIRMED"; audit(db, tenant.id, who, "EXTRACTION_CONFIRMED", d.id, [d.id],
        before={"version":prev.version,"type":prev.document_type,"fields":prev.fields},
        after={"version":new.version,"type":new.document_type,"fields":new.fields,"reason":body.reason}); db.commit()
    return {"id": new.id, "version": new.version, "status": new.status,
            "fields": public_extraction_fields(new.fields)[0]}

class EventCreate(BaseModel):
    document_ids: list[str] = Field(min_length=2, max_length=20)
    roles: list[str] = Field(min_length=2, max_length=20)
    event_type: str = "PURCHASE"
    reason: str = Field(min_length=5, max_length=500)
    rental_direct_expense_confirmed: bool = False

class EventAssignmentBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

@router.get("/business-events/{event_id}/assignments")
def list_business_event_assignments(event_id: str, db: Session = Depends(db_session),
                                   principal: Principal = Depends(current_principal)):
    require(principal,"manage_members")
    tenant,account=active_scope(db,principal)
    event=db.scalar(select(BusinessEvent).where(BusinessEvent.id==event_id,
        BusinessEvent.tenant_id==tenant.id,BusinessEvent.account_set_id==account.id))
    if event is None:
        raise HTTPException(404,"业务组不存在")
    rows=db.execute(select(BusinessEventAssignment,AppUser)
        .join(AppUser,AppUser.id==BusinessEventAssignment.user_id)
        .where(BusinessEventAssignment.event_id==event.id)
        .order_by(AppUser.email)).all()
    return [{"user_id":user.id,"email":user.email,"display_name":user.display_name}
            for _,user in rows]

@router.post("/business-events")
def create_event(body: EventCreate, db: Session = Depends(db_session), who: str = Depends(actor), principal: Principal = Depends(current_principal)):
    require(principal, "reconcile")
    tenant, account = active_scope(db, principal)
    if body.event_type not in {"PURCHASE", "EXPENSE", "ASSET_PURCHASE", "RENTAL"}:
        raise HTTPException(422, "暂不支持此业务类型")
    if body.event_type == "RENTAL" and body.rental_direct_expense_confirmed:
        require(principal, "manage_accounting_policy")
    request_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    previous_request = db.scalar(select(BusinessEvent).where(
        BusinessEvent.account_set_id == account.id, BusinessEvent.created_by == who,
        BusinessEvent.request_hash == request_hash,
    ))
    if previous_request is not None:
        return {"id": previous_request.id, "status": previous_request.status,
                "documents": [link.document_id for link in previous_request.documents], "idempotent_replay": True}
    if body.event_type == "EXPENSE" and ("PAYMENT_PROOF" not in body.roles or "GOODS_RECEIPT" in body.roles):
        raise HTTPException(422, "费用报销需要支付证明，且不能关联入库单")
    if body.event_type == "ASSET_PURCHASE" and ("INVOICE" not in body.roles or "BANK_RECEIPT" not in body.roles
            or "GOODS_RECEIPT" in body.roles or "PAYMENT_PROOF" in body.roles):
        raise HTTPException(422, "设备购置需关联发票和银行回单，不能混入入库单或支付证明")
    if body.event_type == "RENTAL" and ("INVOICE" not in body.roles or "BANK_RECEIPT" not in body.roles
            or "GOODS_RECEIPT" in body.roles or "PAYMENT_PROOF" in body.roles):
        raise HTTPException(422, "租赁业务需关联发票和银行回单，不能混入入库单或支付证明")
    if len(body.document_ids) != len(body.roles) or len(set(body.document_ids)) != len(body.document_ids): raise HTTPException(422, "单据与角色必须一一对应且不可重复")
    docs = db.scalars(select(DocumentAsset).where(DocumentAsset.id.in_(body.document_ids), DocumentAsset.tenant_id == tenant.id, DocumentAsset.account_set_id == account.id)).all()
    if len(docs) != len(body.document_ids): raise HTTPException(404, "部分单据不存在")
    docs_by_id = {doc.id: doc for doc in docs}
    for document_id, role in zip(body.document_ids, body.roles):
        doc = docs_by_id[document_id]
        extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == doc.id).order_by(DocumentExtraction.version.desc()))
        if extraction is None or extraction.status != "CONFIRMED":
            raise HTTPException(409, f"单据 {doc.original_filename} 尚未人工确认字段")
        if extraction.document_type != role_type(role):
            raise HTTPException(422, f"单据 {doc.original_filename} 的已确认类型与角色 {role} 不匹配")
    configured_policy = latest_accounting_policy(db, account.id, body.event_type)
    if body.event_type == "RENTAL" and body.rental_direct_expense_confirmed and (
            configured_policy is None or configured_policy.configuration.get("accounting_treatment") != "DIRECT_EXPENSE"):
        raise HTTPException(422, "请先配置并确认租赁直接费用政策，再逐笔确认合同适用该政策")
    policy_version = accounting_policy_ref(body.event_type, configured_policy.version) if configured_policy else {
        "EXPENSE": "unconfigured-expense-v1", "ASSET_PURCHASE": "unconfigured-asset-v1",
        "RENTAL": "unconfigured-rental-v1"}.get(body.event_type, "account-set-chart-v1")
    event = BusinessEvent(tenant_id=tenant.id, account_set_id=account.id, event_type=body.event_type, status="NEEDS_REVIEW",
        currency="CNY", policy_version=policy_version, findings=[], created_by=who, request_hash=request_hash,
        rental_direct_expense_confirmed=body.event_type == "RENTAL" and body.rental_direct_expense_confirmed,
        rental_direct_expense_confirmed_by=who if body.event_type == "RENTAL" and body.rental_direct_expense_confirmed else None,
        rental_direct_expense_policy_version=accounting_policy_ref("RENTAL", configured_policy.version)
            if body.event_type == "RENTAL" and body.rental_direct_expense_confirmed and configured_policy else None)
    db.add(event); db.flush()
    for document_id, role in zip(body.document_ids, body.roles):
        extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_id).order_by(DocumentExtraction.version.desc()))
        db.add(BusinessEventDocument(event_id=event.id, document_id=document_id, extraction_version=extraction.version, role=role, reason=body.reason, confirmed_by=who))
    audit(db, tenant.id, who, "BUSINESS_EVENT_CREATED", event.id, body.document_ids,
          after={"event_type":event.event_type,"policy_version":event.policy_version,
                 "rental_direct_expense_confirmed":event.rental_direct_expense_confirmed,
                 "rental_direct_expense_confirmed_by":event.rental_direct_expense_confirmed_by,
                 "rental_direct_expense_policy_version":event.rental_direct_expense_policy_version,
                 "roles":body.roles,"reason":body.reason})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous_request = db.scalar(select(BusinessEvent).where(
            BusinessEvent.account_set_id == account.id, BusinessEvent.created_by == who,
            BusinessEvent.request_hash == request_hash,
        ))
        if previous_request is None:
            raise
        return {"id": previous_request.id, "status": previous_request.status,
                "documents": [link.document_id for link in previous_request.documents], "idempotent_replay": True}
    return {"id": event.id, "status": event.status, "documents": body.document_ids, "idempotent_replay": False}

@router.put("/business-events/{event_id}/assignments")
def assign_business_user(event_id: str, body: EventAssignmentBody,
                         db: Session = Depends(db_session), who: str = Depends(actor),
                         principal: Principal = Depends(current_principal)):
    require(principal,"manage_members")
    tenant,account=active_scope(db,principal)
    event=db.scalar(select(BusinessEvent).where(BusinessEvent.id==event_id,
        BusinessEvent.tenant_id==tenant.id,BusinessEvent.account_set_id==account.id))
    if event is None:
        raise HTTPException(404,"业务组不存在")
    user=db.scalar(select(AppUser).where(AppUser.email==body.email.strip().lower(),AppUser.is_active.is_(True)))
    if user is None:
        raise HTTPException(404,"用户不存在或已停用")
    membership=db.scalar(select(TenantMembership).where(TenantMembership.user_id==user.id,
        TenantMembership.tenant_id==tenant.id,TenantMembership.account_set_id==account.id,
        TenantMembership.role=="BUSINESS_USER"))
    if membership is None:
        raise HTTPException(422,"只能指派到当前企业账套中的业务人员")
    assignment=db.scalar(select(BusinessEventAssignment).where(
        BusinessEventAssignment.event_id==event.id,BusinessEventAssignment.user_id==user.id))
    if assignment is None:
        assignment=BusinessEventAssignment(event_id=event.id,tenant_id=tenant.id,
            account_set_id=account.id,user_id=user.id,assigned_by=who)
        db.add(assignment)
        audit(db,tenant.id,who,"BUSINESS_EVENT_ASSIGNED",event.id,
            [link.document_id for link in event.documents],after={"user_id":user.id,"email":user.email})
        db.commit()
    return {"event_id":event.id,"user_id":user.id,"email":user.email,"status":"ASSIGNED"}

@router.delete("/business-events/{event_id}/assignments/{user_id}")
def unassign_business_user(event_id: str, user_id: str, db: Session = Depends(db_session),
                           who: str = Depends(actor), principal: Principal = Depends(current_principal)):
    require(principal,"manage_members")
    tenant,account=active_scope(db,principal)
    event=db.scalar(select(BusinessEvent).where(BusinessEvent.id==event_id,
        BusinessEvent.tenant_id==tenant.id,BusinessEvent.account_set_id==account.id))
    if event is None:
        raise HTTPException(404,"业务组不存在")
    assignment=db.scalar(select(BusinessEventAssignment).where(
        BusinessEventAssignment.event_id==event.id,BusinessEventAssignment.user_id==user_id,
        BusinessEventAssignment.tenant_id==tenant.id,BusinessEventAssignment.account_set_id==account.id))
    if assignment is None:
        raise HTTPException(404,"指派记录不存在")
    db.delete(assignment)
    audit(db,tenant.id,who,"BUSINESS_EVENT_UNASSIGNED",event.id,
        [link.document_id for link in event.documents],after={"user_id":user_id})
    db.commit()
    return {"event_id":event.id,"user_id":user_id,"status":"UNASSIGNED"}

def role_type(role: str) -> str | None:
    return {"INVOICE":"VAT_INVOICE", "GOODS_RECEIPT":"GOODS_RECEIPT", "BANK_RECEIPT":"BANK_RECEIPT",
            "PAYMENT_PROOF":"PAYMENT_PROOF", "CONTRACT":"CONTRACT"}.get(role)

@router.post("/business-events/{event_id}/reconcile")
def reconcile(event_id: str, db: Session = Depends(db_session), who: str = Depends(actor), principal: Principal = Depends(current_principal)):
    require(principal, "reconcile")
    tenant, account = active_scope(db, principal)
    event = db.scalar(select(BusinessEvent).where(BusinessEvent.id == event_id, BusinessEvent.tenant_id == tenant.id, BusinessEvent.account_set_id == account.id))
    if not event: raise HTTPException(404, "业务组不存在")
    previous_status=event.status
    previous_findings=list(event.findings)
    previous_policy_version=event.policy_version
    docs = db.execute(select(BusinessEventDocument, DocumentAsset).join(DocumentAsset, DocumentAsset.id == BusinessEventDocument.document_id).where(BusinessEventDocument.event_id == event.id)).all()
    previous_versions={link.document_id:link.extraction_version for link, _ in docs}
    changed_links=[]
    for link, doc in docs:
        latest=db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id==doc.id)
            .order_by(DocumentExtraction.version.desc()).limit(1))
        if latest and latest.version>link.extraction_version:
            if latest.status!="CONFIRMED":
                raise HTTPException(409,"关联单据有未确认的新字段版本，请先人工确认")
            changed_links.append((link,latest))
    if changed_links:
        active_draft=db.scalar(select(VoucherDraft.id).where(VoucherDraft.event_id==event.id,
            VoucherDraft.status!="REJECTED").limit(1))
        if active_draft:
            raise HTTPException(409,"业务已有凭证版本，不能直接替换其证据；请先处理凭证版本")
        for link, latest in changed_links:
            link.extraction_version=latest.version
        event.version+=1
    facts=[]
    for link, doc in docs:
        e=db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id==doc.id,DocumentExtraction.version==link.extraction_version))
        if not e or e.status != "CONFIRMED": raise HTTPException(409, "所有关联单据字段必须先人工确认")
        facts.append((link.role, e))
    fields_by_role={}
    for role, extraction in facts:
        fields_by_role.setdefault(role, []).append(extraction.fields)
    findings=[]
    is_expense = event.event_type == "EXPENSE"
    is_asset_purchase = event.event_type == "ASSET_PURCHASE"
    is_rental = event.event_type == "RENTAL"
    configured_policy = latest_accounting_policy(db, account.id, event.event_type) if (is_expense or is_asset_purchase or is_rental) else None
    if configured_policy:
        event.policy_version = accounting_policy_ref(event.event_type, configured_policy.version)
    elif event.event_type == "PURCHASE":
        event.policy_version = "account-set-chart-v1"
    required = ({"INVOICE", "BANK_RECEIPT", "PAYMENT_PROOF"} if is_expense else
                {"INVOICE", "BANK_RECEIPT"} if is_asset_purchase or is_rental else
                {"INVOICE", "GOODS_RECEIPT", "BANK_RECEIPT"})
    missing_roles=sorted(role for role in required if not fields_by_role.get(role))
    duplicated_roles=sorted(role for role in ("CONTRACT",)
                            if len(fields_by_role.get(role, [])) > 1)
    if missing_roles or duplicated_roles:
        findings.append({"code":"REQUIRED_DOCUMENT_ROLES_MISSING_OR_DUPLICATED",
                         "missing_roles":missing_roles,"duplicated_roles":duplicated_roles,
                         "severity":"BLOCKING"})
    expected_types={"INVOICE":"VAT_INVOICE","GOODS_RECEIPT":"GOODS_RECEIPT","BANK_RECEIPT":"BANK_RECEIPT",
                    "PAYMENT_PROOF":"PAYMENT_PROOF","CONTRACT":"CONTRACT"}
    if any(role not in expected_types or extraction.document_type != expected_types[role]
           for role, extraction in facts):
        findings.append({"code":"DOCUMENT_TYPE_ROLE_CONFLICT","severity":"BLOCKING"})
    def value(fields,key): return fields.get(key,{}).get("normalized_value")
    invoices=fields_by_role.get("INVOICE", [])
    goods_receipts=fields_by_role.get("GOODS_RECEIPT", [])
    bank_receipts=fields_by_role.get("BANK_RECEIPT", [])
    payment_proofs=fields_by_role.get("PAYMENT_PROOF", [])
    def amount_total(field_sets, key):
        total=Decimal("0.00")
        for fields in field_sets:
            try: total += Decimal(str(value(fields,key)))
            except Exception: return None
        return total if field_sets else None
    invoice_gross=amount_total(invoices,"gross_amount")
    invoice_net=amount_total(invoices,"net_amount")
    invoice_tax=amount_total(invoices,"tax_amount")
    bank_gross=amount_total(bank_receipts,"gross_amount")
    if is_expense:
        proof_gross = amount_total(payment_proofs, "gross_amount")
        if None in (invoice_gross, bank_gross, proof_gross):
            findings.append({"code":"GROSS_AMOUNT_MISSING_OR_INVALID","severity":"BLOCKING"})
        elif invoice_gross != bank_gross or invoice_gross != proof_gross:
            findings.append({"code":"GROSS_AMOUNT_MISMATCH","severity":"BLOCKING"})
    elif is_asset_purchase or is_rental:
        if None in (invoice_gross, bank_gross):
            findings.append({"code":"GROSS_AMOUNT_MISSING_OR_INVALID","severity":"BLOCKING"})
        elif invoice_gross != bank_gross:
            findings.append({"code":"GROSS_AMOUNT_MISMATCH","severity":"BLOCKING"})
    else:
        receipt_gross=amount_total(goods_receipts,"gross_amount")
        if None in (invoice_gross,receipt_gross,bank_gross):
            findings.append({"code":"GROSS_AMOUNT_MISSING_OR_INVALID","severity":"BLOCKING"})
        elif invoice_gross != receipt_gross or invoice_gross != bank_gross:
            findings.append({"code":"GROSS_AMOUNT_MISMATCH","severity":"BLOCKING"})
    if invoice_net is None or invoice_tax is None or invoice_gross is None or invoice_net + invoice_tax != invoice_gross:
        findings.append({"code":"INVOICE_TAX_OR_NET_MISMATCH","severity":"BLOCKING"})
    if goods_receipts and any(value(fields,"received") not in (True,"true","True") for fields in goods_receipts):
        findings.append({"code":"GOODS_RECEIPT_NOT_CONFIRMED","severity":"BLOCKING"})
    contracts=fields_by_role.get("CONTRACT", [])
    if is_rental and not contracts:
        findings.append({"code":"RENTAL_CONTRACT_MISSING","severity":"REVIEW"})
    if is_rental and configured_policy is not None and configured_policy.configuration.get("accounting_treatment") == "DIRECT_EXPENSE":
        current_rental_policy = accounting_policy_ref("RENTAL", configured_policy.version)
        if not event.rental_direct_expense_confirmed:
            findings.append({"code":"RENTAL_DIRECT_EXPENSE_NOT_CONFIRMED","severity":"REVIEW"})
        elif not rental_direct_expense_confirmation_is_current(event, configured_policy):
            findings.append({"code":"RENTAL_DIRECT_EXPENSE_CONFIRMATION_STALE","severity":"REVIEW",
                             "confirmed_policy_version":event.rental_direct_expense_policy_version,
                             "current_policy_version":current_rental_policy})
    if len(contracts)==1 and goods_receipts:
        try:
            ordered_quantity=Decimal(str(value(contracts[0],"ordered_quantity")))
            received_quantities=[Decimal(str(value(fields,"quantity"))) for fields in goods_receipts]
            received_quantity=sum(received_quantities, Decimal("0"))
            if (not ordered_quantity.is_finite() or not all(quantity.is_finite() for quantity in received_quantities)
                    or ordered_quantity <= 0 or any(quantity < 0 for quantity in received_quantities)):
                raise ValueError("invalid quantity")
        except Exception:
            findings.append({"code":"ORDER_OR_RECEIPT_QUANTITY_MISSING_OR_INVALID","severity":"BLOCKING"})
        else:
            if received_quantity < ordered_quantity:
                findings.append({"code":"GOODS_RECEIPT_QUANTITY_SHORTFALL","ordered_quantity":str(ordered_quantity),
                                 "received_quantity":str(received_quantity),
                                 "missing_quantity":str(ordered_quantity-received_quantity),"severity":"BLOCKING"})
            elif received_quantity > ordered_quantity:
                findings.append({"code":"GOODS_RECEIPT_QUANTITY_EXCEEDS_ORDER","ordered_quantity":str(ordered_quantity),
                                 "received_quantity":str(received_quantity),"severity":"BLOCKING"})
    for fields in bank_receipts:
        settlement_state = normalize_bank_settlement_state(value(fields, "settled"))
        if settlement_state == "NOT_SETTLED":
            findings.append({"code":"BANK_SETTLEMENT_NOT_CONFIRMED","severity":"BLOCKING"})
        elif settlement_state != "SETTLED":
            findings.append({"code":"BANK_SETTLEMENT_STATUS_UNKNOWN","severity":"REVIEW"})
    if (is_expense or is_asset_purchase or is_rental) and configured_policy is None:
        policy_finding = "EXPENSE_ACCOUNT_POLICY_REVIEW_REQUIRED" if is_expense else \
            "ASSET_CAPITALIZATION_POLICY_REVIEW_REQUIRED" if is_asset_purchase else "RENTAL_ACCOUNT_POLICY_REVIEW_REQUIRED"
        findings.append({"code":policy_finding,"severity":"REVIEW"})
    elif configured_policy is not None:
        if not configured_policy.configuration.get("allowed_account_codes"):
            findings.append({"code": "ACCOUNTING_POLICY_RECONFIRM_REQUIRED", "severity": "REVIEW"})
        for code in configured_policy.configuration.get("allowed_account_codes", []):
            if not code:
                continue
            account_row = db.scalar(select(ChartAccount).where(
                ChartAccount.account_set_id == account.id, ChartAccount.code == code,
                ChartAccount.is_active.is_(True), ChartAccount.is_leaf.is_(True)))
            if account_row is None:
                findings.append({"code":"ACCOUNTING_POLICY_ACCOUNT_UNAVAILABLE","account_code":code,"severity":"REVIEW"})
    parsed_dates={}
    date_sets=[("INVOICE",fields,"issue_date") for fields in invoices]
    date_sets.extend(("GOODS_RECEIPT",fields,"receipt_date") for fields in goods_receipts)
    date_sets.extend(("BANK_RECEIPT",fields,"settlement_date") for fields in bank_receipts)
    date_sets.extend(("PAYMENT_PROOF",fields,"payment_date") for fields in payment_proofs)
    for role,fields,key in date_sets:
        try: parsed_dates.setdefault(role,[]).append(date.fromisoformat(str(value(fields,key))))
        except (ValueError,TypeError): findings.append({"code":"DATE_EVIDENCE_MISSING_OR_INVALID","role":role,"severity":"BLOCKING"})
    all_dates=[day for values in parsed_dates.values() for day in values]
    if len(all_dates) != len(date_sets) or (all_dates and len({d.strftime("%Y-%m") for d in all_dates})!=1):
        findings.append({"code":"DOCUMENT_PERIOD_MISMATCH","severity":"BLOCKING"})
    invoice_buyers=[value(fields,"buyer") for fields in invoices]
    invoice_sellers=[value(fields,"seller") for fields in invoices]
    bank_payers=[value(fields,"payer") for fields in bank_receipts]
    bank_payees=[value(fields,"payee") for fields in bank_receipts]
    if (not invoices or not bank_receipts or any(not item for item in invoice_buyers + invoice_sellers + bank_payers + bank_payees)):
        findings.append({"code":"PARTY_EVIDENCE_MISSING","severity":"BLOCKING"})
    else:
        if len(set(invoice_buyers)) != 1 or len(set(bank_payers)) != 1 or set(invoice_buyers) != set(bank_payers):
            findings.append({"code":"PAYER_BUYER_MISMATCH","severity":"BLOCKING"})
        if len(set(invoice_sellers)) != 1 or len(set(bank_payees)) != 1 or set(invoice_sellers) != set(bank_payees):
            findings.append({"code":"PAYEE_SELLER_MISMATCH","severity":"BLOCKING"})
    blocking_findings = any(finding.get("severity") == "BLOCKING" for finding in findings)
    equal=not findings and invoice_gross is not None
    event.amount=invoice_gross if invoice_gross is not None and not blocking_findings else None; event.findings=findings
    event.status="MATCHED" if equal else "NEEDS_REVIEW"
    if findings:
        open_case=db.scalar(select(ExceptionCase).where(ExceptionCase.tenant_id==tenant.id,ExceptionCase.event_id==event.id,ExceptionCase.case_type=="RECONCILIATION",ExceptionCase.status=="OPEN"))
        case_severity = "BLOCKING" if blocking_findings else "REVIEW"
        if open_case is None: db.add(ExceptionCase(tenant_id=tenant.id,event_id=event.id,case_type="RECONCILIATION",status="OPEN",severity=case_severity,details={"findings":findings},source_refs=[x[0].document_id for x in docs]))
        else:
            open_case.details={"findings":findings}
            open_case.severity=case_severity
    else:
        open_case=db.scalar(select(ExceptionCase).where(ExceptionCase.tenant_id==tenant.id,ExceptionCase.event_id==event.id,ExceptionCase.case_type=="RECONCILIATION",ExceptionCase.status=="OPEN"))
        if open_case is not None: open_case.status="RESOLVED";open_case.resolved_at=datetime.now(timezone.utc)
    audit(db,tenant.id,who,"BUSINESS_EVENT_RECONCILED",event.id,[x[0].document_id for x in docs],
        before={"status":previous_status,"findings":previous_findings,"policy_version":previous_policy_version,
                "extraction_versions":previous_versions},
        after={"status":event.status,"findings":event.findings,"amount":event.amount,"version":event.version,
               "policy_version":event.policy_version,
               "extraction_versions":{link.document_id:link.extraction_version for link,_ in docs}});db.commit()
    if configured_policy:
        note = f"本次核对使用财务负责人确认的账套政策 {event.policy_version}；核对结果和凭证仍需人工审批，未写入 ERP。"
    elif is_expense:
        note = "费用科目政策尚未配置并确认；支付证明不代替银行清算证据。"
    elif is_asset_purchase:
        note = "固定资产资本化政策尚未配置并确认；当前不会据此制证。"
    elif is_rental:
        note = "租赁会计政策尚未配置并确认；合同和租赁处理方式需要财务确认。"
    else:
        note = "单据证据已核对；凭证科目由 DeepSeek 从当前账套有效科目中提出，仍需人工审核。"
    return {"id":event.id,"status":event.status,"amount":str(event.amount) if event.amount is not None else None,
            "rule_version":event.policy_version,"findings":event.findings,
            "note":note}

@router.post("/business-events/{event_id}/voucher-drafts")
def create_voucher(event_id: str, db: Session = Depends(db_session), who: str = Depends(actor), principal: Principal = Depends(current_principal),
                   suggestion_provider: AccountingSuggestionProvider = Depends(get_accounting_suggestion_provider)):
    require(principal, "voucher_create")
    tenant, account=active_scope(db, principal)
    event=db.scalar(select(BusinessEvent).where(BusinessEvent.id==event_id,BusinessEvent.tenant_id==tenant.id,BusinessEvent.account_set_id==account.id))
    if not event: raise HTTPException(404,"业务组不存在")
    configured_policy = latest_accounting_policy(db, account.id, event.event_type) if event.event_type in {"EXPENSE", "ASSET_PURCHASE", "RENTAL"} else None
    if event.event_type in {"EXPENSE", "ASSET_PURCHASE", "RENTAL"}:
        if configured_policy is None:
            raise HTTPException(422, "该业务类型尚未配置并确认账套会计政策，当前不能生成凭证草稿")
        if not configured_policy.configuration.get("allowed_account_codes"):
            raise HTTPException(422, "旧版会计政策缺少允许科目范围，请财务负责人重新确认")
        current_policy_ref = accounting_policy_ref(event.event_type, configured_policy.version)
        if event.policy_version != current_policy_ref:
            raise HTTPException(409, "会计政策已更新，请重新运行核对后再生成凭证")
    if event.event_type == "RENTAL" and configured_policy is not None and configured_policy.configuration.get("accounting_treatment") == "DIRECT_EXPENSE":
        if not rental_direct_expense_confirmation_is_current(event, configured_policy):
            raise HTTPException(422, "财务尚未按当前租赁政策逐笔确认该合同，不能生成凭证草稿")
    if event.status!="MATCHED" or event.amount is None:
        raise HTTPException(422,"业务核对未通过，不能生成凭证草稿")
    previous=db.scalar(select(VoucherDraft).where(VoucherDraft.event_id==event.id,VoucherDraft.status!="REJECTED").order_by(VoucherDraft.version.desc()).limit(1))
    if previous is not None:
        audit(db,tenant.id,who,"VOUCHER_DUPLICATE_CREATION_BLOCKED",previous.id,
              [document.document_id for document in event.documents],
              after={"event_id":event.id,"existing_version":previous.version})
        db.commit()
        raise HTTPException(409,"此业务组已有未拒绝的凭证草稿，不能重复生成")
    amount=event.amount
    invoice_links=[link for link in event.documents if link.role=="INVOICE"]
    invoice_extractions=[db.scalar(select(DocumentExtraction).where(
        DocumentExtraction.document_id==link.document_id,
        DocumentExtraction.version==link.extraction_version)) for link in invoice_links]
    source_extractions = [db.scalar(select(DocumentExtraction).where(
        DocumentExtraction.document_id == link.document_id,
        DocumentExtraction.version == link.extraction_version)) for link in event.documents]
    if any(extraction is None or extraction.status != "CONFIRMED" for extraction in source_extractions):
        raise HTTPException(409, "关联证据的确认版本不可用，请重新确认并核对")
    if not invoice_extractions:
        raise HTTPException(409,"关联发票抽取版本不可用，请重新确认并核对")
    invoice_fields_list=[extraction.fields for extraction in invoice_extractions]
    invoice_fields=invoice_fields_list[0]
    try:
        accounting_date=date.fromisoformat(str(invoice_fields["issue_date"]["normalized_value"]))
        accounting_period=accounting_date.strftime("%Y-%m")
        for fields in invoice_fields_list[1:]:
            other_date=date.fromisoformat(str(fields["issue_date"]["normalized_value"]))
            if other_date.strftime("%Y-%m")!=accounting_period:
                raise ValueError("多张发票不在同一会计期间")
        period_row=db.scalar(select(AccountingPeriod).where(AccountingPeriod.account_set_id==account.id,AccountingPeriod.period==accounting_period))
        if period_row is None or period_row.status!="OPEN":
            raise ValueError(f"会计期间 {accounting_period} 未开放")
        invoice_identities=[]
        for fields in invoice_fields_list:
            invoice_number=str(fields.get("invoice_number",{}).get("normalized_value") or "").strip()
            seller=str(fields.get("seller",{}).get("normalized_value") or "").strip()
            issue_date=str(fields.get("issue_date",{}).get("normalized_value") or "")
            if not invoice_number or not seller or not issue_date:
                raise ValueError("发票号码、销售方或开票日期缺失")
            invoice_identities.append((invoice_number,issue_date[:7],seller))
        duplicate_ref=None
        duplicate_invoice_number=None
        seen_invoice_identities=set()
        for link,identity in zip(invoice_links,invoice_identities):
            if identity in seen_invoice_identities:
                duplicate_ref=link.document_id
                duplicate_invoice_number=identity[0]
                break
            seen_invoice_identities.add(identity)
        first_source_link_id = min(link.id for link in event.documents)
        linked_invoices=db.execute(select(BusinessEventDocument,BusinessEvent).join(BusinessEvent,BusinessEvent.id==BusinessEventDocument.event_id)
            .where(BusinessEvent.tenant_id==tenant.id,BusinessEvent.account_set_id==account.id,
                   BusinessEventDocument.role=="INVOICE",BusinessEvent.id!=event.id,
                   BusinessEventDocument.id < first_source_link_id)).all()
        for link,other_event in linked_invoices:
            other_extraction=db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id==link.document_id,
                                                                          DocumentExtraction.version==link.extraction_version))
            other_number=(other_extraction.fields.get("invoice_number",{}).get("normalized_value") if other_extraction else None)
            other_date=(other_extraction.fields.get("issue_date",{}).get("normalized_value") if other_extraction else None)
            other_seller=(other_extraction.fields.get("seller",{}).get("normalized_value") if other_extraction else None)
            other_identity=(str(other_number).strip(),str(other_date or "")[:7],str(other_seller or "").strip())
            if duplicate_ref is None and other_number and other_identity in invoice_identities:
                duplicate_ref=link.document_id
                duplicate_invoice_number=other_identity[0]
                break
        if duplicate_ref:
            duplicate_case=db.scalar(select(ExceptionCase).where(ExceptionCase.tenant_id==tenant.id,ExceptionCase.event_id==event.id,
                ExceptionCase.case_type=="DUPLICATE_INVOICE",ExceptionCase.status=="OPEN"))
            if duplicate_case is None:
                db.add(ExceptionCase(tenant_id=tenant.id,event_id=event.id,case_type="DUPLICATE_INVOICE",status="OPEN",
                    severity="BLOCKING",details={"invoice_number_hash":hashlib.sha256(duplicate_invoice_number.encode()).hexdigest()},
                    source_refs=[duplicate_ref,*[x.document_id for x in event.documents]]))
            audit(db,tenant.id,who,"DUPLICATE_INVOICE_BLOCKED",event.id,[duplicate_ref,*[x.document_id for x in event.documents]])
            db.commit()
            raise HTTPException(409,"检测到本账套重复发票号，已生成异常工单，不能重复制单")
        net=sum((Decimal(str(fields["net_amount"]["normalized_value"])) for fields in invoice_fields_list),Decimal("0.00"))
        tax=sum((Decimal(str(fields["tax_amount"]["normalized_value"])) for fields in invoice_fields_list),Decimal("0.00"))
        invoice_gross=sum((Decimal(str(fields["gross_amount"]["normalized_value"])) for fields in invoice_fields_list),Decimal("0.00"))
        if invoice_gross!=amount:
            raise ValueError("关联发票金额与已核对业务金额不一致，请重新核对")
        evidence_refs = tuple(x.document_id for x in event.documents)
        if configured_policy is not None:
            configured = dict(configured_policy.configuration)
            policy_ref = accounting_policy_ref(event.event_type, configured_policy.version)
            policy_summary = {
                "policy_ref": policy_ref,
                "tax_treatment": str(configured.get("tax_treatment", "")),
                "accounting_treatment": str(configured.get("accounting_treatment", "")),
            }
            policy_summary["input_tax_account_codes"] = list(configured["input_tax_account_codes"])
            policy_summary["allowed_account_codes"] = list(configured["allowed_account_codes"])
            business_use = configured.get("business_use")
            if isinstance(business_use, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", business_use):
                policy_summary["business_use"] = business_use
        else:
            policy_ref = "account-set-chart-v1"
            policy_summary = {"policy_ref": policy_ref}
        available_accounts = tuple(SuggestedAccount(row.code, row.name) for row in db.scalars(
            select(ChartAccount).where(ChartAccount.account_set_id == account.id,
                ChartAccount.is_active.is_(True), ChartAccount.is_leaf.is_(True)).order_by(ChartAccount.code)).all())
        confirmed_facts = {
            "business_type": event.event_type,
            "currency": account.currency,
            "issue_date": accounting_date.isoformat(),
            "net_amount": str(net), "tax_amount": str(tax), "gross_amount": str(amount),
        }
        for field in ("item_category", "purpose_category", "payment_state", "quantity"):
            raw_value = invoice_fields.get(field, {}).get("normalized_value")
            if raw_value is None:
                continue
            if field == "quantity":
                try:
                    confirmed_facts[field] = str(Decimal(str(raw_value)))
                except Exception:
                    continue
            elif isinstance(raw_value, str):
                confirmed_facts[field] = raw_value.strip().upper() if field == "payment_state" else raw_value.strip()
        source_documents = tuple({
            "evidence_ref": link.document_id,
            "role": link.role,
            "fields": {key: value["normalized_value"] for key, value in extraction.fields.items()
                       if key in SOURCE_FACT_FIELDS and isinstance(value, dict)
                       and value.get("normalized_value") is not None},
        } for link, extraction in zip(event.documents, source_extractions))
        reconciliation_codes = tuple(sorted({"BUSINESS_RECONCILIATION_MATCHED", *(
            str(item.get("code")) for item in (event.findings or [])
            if item.get("passed") is True and isinstance(item.get("code"), str))}))
        context = VoucherSuggestionContext(event.event_type, confirmed_facts, reconciliation_codes,
            available_accounts, policy_summary, source_documents)
        suggestion = suggestion_provider.suggest(net=net, tax=tax, gross=amount,
            evidence_refs=evidence_refs, policy={"policy_ref": policy_ref, **policy_summary}, context=context)
    except SuggestionProviderError as exc:
        trace = exc.trace
        if trace is not None:
            audit(db, tenant.id, who, "VOUCHER_AI_GENERATION_FAILED", event.id, evidence_refs,
                after={"provider": trace.provider, "model": trace.model_version,
                       "prompt_version": trace.prompt_version, "input_digest": trace.input_digest,
                       "output_digest": trace.output_digest, "call_status": trace.call_status,
                       "validation_status": trace.validation_status})
            db.commit()
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, f"凭证建议输入或输出无效：{exc}") from exc
    suggestion_accounts = {}
    auxiliary_requirements = {}
    for line in suggestion.lines:
        account_row = db.scalar(select(ChartAccount).where(
            ChartAccount.account_set_id == account.id,
            ChartAccount.code == line.account_code,
            ChartAccount.is_active.is_(True),
            ChartAccount.is_leaf.is_(True),
        ).with_for_update())
        if account_row is None:
            raise HTTPException(422, f"科目不可用：{line.account_code}")
        if account_row.requires_auxiliary:
            if not account_row.required_auxiliary_fields:
                raise HTTPException(422, f"科目 {line.account_code} 尚未配置辅助核算维度，不能生成凭证")
            auxiliary_requirements[line.account_code] = list(account_row.required_auxiliary_fields)
        suggestion_accounts[line.account_code] = account_row
    validate_voucher_amounts_and_accounts(suggestion.lines,
        configured_policy.configuration if configured_policy else None, net, tax, amount)
    debit = sum((line.debit for line in suggestion.lines), Decimal("0.00"))
    credit = sum((line.credit for line in suggestion.lines), Decimal("0.00"))
    tax_codes = (set(configured_policy.configuration["input_tax_account_codes"])
                 if configured_policy and policy_summary["tax_treatment"] == "DEDUCTIBLE" else set())
    validate_voucher_evidence(suggestion.lines, event.documents, tax_codes)
    if any(not line.rationale.strip() for line in suggestion.lines):
        raise HTTPException(422, "AI 凭证每条分录必须提供理由")
    source_docs=[x.document_id for x in event.documents]
    import json
    source_versions=sorted((link.document_id,link.extraction_version) for link in event.documents)
    source_hash=hashlib.sha256(json.dumps(source_versions,separators=(",",":")).encode()).hexdigest()
    version=(db.scalar(select(VoucherDraft.version).where(VoucherDraft.event_id==event.id).order_by(VoucherDraft.version.desc()).limit(1)) or 0)+1
    auxiliary_missing = bool(auxiliary_requirements)
    draft_status = "NEEDS_REVIEW" if auxiliary_missing else "VALIDATED"
    validations = [
        {"code":"SOURCE_CURRENT","passed":True},
        {"code":"INVOICE_NET_TAX_GROSS","passed":True},
        {"code":"ACCOUNT_PERIOD_OPEN","passed":True},
        {"code":"POLICY_CURRENT","passed":True},
        {"code":"EVIDENCE_REFERENCES","passed":True},
        {"code":"AMOUNTS_MATCHED","passed":True},
        {"code":"BALANCED","passed":True},
        {"code":"CHART_ACCOUNTS","passed":True},
    ]
    if auxiliary_missing:
        validations.append({"code":"AUXILIARY_COMPLETE","passed":False,
            "required_fields":auxiliary_requirements,"reason":"凭证候选没有提供必填辅助核算，需要人工补充"})
    else:
        validations.append({"code":"AUXILIARY_COMPLETE","passed":True})
    trace = suggestion.trace
    voucher_policy = {"kind":"AI_CANDIDATE", "provider":suggestion.provider,
        "model":trace.model_version if trace else settings.deepseek_model,
        "policy_ref":suggestion.policy_ref, "business_type":event.event_type,
        "confirmed_by":configured_policy.confirmed_by if configured_policy is not None else None,
        "input_vat_deductible":policy_summary.get("tax_treatment") == "DEDUCTIBLE" if configured_policy else None,
        "policy_summary":policy_summary, "policy_explanation":suggestion.policy_explanation,
        "warnings":list(suggestion.warnings), "input_snapshot_hash":trace.input_digest if trace else None,
        "output_hash":trace.output_digest if trace else None,
        "prompt_version":trace.prompt_version if trace else None,
        "trace":{"call_status":trace.call_status,"validation_status":trace.validation_status} if trace else None,
        "source_snapshot_hash":source_hash,
        "source_snapshot":[{"document_id":link.document_id,"extraction_version":link.extraction_version,
            "role":link.role} for link in sorted(event.documents, key=lambda item: item.document_id)],
        "auxiliary_requirements":auxiliary_requirements}
    draft=VoucherDraft(event_id=event.id,tenant_id=tenant.id,account_set_id=account.id,accounting_period=accounting_period,
        accounting_date=accounting_date,status=draft_status,version=version,source_hash=source_hash,
        policy=voucher_policy,
        validations=validations,debit_total=debit,credit_total=credit,created_by=who)
    db.add(draft)
    try:
        db.flush()
        for i,line in enumerate(suggestion.lines,1):
            account_row = suggestion_accounts[line.account_code]
            db.add(VoucherDraftLine(draft_id=draft.id,sequence=i,account_code=line.account_code,account_name=account_row.name,debit=line.debit,credit=line.credit,description=line.description,rationale=line.rationale,evidence_refs=list(line.evidence_refs)))
        audit(db,tenant.id,who,"VOUCHER_AI_CANDIDATE_CREATED",draft.id,source_docs,
              after={"version":draft.version,"source_hash":draft.source_hash,"status":draft.status,
                     "provider":suggestion.provider,"model":voucher_policy["model"],
                     "prompt_version":voucher_policy["prompt_version"],
                     "input_snapshot_hash":voucher_policy["input_snapshot_hash"],
                     "output_hash":voucher_policy["output_hash"],
                     "debit_total":draft.debit_total,"credit_total":draft.credit_total})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent_draft = db.scalar(select(VoucherDraft).where(
            VoucherDraft.event_id == event.id, VoucherDraft.status != "REJECTED",
        ).order_by(VoucherDraft.version.desc()).limit(1))
        if concurrent_draft is None:
            raise
        audit(db, tenant.id, who, "VOUCHER_DUPLICATE_CREATION_BLOCKED", concurrent_draft.id,
              source_docs, after={"event_id": event.id, "existing_version": concurrent_draft.version,
                                  "reason": "concurrent request"})
        db.commit()
        raise HTTPException(409, "此业务组已有未拒绝的凭证草稿，不能重复生成") from exc
    return voucher_json(draft)

def voucher_json(d):
    return {"id":d.id,"event_id":d.event_id,"version":d.version,"status":d.status,"suggestion_type":"AI 生成凭证草稿",
        "policy":d.policy,"period":d.accounting_period,"debit_total":str(d.debit_total),"credit_total":str(d.credit_total),
        "validations":d.validations,
        "lines":[{"account_code":l.account_code,"account_name":l.account_name,"debit":str(l.debit),"credit":str(l.credit),"description":l.description,"rationale":l.rationale,"auxiliary":l.auxiliary,"required_auxiliary_fields":(d.policy.get("auxiliary_requirements") or {}).get(l.account_code, []),"evidence_refs":l.evidence_refs} for l in d.lines]}


def require_auxiliary_complete(db: Session, draft: VoucherDraft) -> None:
    requirements = draft.policy.get("auxiliary_requirements") or {}
    for line in draft.lines:
        account_row = db.scalar(select(ChartAccount).where(
            ChartAccount.account_set_id == draft.account_set_id,
            ChartAccount.code == line.account_code,
        ).with_for_update())
        if account_row is None:
            raise HTTPException(422, f"科目不存在：{line.account_code}")
        if not account_row.is_active or not account_row.is_leaf:
            raise HTTPException(422, f"科目不可用或不是末级科目：{line.account_code}")
        if account_row.requires_auxiliary and not account_row.required_auxiliary_fields:
            raise HTTPException(422, f"科目 {line.account_code} 尚未配置辅助核算维度，不能提交审核")
        configured = set(account_row.required_auxiliary_fields or []) if account_row.requires_auxiliary else set()
        snapshot = set(requirements.get(line.account_code, []))
        provided = set(line.auxiliary or {})
        if configured != snapshot:
            raise HTTPException(409, f"科目 {line.account_code} 的辅助核算政策与凭证版本不一致，请重新生成候选")
        if configured != provided:
            missing = sorted(configured - provided)
            unexpected = sorted(provided - configured)
            details = []
            if missing:
                details.append(f"缺少：{', '.join(missing)}")
            if unexpected:
                details.append(f"未配置：{', '.join(unexpected)}")
            raise HTTPException(422, f"科目 {line.account_code} 辅助核算维度不完整（{'；'.join(details)}）")


class VoucherLineCorrection(BaseModel):
    account_code: str = Field(min_length=1, max_length=40)
    debit: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    credit: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    description: str = Field(min_length=1, max_length=500)
    auxiliary: dict[str, str] = Field(default_factory=dict, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("auxiliary")
    @classmethod
    def validate_auxiliary_values(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {}
        for raw_key, raw_value in value.items():
            key = raw_key.strip()
            item = raw_value.strip()
            if not key or not item:
                raise ValueError("辅助核算的项目名称和值均不能为空")
            if key in normalized:
                raise ValueError(f"辅助核算维度重复：{key}")
            normalized[key] = item
        return normalized


class VoucherCorrectionBody(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)
    lines: list[VoucherLineCorrection] = Field(min_length=2, max_length=100)


@router.post("/voucher-drafts/{draft_id}/versions")
def correct_voucher(draft_id: str, body: VoucherCorrectionBody, db: Session = Depends(db_session),
                    who: str = Depends(actor), principal: Principal = Depends(current_principal)):
    require(principal, "voucher_create")
    tenant, account = active_scope(db, principal)
    previous = db.scalar(select(VoucherDraft).where(VoucherDraft.id == draft_id,
        VoucherDraft.tenant_id == tenant.id, VoucherDraft.account_set_id == account.id))
    if previous is None:
        raise HTTPException(404, "凭证草稿不存在")
    if previous.version != body.expected_version:
        raise HTTPException(409, "凭证版本已变化，请刷新后再修改")
    if previous.status in {"PENDING_APPROVAL", "SUBMITTING", "POSTED_PENDING_VERIFY", "VERIFIED", "UNKNOWN", "FAILED_POSTING", "EXPORTED_PENDING_MANUAL_POST"}:
        raise HTTPException(409, "当前凭证正在审核或已导出，不能直接修正；请先处理当前流程")
    event = db.scalar(select(BusinessEvent).where(BusinessEvent.id == previous.event_id,
        BusinessEvent.tenant_id == tenant.id, BusinessEvent.account_set_id == account.id).with_for_update())
    if event is None or event.status != "MATCHED":
        raise HTTPException(409, "原业务核对已失效，请先重新核对")
    current_policy = latest_accounting_policy(db, account.id, event.event_type) if event.event_type in {"EXPENSE", "ASSET_PURCHASE", "RENTAL"} else None
    if event.event_type in {"EXPENSE", "ASSET_PURCHASE", "RENTAL"}:
        if current_policy is None or event.policy_version != accounting_policy_ref(event.event_type, current_policy.version):
            raise HTTPException(409, "会计政策已变化，请重新核对后再修正凭证")
        if not current_policy.configuration.get("allowed_account_codes"):
            raise HTTPException(422, "旧版会计政策缺少允许科目范围，请财务负责人重新确认")
    latest_version = db.scalar(select(VoucherDraft.version).where(VoucherDraft.event_id == event.id)
        .order_by(VoucherDraft.version.desc()).limit(1))
    if latest_version != previous.version:
        raise HTTPException(409, "只能基于这笔业务的最新凭证版本继续修正")
    source_versions = sorted((link.document_id, link.extraction_version) for link in event.documents)
    current_hash = hashlib.sha256(json.dumps(source_versions, separators=(",", ":")).encode()).hexdigest()
    if current_hash != previous.source_hash:
        raise HTTPException(409, "来源单据版本已变化，请重新核对并生成凭证")
    invoice_links = [link for link in event.documents if link.role == "INVOICE"]
    invoice_extractions = [db.scalar(select(DocumentExtraction).where(
        DocumentExtraction.document_id == link.document_id,
        DocumentExtraction.version == link.extraction_version)) for link in invoice_links]
    if not invoice_extractions or any(extraction is None or extraction.status != "CONFIRMED" for extraction in invoice_extractions):
        raise HTTPException(409, "关联发票的确认版本不可用，请重新核对")
    try:
        net_total = sum((Decimal(str(extraction.fields["net_amount"]["normalized_value"]))
                         for extraction in invoice_extractions), Decimal("0.00"))
        tax_total = sum((Decimal(str(extraction.fields["tax_amount"]["normalized_value"]))
                         for extraction in invoice_extractions), Decimal("0.00"))
        gross_total = sum((Decimal(str(extraction.fields["gross_amount"]["normalized_value"]))
                           for extraction in invoice_extractions), Decimal("0.00"))
        issue_months = {date.fromisoformat(str(extraction.fields["issue_date"]["normalized_value"])).strftime("%Y-%m")
                        for extraction in invoice_extractions}
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "关联发票缺少可用的金额或日期字段，请重新核对") from exc
    if len(issue_months) != 1 or issue_months != {previous.accounting_period}:
        raise HTTPException(422, "关联发票必须属于凭证会计期间")
    if net_total + tax_total != gross_total or gross_total != event.amount:
        raise HTTPException(422, "修正前请先解决发票未税金额、税额和业务总额不一致的问题")
    period = db.scalar(select(AccountingPeriod).where(AccountingPeriod.account_set_id == account.id,
        AccountingPeriod.period == previous.accounting_period))
    if period is None or period.status != "OPEN":
        raise HTTPException(422, f"会计期间 {previous.accounting_period} 未开放")
    debit = sum((line.debit for line in body.lines), Decimal("0.00"))
    credit = sum((line.credit for line in body.lines), Decimal("0.00"))
    validate_voucher_amounts_and_accounts(body.lines,
        current_policy.configuration if current_policy else None, net_total, tax_total, gross_total)
    resolved_lines = []
    for line in body.lines:
        account_row = db.scalar(select(ChartAccount).where(ChartAccount.account_set_id == account.id,
            ChartAccount.code == line.account_code, ChartAccount.is_active.is_(True), ChartAccount.is_leaf.is_(True)).with_for_update())
        if account_row is None:
            raise HTTPException(422, f"科目不可用：{line.account_code}")
        required_auxiliary = set(account_row.required_auxiliary_fields or [])
        provided_auxiliary = set(line.auxiliary)
        if account_row.requires_auxiliary and not required_auxiliary:
            raise HTTPException(422, f"科目 {line.account_code} 尚未配置辅助核算维度，不能通过校验")
        if account_row.requires_auxiliary and required_auxiliary != provided_auxiliary:
            missing = sorted(required_auxiliary - provided_auxiliary)
            unexpected = sorted(provided_auxiliary - required_auxiliary)
            details = []
            if missing:
                details.append(f"缺少：{', '.join(missing)}")
            if unexpected:
                details.append(f"未配置：{', '.join(unexpected)}")
            raise HTTPException(422, f"科目 {line.account_code} 辅助核算维度不完整（{'；'.join(details)}）")
        if not account_row.requires_auxiliary and line.auxiliary:
            raise HTTPException(422, f"科目 {line.account_code} 未配置辅助核算维度，不能提交额外辅助核算")
        resolved_lines.append((line, account_row))
    tax_codes = set(current_policy.configuration["input_tax_account_codes"]) if current_policy else set()
    validate_voucher_evidence(body.lines, event.documents, tax_codes)
    corrected = VoucherDraft(event_id=event.id, tenant_id=tenant.id, account_set_id=account.id,
        accounting_period=previous.accounting_period, accounting_date=previous.accounting_date,
        status="VALIDATED", version=latest_version + 1, source_hash=current_hash,
        policy={**previous.policy, "corrected_from": {"draft_id": previous.id, "version": previous.version},
                "auxiliary_requirements": {account_row.code: list(account_row.required_auxiliary_fields or [])
                    for _, account_row in resolved_lines if account_row.requires_auxiliary},
                "correction_reason": body.reason},
        validations=[{"code": "BALANCED", "passed": True}, {"code": "CHART_ACCOUNTS", "passed": True},
                     {"code": "AUXILIARY_COMPLETE", "passed": True},
                     {"code": "SOURCE_CURRENT", "passed": True}, {"code": "INVOICE_NET_TAX_GROSS", "passed": True},
                     {"code": "POLICY_CURRENT", "passed": True}, {"code": "ACCOUNT_PERIOD_OPEN", "passed": True}],
        debit_total=debit, credit_total=credit, created_by=who)
    try:
        db.add(corrected)
        db.flush()
        for sequence, (line, account_row) in enumerate(resolved_lines, 1):
            db.add(VoucherDraftLine(draft_id=corrected.id, sequence=sequence, account_code=account_row.code,
                account_name=account_row.name, debit=line.debit, credit=line.credit,
                description=line.description, auxiliary=line.auxiliary, evidence_refs=line.evidence_refs))
        audit(db, tenant.id, who, "VOUCHER_CORRECTED_NEW_VERSION", corrected.id,
            [ref for line in body.lines for ref in line.evidence_refs],
            before={"draft_id": previous.id, "version": previous.version, "status": previous.status},
            after={"draft_id": corrected.id, "version": corrected.version, "status": corrected.status,
                   "reason": body.reason, "debit_total": str(debit), "credit_total": str(credit)})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "凭证版本已被其他操作更新，请刷新后重试") from exc
    return voucher_json(corrected)

@router.post("/voucher-drafts/{draft_id}/submit-review")
def submit_review(draft_id:str,db:Session=Depends(db_session),who:str=Depends(actor),principal:Principal=Depends(current_principal)):
    require(principal,"voucher_submit")
    tenant,account=active_scope(db,principal);d=db.scalar(select(VoucherDraft).where(VoucherDraft.id==draft_id,VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id))
    if not d:raise HTTPException(404,"凭证不存在")
    if d.status!="VALIDATED":raise HTTPException(409,"凭证状态不允许提交")
    require_auxiliary_complete(db, d)
    before=d.status;d.status="PENDING_APPROVAL";audit(db,tenant.id,who,"VOUCHER_SUBMITTED",d.id,before={"status":before,"version":d.version},after={"status":d.status,"version":d.version});db.commit();return voucher_json(d)

class Decision(BaseModel): decision:str=Field(pattern="^(APPROVE|REJECT)$"); reason:str|None=None
@router.post("/voucher-drafts/{draft_id}/approve")
def approve(draft_id:str,body:Decision,db:Session=Depends(db_session),who:str=Depends(actor),principal:Principal=Depends(current_principal)):
    require(principal,"approve")
    tenant,account=active_scope(db,principal);d=db.scalar(select(VoucherDraft).where(VoucherDraft.id==draft_id,VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id))
    if not d:raise HTTPException(404,"凭证不存在")
    if d.status!="PENDING_APPROVAL":raise HTTPException(409,"凭证尚未待审")
    require_auxiliary_complete(db, d)
    if d.created_by==who:raise HTTPException(403,"制单人与审批人必须不同")
    before=d.status;d.status="APPROVED" if body.decision=="APPROVE" else "REJECTED";d.approved_by=who;d.approved_at=datetime.now(timezone.utc)
    db.add(VoucherApproval(draft_id=d.id,draft_version=d.version,approver=who,decision=body.decision,reason=body.reason))
    audit(db,tenant.id,who,"VOUCHER_"+body.decision,d.id,before={"status":before,"version":d.version},after={"status":d.status,"version":d.version,"approver":who,"reason":body.reason});db.commit();return voucher_json(d)

@router.post("/voucher-drafts/{draft_id}/export")
def export_voucher(draft_id:str,db:Session=Depends(db_session),who:str=Depends(actor),principal:Principal=Depends(current_principal)):
    require(principal,"export")
    tenant,account=active_scope(db,principal);d=db.scalar(select(VoucherDraft).where(VoucherDraft.id==draft_id,VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id))
    if not d:raise HTTPException(404,"凭证不存在")
    if d.status!="APPROVED":raise HTTPException(409,"仅可导出已审批版本")
    require_auxiliary_complete(db, d)
    artifact=GenericCsvVoucherExporter().export(VoucherExportData(
        voucher_id=d.id,
        version=d.version,
        period=d.accounting_period,
        lines=tuple(VoucherExportLine(
            account_code=line.account_code,
            account_name=line.account_name,
            debit=line.debit,
            credit=line.credit,
            description=line.description,
            auxiliary=line.auxiliary,
        ) for line in d.lines),
    ))
    key=artifact.filename;before=d.status;d.export_key=key;d.status=artifact.posting_status
    audit(db,tenant.id,who,"VOUCHER_EXPORTED_PENDING_MANUAL_POST",d.id,[r for l in d.lines for r in l.evidence_refs],
          before={"status":before,"version":d.version},after={"status":d.status,"export_key":key,"format_id":artifact.format_id});db.commit()
    return StreamingResponse(iter([artifact.content]),media_type=artifact.media_type,
        headers={"Content-Disposition":f'attachment; filename="{key}"',
                 "X-Posting-Status":artifact.posting_status,"X-Export-Format":artifact.format_id})

@router.get("/business-events")
def list_events(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                search: str = Query(default="", max_length=100), event_type: str = "", status: str = "",
                archive_only: bool | None = None,
                db:Session=Depends(db_session),principal:Principal=Depends(current_principal)):
    require_document_read(principal)
    tenant,account=active_scope(db,principal)
    event_query=apply_event_read_scope(select(BusinessEvent).where(BusinessEvent.tenant_id==tenant.id,
        BusinessEvent.account_set_id==account.id),principal)
    has_voucher = select(VoucherDraft.id).where(
        VoucherDraft.event_id == BusinessEvent.id, VoucherDraft.status != "REJECTED").exists()
    if archive_only is not None:
        event_query = event_query.where(has_voucher if archive_only else ~has_voucher)
    if event_type:
        event_query = event_query.where(BusinessEvent.event_type == event_type)
    if status:
        event_query = event_query.where(BusinessEvent.status == status)
    if search.strip():
        pattern = f"%{search.strip()}%"
        filename_matches = (select(BusinessEventDocument.event_id)
            .join(DocumentAsset, DocumentAsset.id == BusinessEventDocument.document_id)
            .where(DocumentAsset.original_filename.ilike(pattern)))
        seller_matches = (select(BusinessEventDocument.event_id)
            .join(DocumentExtraction, (DocumentExtraction.document_id == BusinessEventDocument.document_id)
                & (DocumentExtraction.version == BusinessEventDocument.extraction_version))
            .where(DocumentExtraction.fields["seller"]["normalized_value"].as_string().ilike(pattern)))
        event_query = event_query.where(or_(BusinessEvent.id.ilike(pattern),
            BusinessEvent.party_ref.ilike(pattern), BusinessEvent.id.in_(filename_matches),
            BusinessEvent.id.in_(seller_matches)))
    total=db.scalar(select(func.count()).select_from(event_query.subquery())) or 0
    event_query=event_query.order_by(BusinessEvent.created_at.desc()).offset((page-1)*page_size).limit(page_size)
    events=db.scalars(event_query).all()
    drafts=[] if principal.role=="BUSINESS_USER" and not principal.demo else db.scalars(
        select(VoucherDraft).where(VoucherDraft.tenant_id==tenant.id,
        VoucherDraft.account_set_id==account.id, VoucherDraft.event_id.in_([event.id for event in events]))
        .order_by(VoucherDraft.created_at.desc())).all()
    latest_draft_by_event={}
    for draft in drafts:
        latest_draft_by_event.setdefault(draft.event_id,draft)
    summaries=[]
    for e in events:
        party_name=e.party_ref
        if not party_name:
            invoice_link=next((link for link in e.documents if link.role=="INVOICE"),None)
            if invoice_link:
                extraction=db.scalar(select(DocumentExtraction).where(
                    DocumentExtraction.document_id==invoice_link.document_id,
                    DocumentExtraction.version==invoice_link.extraction_version))
                party_name=(extraction.fields.get("seller",{}).get("normalized_value") if extraction else None)
        latest_draft=latest_draft_by_event.get(e.id)
        if principal.role=="BUSINESS_USER" and not principal.demo:
            voucher_status=None
            posting_status="财务角色可见"
        else:
            voucher_status=latest_draft.status if latest_draft else None
            posting_status=("VERIFIED" if latest_draft.status=="VERIFIED" else
                "待人工入账" if latest_draft.status=="EXPORTED_PENDING_MANUAL_POST" else "未写入 ERP") if latest_draft else "尚无凭证"
        summaries.append({"id":e.id,"event_type":e.event_type,"party_name":party_name,
        "status":e.status,"amount":str(e.amount) if e.amount is not None else None,
        "created_at":e.created_at,"document_count":len(e.documents),
        "document_ids":[link.document_id for link in e.documents],
        "voucher_status":voucher_status,"posting_status":posting_status})
    return {"items": summaries, "page": page, "page_size": page_size, "total": total}


class BulkDeleteEventsBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/business-events/bulk-delete")
def bulk_delete_events(body: BulkDeleteEventsBody, db: Session = Depends(db_session),
                       principal: Principal = Depends(current_principal)):
    require(principal, "voucher_create")
    tenant, account = active_scope(db, principal)
    ids = list(dict.fromkeys(body.ids))
    events = db.scalars(select(BusinessEvent).where(
        BusinessEvent.id.in_(ids), BusinessEvent.tenant_id == tenant.id,
        BusinessEvent.account_set_id == account.id,
    ).with_for_update()).all()
    if len(events) != len(ids):
        raise HTTPException(404, "部分业务不存在或不属于当前账套")
    drafts = db.scalars(select(VoucherDraft).where(
        VoucherDraft.event_id.in_(ids), VoucherDraft.account_set_id == account.id,
    )).all()
    if any(draft.status in {"VERIFIED", "SUBMITTING", "POSTED_PENDING_VERIFY", "UNKNOWN"} for draft in drafts):
        raise HTTPException(409, "所选业务已有入账或结果待核实凭证，不能删除")
    draft_ids = [draft.id for draft in drafts]
    if draft_ids:
        db.execute(delete(PostingAttempt).where(PostingAttempt.voucher_draft_id.in_(draft_ids)))
        db.execute(delete(VoucherApproval).where(VoucherApproval.draft_id.in_(draft_ids)))
    for draft in drafts:
        db.delete(draft)
    db.flush()
    db.execute(delete(ExceptionCase).where(ExceptionCase.event_id.in_(ids)))
    db.execute(delete(BusinessEventAssignment).where(BusinessEventAssignment.event_id.in_(ids)))
    source_ids = list(dict.fromkeys(link.document_id for event in events for link in event.documents))
    for event in events:
        db.delete(event)
    audit(db, tenant.id, principal.actor, "BUSINESS_EVENTS_DELETED", account.id, source_ids,
          before={"event_ids": ids, "draft_ids": draft_ids}, after={"deleted_count": len(ids)})
    db.commit()
    return {"deleted_count": len(ids)}


@router.get("/insights/summary")
def insights_summary(db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require_document_read(principal)
    tenant, account = active_scope(db, principal)
    query = apply_event_read_scope(select(BusinessEvent).where(
        BusinessEvent.tenant_id == tenant.id,
        BusinessEvent.account_set_id == account.id,
    ), principal)
    visible_events = query.with_only_columns(BusinessEvent.id).subquery()
    event_count = db.scalar(select(func.count()).select_from(visible_events)) or 0
    matched_count, matched_amount = db.execute(select(
        func.count(BusinessEvent.id), func.coalesce(func.sum(BusinessEvent.amount), Decimal("0.00")),
    ).join(visible_events, visible_events.c.id == BusinessEvent.id).where(
        BusinessEvent.status == "MATCHED", BusinessEvent.amount.is_not(None),
    )).one()
    needs_review_count = db.scalar(select(func.count(BusinessEvent.id)).join(
        visible_events, visible_events.c.id == BusinessEvent.id,
    ).where(BusinessEvent.status == "NEEDS_REVIEW")) or 0
    open_case_count = db.scalar(select(func.count(ExceptionCase.id)).join(
        visible_events, visible_events.c.id == ExceptionCase.event_id,
    ).where(
        ExceptionCase.tenant_id == tenant.id,
        ExceptionCase.status == "OPEN",
    )) or 0

    latest_drafts = []
    if event_count and principal.role != "BUSINESS_USER":
        latest_versions = select(
            VoucherDraft.event_id.label("event_id"),
            func.max(VoucherDraft.version).label("version"),
        ).where(
            VoucherDraft.tenant_id == tenant.id,
            VoucherDraft.account_set_id == account.id,
            VoucherDraft.event_id.in_(select(visible_events.c.id)),
        ).group_by(VoucherDraft.event_id).subquery()
        latest_drafts = db.scalars(select(VoucherDraft).join(latest_versions,
            (latest_versions.c.event_id == VoucherDraft.event_id)
            & (latest_versions.c.version == VoucherDraft.version))).all()
    source_events = db.scalars(query.order_by(BusinessEvent.created_at.desc()).limit(5)).all()

    return {
        "scope": {"tenant": tenant.name, "account_set": account.name},
        "period_scope": "全部时间 · 当前权限范围内的全部业务记录",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "event_count": event_count,
        "matched_event_count": matched_count,
        "matched_event_amount": str(matched_amount),
        "needs_review_count": needs_review_count,
        "open_case_count": open_case_count,
        "voucher_metrics_visible": principal.role != "BUSINESS_USER",
        "pending_manual_post_count": sum(
            draft.status == "EXPORTED_PENDING_MANUAL_POST" for draft in latest_drafts),
        "verified_voucher_count": sum(draft.status == "VERIFIED" for draft in latest_drafts),
        "source_events": [{"id": event.id, "status": event.status,
            "amount": str(event.amount) if event.amount is not None else None,
            "created_at": event.created_at.isoformat()} for event in source_events],
        "limitations": "金额只代表已核对业务候选金额，不等同收入、现金余额或正式入账金额。当前没有授权 ERP 总账数据，不能计算利润、应收应付或银行余额。",
    }

@router.get("/business-events/{event_id}")
def get_event(event_id:str,db:Session=Depends(db_session),principal:Principal=Depends(current_principal)):
    require_document_read(principal)
    tenant,account=active_scope(db,principal)
    event_query=apply_event_read_scope(select(BusinessEvent).where(BusinessEvent.id==event_id,
        BusinessEvent.tenant_id==tenant.id,BusinessEvent.account_set_id==account.id),principal)
    event=db.scalar(event_query)
    if not event:raise HTTPException(404,"业务组不存在")
    docs=db.execute(select(BusinessEventDocument,DocumentAsset).join(DocumentAsset,DocumentAsset.id==BusinessEventDocument.document_id).where(BusinessEventDocument.event_id==event.id)).all()
    return {"id":event.id,"event_type":event.event_type,"status":event.status,"amount":str(event.amount) if event.amount is not None else None,"findings":event.findings,
            "rental_direct_expense_confirmed":event.rental_direct_expense_confirmed,
            "rental_direct_expense_confirmed_by":event.rental_direct_expense_confirmed_by,
            "rental_direct_expense_policy_version":event.rental_direct_expense_policy_version,
            "documents":[{"id":d.id,"filename":d.original_filename,"role":link.role,"extraction_version":link.extraction_version} for link,d in docs]}

@router.get("/exception-cases")
def list_cases(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
               db:Session=Depends(db_session),principal:Principal=Depends(current_principal)):
    require_document_read(principal)
    tenant,account=active_scope(db,principal);query=(select(ExceptionCase).join(BusinessEvent,ExceptionCase.event_id==BusinessEvent.id)
        .where(ExceptionCase.tenant_id==tenant.id,BusinessEvent.tenant_id==tenant.id,BusinessEvent.account_set_id==account.id))
    query=apply_event_read_scope(query,principal)
    total=db.scalar(select(func.count()).select_from(query.subquery())) or 0
    open_total=db.scalar(select(func.count()).select_from(query.where(ExceptionCase.status=="OPEN").subquery())) or 0
    rows=db.scalars(query.order_by(ExceptionCase.opened_at.desc()).offset((page-1)*page_size).limit(page_size)).all()
    return {"items": [{"id":c.id,"event_id":c.event_id,"type":c.case_type,"status":c.status,"severity":c.severity,"details":c.details,"source_refs":c.source_refs} for c in rows],
            "page": page, "page_size": page_size, "total": total, "open_total": open_total}

@router.get("/exception-cases/{case_id}")
def get_case_detail(case_id: str, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require_document_read(principal)
    tenant, account = active_scope(db, principal)
    query = (select(ExceptionCase).join(BusinessEvent, ExceptionCase.event_id == BusinessEvent.id)
        .where(ExceptionCase.id == case_id, ExceptionCase.tenant_id == tenant.id,
               BusinessEvent.tenant_id == tenant.id, BusinessEvent.account_set_id == account.id))
    case = db.scalar(apply_event_read_scope(query, principal))
    if case is None:
        raise HTTPException(404, "异常记录不存在")
    source_refs = list(dict.fromkeys(case.source_refs))
    event = db.scalar(select(BusinessEvent).where(BusinessEvent.id == case.event_id,
        BusinessEvent.tenant_id == tenant.id, BusinessEvent.account_set_id == account.id))
    scoped_documents = apply_document_read_scope(select(DocumentAsset).where(
        DocumentAsset.id.in_(source_refs), DocumentAsset.tenant_id == tenant.id,
        DocumentAsset.account_set_id == account.id), principal)
    visible_documents = db.scalars(scoped_documents).all()
    source_roles = db.execute(select(BusinessEventDocument.document_id, BusinessEventDocument.role,
        BusinessEventDocument.extraction_version).where(
            BusinessEventDocument.document_id.in_([document.id for document in visible_documents]),
            BusinessEventDocument.event_id == event.id)).all()
    role_by_document = {document_id: (role, version) for document_id, role, version in source_roles}
    source_documents = {document.id: {"id": document.id, "filename": document.original_filename,
        "role": role_by_document.get(document.id, ("RELATED_SOURCE", None))[0],
        "extraction_version": role_by_document.get(document.id, (None, None))[1],
        "mime_type": document.mime_type} for document in visible_documents}
    assignments = db.execute(select(AppUser.id, AppUser.display_name, AppUser.email)
        .join(BusinessEventAssignment, BusinessEventAssignment.user_id == AppUser.id)
        .where(BusinessEventAssignment.event_id == event.id,
               BusinessEventAssignment.tenant_id == tenant.id,
               BusinessEventAssignment.account_set_id == account.id)).all()
    audit_rows = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id,
        AuditEvent.entity_ref == event.id).order_by(AuditEvent.occurred_at)).all()
    return {"id": case.id, "event_id": event.id, "type": case.case_type, "status": case.status,
        "severity": case.severity, "details": case.details, "findings": case.details.get("findings", []),
        "source_documents": [source_documents[ref] for ref in source_refs if ref in source_documents],
        "unavailable_source_count": len(set(source_refs) - set(source_documents)),
        "assignees": [{"user_id": row.id, "display_name": row.display_name, "email": row.email}
                      for row in assignments],
        "event_status": event.status,
        "audit_trail": [{"actor": row.actor, "event_type": row.event_type, "occurred_at": row.occurred_at}
                        for row in audit_rows]}

@router.get("/voucher-drafts/{draft_id}")
def get_voucher(draft_id:str,db:Session=Depends(db_session),principal:Principal=Depends(current_principal)):
    require(principal,"read")
    tenant,account=active_scope(db,principal);d=db.scalar(select(VoucherDraft).where(VoucherDraft.id==draft_id,VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id))
    if not d:raise HTTPException(404,"凭证不存在")
    return voucher_json(d)

@router.delete("/voucher-drafts/{draft_id}")
def delete_voucher(draft_id: str, db: Session = Depends(db_session), who: str = Depends(actor),
                   principal: Principal = Depends(current_principal)):
    require(principal, "voucher_create")
    tenant, account = active_scope(db, principal)
    selected = db.scalar(select(VoucherDraft).where(VoucherDraft.id == draft_id,
        VoucherDraft.tenant_id == tenant.id, VoucherDraft.account_set_id == account.id))
    if selected is None:
        raise HTTPException(404, "凭证不存在")
    versions = db.scalars(select(VoucherDraft).where(VoucherDraft.event_id == selected.event_id,
        VoucherDraft.tenant_id == tenant.id, VoucherDraft.account_set_id == account.id)).all()
    version_ids = [item.id for item in versions]
    db.execute(delete(PostingAttempt).where(PostingAttempt.voucher_draft_id.in_(version_ids)))
    db.execute(delete(VoucherApproval).where(VoucherApproval.draft_id.in_(version_ids)))
    for item in versions:
        db.delete(item)
    audit(db, tenant.id, who, "VOUCHER_VERSIONS_DELETED", selected.event_id,
        [link.document_id for link in db.get(BusinessEvent, selected.event_id).documents],
        before={"draft_ids": version_ids}, after={"deleted_count": len(version_ids)})
    db.commit()
    return {"event_id": selected.event_id, "deleted_count": len(version_ids)}

@router.get("/voucher-drafts")
def list_vouchers(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                  event_id: str = "",
                  db:Session=Depends(db_session),principal:Principal=Depends(current_principal)):
    require(principal,"read")
    tenant,account=active_scope(db,principal)
    query=select(VoucherDraft).where(VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id)
    if event_id:
        query = query.where(VoucherDraft.event_id == event_id)
    total=db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows=db.scalars(query.order_by(VoucherDraft.created_at.desc()).offset((page-1)*page_size).limit(page_size)).all()
    latest_versions=(select(VoucherDraft.event_id,func.max(VoucherDraft.version).label("version"))
        .where(VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id)
        .group_by(VoucherDraft.event_id).subquery())
    counts=db.execute(select(VoucherDraft.accounting_period,VoucherDraft.status,func.count())
        .join(latest_versions,(VoucherDraft.event_id==latest_versions.c.event_id)
            &(VoucherDraft.version==latest_versions.c.version))
        .where(VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id)
        .group_by(VoucherDraft.accounting_period,VoucherDraft.status)).all()
    status_counts=[{"period":period,"status":status,"count":count} for period,status,count in counts]
    return {"items": [voucher_json(d) for d in rows], "page": page, "page_size": page_size,
            "total": total, "status_counts": status_counts}

@router.get("/voucher-drafts/{draft_id}/audit")
def get_voucher_audit(draft_id:str,db:Session=Depends(db_session),principal:Principal=Depends(current_principal)):
    require(principal,"read")
    tenant,account=active_scope(db,principal);d=db.scalar(select(VoucherDraft).where(VoucherDraft.id==draft_id,VoucherDraft.tenant_id==tenant.id,VoucherDraft.account_set_id==account.id))
    if not d:raise HTTPException(404,"凭证不存在")
    return [{"id":a.id,"actor":a.actor,"event_type":a.event_type,"entity_ref":a.entity_ref,"source_refs":a.source_refs,
             "before_hash":a.before_hash,"after_hash":a.after_hash,"occurred_at":a.occurred_at}
            for a in db.scalars(select(AuditEvent).where(AuditEvent.tenant_id==tenant.id,AuditEvent.entity_ref==draft_id).order_by(AuditEvent.occurred_at)).all()]
