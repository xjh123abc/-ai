from datetime import datetime, timezone
import hmac
import json
from hashlib import sha256
from math import ceil

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    AccountSet,
    AccountingPolicy,
    AccountingPeriod,
    AppUser,
    AuditEvent,
    AuthSession,
    BusinessEvent,
    ChartAccount,
    LoginThrottleBucket,
    Tenant,
    TenantMembership,
    VoucherDraft,
    VoucherDraftLine,
)
from app.db.session import db_session
from app.security import current_principal, hash_password, issue_session, Principal, require, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=1, max_length=256)
    membership_id: str | None = None


LOGIN_WINDOW = 15 * 60
LOGIN_FAILURE_LIMITS = {"pair": 5, "email": 20, "ip": 30}


def login_failure_buckets(email: str, client_ip: str) -> dict[str, str]:
    normalized_email = email.strip().lower()
    key = settings.database_url.encode()

    def digest(value: str) -> str:
        return hmac.new(key, value.encode(), sha256).hexdigest()

    email_digest = digest(normalized_email)
    ip_digest = digest(client_ip)
    pair_digest = digest(f"{normalized_email}\0{client_ip}")
    return {"pair": pair_digest, "email": email_digest, "ip": ip_digest}


def consume_login_attempts(
    db: Session, buckets: dict[str, str], now: datetime
) -> dict[str, tuple[int, datetime]]:
    window_start = datetime.fromtimestamp(now.timestamp() - LOGIN_WINDOW, timezone.utc)
    db.execute(delete(LoginThrottleBucket).where(
        LoginThrottleBucket.window_started_at
        < datetime.fromtimestamp(now.timestamp() - 86400, timezone.utc)
    ))
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert_bucket = postgres_insert
    elif dialect == "sqlite":
        insert_bucket = sqlite_insert
    else:
        raise HTTPException(503, "当前数据库不支持登录限速")
    results: dict[str, tuple[int, datetime]] = {}
    for dimension, key in buckets.items():
        expired = LoginThrottleBucket.window_started_at <= window_start
        statement = insert_bucket(LoginThrottleBucket).values(
            bucket_key=key, window_started_at=now, attempts=1
        ).on_conflict_do_update(
            index_elements=[LoginThrottleBucket.bucket_key],
            set_={
                "window_started_at": case((expired, now), else_=LoginThrottleBucket.window_started_at),
                "attempts": case((expired, 1), else_=LoginThrottleBucket.attempts + 1),
            },
        ).returning(LoginThrottleBucket.attempts, LoginThrottleBucket.window_started_at)
        attempts, started_at = db.execute(statement).one()
        results[dimension] = (attempts, started_at)
    db.commit()
    return results


def enforce_login_limit(attempts: dict[str, tuple[int, datetime]], now: datetime) -> None:
    for dimension, limit in LOGIN_FAILURE_LIMITS.items():
        count, started_at = attempts[dimension]
        if count > limit:
            started_utc = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
            retry_after = max(1, ceil((started_utc - now).total_seconds() + LOGIN_WINDOW))
            raise HTTPException(
                429,
                "登录尝试次数过多，请稍后再试。",
                headers={"Retry-After": str(retry_after)},
            )


@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(db_session)):
    now = datetime.now(timezone.utc)
    client_ip = request.client.host if request.client else "unknown"
    buckets = login_failure_buckets(body.email, client_ip)
    enforce_login_limit(consume_login_attempts(db, buckets, now), now)
    user = db.scalar(select(AppUser).where(AppUser.email == body.email.strip().lower(), AppUser.is_active.is_(True)))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码错误")
    memberships = db.scalars(select(TenantMembership).where(TenantMembership.user_id == user.id)).all()
    if body.membership_id:
        memberships = [m for m in memberships if m.id == body.membership_id]
    if len(memberships) != 1:
        if not memberships:
            raise HTTPException(403, "此账号尚未分配企业账套")
        choices = [{"membership_id":m.id,"tenant_id":m.tenant_id,"account_set_id":m.account_set_id,"role":m.role} for m in memberships]
        raise HTTPException(409, {"code":"ACCOUNT_SET_SELECTION_REQUIRED","memberships":choices})
    membership = memberships[0]
    if db.get(Tenant, membership.tenant_id) is None or db.get(Tenant, membership.tenant_id).is_demo:
        raise HTTPException(403, "演示企业不能用于生产登录")
    db.execute(delete(LoginThrottleBucket).where(LoginThrottleBucket.bucket_key.in_(buckets.values())))
    token, expires = issue_session(db, user, membership)
    db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_at": expires,
            "tenant_id": membership.tenant_id, "account_set_id": membership.account_set_id,
            "role": membership.role, "display_name": user.display_name}


@router.get("/me")
def me(principal: Principal = Depends(current_principal), db: Session = Depends(db_session)):
    if principal.demo:
        scopes=[{"membership_id":"demo","tenant_id":principal.tenant_id,"account_set_id":principal.account_set_id,
                 "tenant_name":"本地验收演示企业","account_set_name":"验收演示账套","role":"DEMO"}]
    else:
        rows=db.execute(select(TenantMembership,Tenant,AccountSet).join(Tenant,Tenant.id==TenantMembership.tenant_id)
            .join(AccountSet,AccountSet.id==TenantMembership.account_set_id)
            .where(TenantMembership.user_id==principal.user_id,Tenant.is_demo.is_(False))).all()
        scopes=[{"membership_id":membership.id,"tenant_id":tenant.id,"account_set_id":account.id,
                 "tenant_name":tenant.name,"account_set_name":account.name,"role":membership.role}
                for membership,tenant,account in rows]
    return {"user_id": principal.user_id, "tenant_id": principal.tenant_id,
            "account_set_id": principal.account_set_id, "role": principal.role,
            "demo": principal.demo,"scopes":scopes}


class ScopeSwitchBody(BaseModel):
    membership_id: str = Field(min_length=1, max_length=36)


@router.post("/switch-scope")
def switch_scope(body: ScopeSwitchBody, authorization: str | None = Header(default=None),
                 db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    if principal.demo:
        raise HTTPException(409,"本地演示身份不能切换企业账套")
    membership=db.scalar(select(TenantMembership).where(TenantMembership.id==body.membership_id,
        TenantMembership.user_id==principal.user_id))
    if membership is None:
        raise HTTPException(403,"所选账套不属于当前用户的授权范围")
    tenant=db.get(Tenant,membership.tenant_id)
    account=db.scalar(select(AccountSet).where(AccountSet.id==membership.account_set_id,
        AccountSet.tenant_id==membership.tenant_id))
    if tenant is None or tenant.is_demo or account is None:
        raise HTTPException(403,"所选企业或账套不可用")
    current_token=authorization.removeprefix("Bearer ").strip() if authorization else ""
    current_digest=__import__("hashlib").sha256(current_token.encode()).hexdigest()
    current_session=db.scalar(select(AuthSession).where(AuthSession.token_digest==current_digest,
        AuthSession.revoked_at.is_(None)))
    if current_session is None:
        raise HTTPException(401,"登录会话已失效，请重新登录")
    current_session.revoked_at=datetime.now(timezone.utc)
    token,expires=issue_session(db,db.get(AppUser,principal.user_id),membership)
    db.add(AuditEvent(tenant_id=tenant.id,actor=principal.actor,event_type="ACCOUNT_SCOPE_SWITCHED",
        entity_ref=account.id,source_refs=[]))
    db.commit()
    return {"access_token":token,"token_type":"bearer","expires_at":expires,
        "tenant_id":tenant.id,"account_set_id":account.id,"role":membership.role}


@router.get("/accounting-periods")
def list_accounting_periods(db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal,"read")
    rows=db.scalars(select(AccountingPeriod).where(AccountingPeriod.account_set_id==principal.account_set_id)
        .order_by(AccountingPeriod.period.desc())).all()
    return [{"period":row.period,"status":row.status} for row in rows]


@router.post("/logout")
def logout(authorization: str | None = Header(default=None), db: Session = Depends(db_session),
          principal: Principal = Depends(current_principal)):
    if principal.demo:
        return {"status": "DEMO_SESSION_NOT_PERSISTED"}
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    digest = __import__("hashlib").sha256(token.encode()).hexdigest()
    session = db.scalar(select(AuthSession).where(AuthSession.token_digest == digest, AuthSession.revoked_at.is_(None)))
    if session:
        session.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return {"status": "REVOKED"}


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    display_name: str = Field(min_length=1, max_length=200)
    initial_password: str = Field(min_length=12, max_length=256)
    role: str = Field(pattern="^(TENANT_ADMIN|ACCOUNTANT|APPROVER|BUSINESS_USER|AUDITOR|OWNER)$")


@router.get("/memberships")
def list_memberships(role: str | None = Query(default=None), db: Session = Depends(db_session),
                    principal: Principal = Depends(current_principal)):
    require(principal,"manage_members")
    query=(select(TenantMembership,AppUser).join(AppUser,AppUser.id==TenantMembership.user_id)
        .where(TenantMembership.tenant_id==principal.tenant_id,
               TenantMembership.account_set_id==principal.account_set_id))
    if role:
        query=query.where(TenantMembership.role==role)
    rows=db.execute(query.order_by(AppUser.email)).all()
    return [{"membership_id":membership.id,"user_id":user.id,"email":user.email,
             "display_name":user.display_name,"role":membership.role}
            for membership,user in rows]


@router.post("/memberships", status_code=201)
def add_member(body: InviteBody, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "manage_members")
    email = body.email.strip().lower()
    account = db.scalar(select(AccountSet).where(AccountSet.id == principal.account_set_id,
                                                  AccountSet.tenant_id == principal.tenant_id))
    tenant = db.get(Tenant, principal.tenant_id)
    if account is None or tenant is None:
        raise HTTPException(403, "企业或账套授权已失效")
    user = db.scalar(select(AppUser).where(AppUser.email == email))
    if user is None:
        user = AppUser(email=email, display_name=body.display_name,
                       password_hash=hash_password(body.initial_password), is_active=True)
        db.add(user)
        db.flush()
    elif not user.is_active:
        raise HTTPException(409, "该用户已停用，请管理员先处理账号状态")
    membership = db.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id,
                                                           TenantMembership.account_set_id == account.id))
    if membership is not None:
        raise HTTPException(409, "该用户已属于此账套")
    membership = TenantMembership(user_id=user.id, tenant_id=tenant.id,
                                  account_set_id=account.id, role=body.role)
    db.add(membership)
    db.flush()
    db.add(AuditEvent(tenant_id=tenant.id, actor=principal.actor, event_type="MEMBERSHIP_CREATED",
                      entity_ref=membership.id, source_refs=[]))
    db.commit()
    return {"membership_id": membership.id, "user_id": user.id, "email": user.email,
            "tenant_id": tenant.id, "account_set_id": account.id, "role": membership.role}


class AccountSetBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="CNY", pattern="^[A-Z]{3}$")


@router.post("/account-sets", status_code=201)
def create_account_set(body: AccountSetBody, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "manage_members")
    tenant = db.get(Tenant, principal.tenant_id)
    user = db.get(AppUser, principal.user_id)
    if tenant is None or tenant.is_demo or user is None:
        raise HTTPException(403, "仅生产企业管理员可以创建账套")
    account = AccountSet(tenant_id=tenant.id, name=body.name, currency=body.currency)
    db.add(account)
    db.flush()
    membership = TenantMembership(user_id=user.id, tenant_id=tenant.id,
                                  account_set_id=account.id, role="TENANT_ADMIN")
    db.add(membership)
    db.add(AuditEvent(tenant_id=tenant.id, actor=principal.actor,
                      event_type="ACCOUNT_SET_CREATED", entity_ref=account.id, source_refs=[]))
    db.commit()
    return {"account_set_id": account.id, "tenant_id": tenant.id, "name": account.name,
            "currency": account.currency, "membership_id": membership.id}


class PeriodBody(BaseModel):
    status: str = Field(pattern="^(OPEN|CLOSED)$")


@router.put("/accounting-periods/{period}")
def set_period(period: str, body: PeriodBody, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "manage_members")
    import re
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise HTTPException(422, "期间必须使用 YYYY-MM 格式")
    row = db.scalar(select(AccountingPeriod).where(AccountingPeriod.account_set_id == principal.account_set_id,
                                                    AccountingPeriod.period == period))
    if row is None:
        row = AccountingPeriod(account_set_id=principal.account_set_id, period=period,
                               status=body.status, updated_by=principal.actor)
        db.add(row)
    else:
        row.status = body.status
        row.updated_by = principal.actor
    db.add(AuditEvent(tenant_id=principal.tenant_id, actor=principal.actor,
                      event_type="ACCOUNTING_PERIOD_" + body.status, entity_ref=principal.account_set_id, source_refs=[]))
    db.commit()
    return {"period": row.period, "status": row.status}


class ChartAccountInput(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    is_active: bool = True
    is_leaf: bool = True
    requires_auxiliary: bool = False
    required_auxiliary_fields: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("required_auxiliary_fields")
    @classmethod
    def normalize_auxiliary_fields(cls, fields: list[str]) -> list[str]:
        normalized = [field.strip() for field in fields]
        if any(not field for field in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("辅助核算维度名称不能为空且不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_auxiliary_requirements(self):
        if self.requires_auxiliary != bool(self.required_auxiliary_fields):
            raise ValueError("需要辅助核算的科目必须提供必填维度；不需要时维度列表必须为空")
        return self


class ChartAccountsBody(BaseModel):
    accounts: list[ChartAccountInput] = Field(min_length=1, max_length=2000)


class AccountingPolicyInput(BaseModel):
    allowed_account_codes: list[str] = Field(min_length=1, max_length=2000)
    tax_treatment: str = Field(pattern="^(DEDUCTIBLE|NON_DEDUCTIBLE)$")
    input_tax_account_codes: list[str] = Field(min_length=1, max_length=100)
    accounting_treatment: str = Field(pattern="^(EXPENSE|CAPITALIZE|DIRECT_EXPENSE)$")
    confirmed: bool

    @model_validator(mode="after")
    def validate_policy_confirmation(self):
        if not self.confirmed:
            raise ValueError("保存前必须确认这是财务负责人核定的会计处理政策")
        tax_codes = set(self.input_tax_account_codes)
        allowed_codes = set(self.allowed_account_codes)
        if self.tax_treatment == "DEDUCTIBLE" and not tax_codes.issubset(allowed_codes):
            raise ValueError("进项税科目必须在允许科目范围内")
        if self.tax_treatment == "NON_DEDUCTIBLE" and tax_codes.intersection(allowed_codes):
            raise ValueError("不可抵扣政策不能允许进项税科目入账")
        if len(self.allowed_account_codes) != len(set(self.allowed_account_codes)):
            raise ValueError("允许科目不能重复")
        if len(self.input_tax_account_codes) != len(tax_codes):
            raise ValueError("进项税科目不能重复")
        return self


@router.get("/chart-accounts")
def list_chart_accounts(db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "read")
    rows = db.scalars(select(ChartAccount).where(
        ChartAccount.account_set_id == principal.account_set_id,
    ).order_by(ChartAccount.code)).all()
    return {"account_set_id": principal.account_set_id, "accounts": [
        {"code": row.code, "name": row.name, "is_active": row.is_active, "is_leaf": row.is_leaf,
         "requires_auxiliary": row.requires_auxiliary,
         "required_auxiliary_fields": row.required_auxiliary_fields or []}
        for row in rows
    ]}


@router.get("/accounting-policies")
def list_accounting_policies(db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "read")
    rows = db.scalars(select(AccountingPolicy).where(
        AccountingPolicy.account_set_id == principal.account_set_id,
    ).order_by(AccountingPolicy.business_type, AccountingPolicy.version.desc())).all()
    latest = {}
    for row in rows:
        latest.setdefault(row.business_type, row)
    return {"policies": [{"business_type": row.business_type, "version": row.version,
        "configuration": row.configuration, "confirmed_by": row.confirmed_by,
        "created_at": row.created_at} for row in latest.values()]}


@router.put("/accounting-policies/{business_type}")
def save_accounting_policy(business_type: str, body: AccountingPolicyInput,
                           db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "manage_accounting_policy")
    if business_type not in {"EXPENSE", "ASSET_PURCHASE", "RENTAL"}:
        raise HTTPException(422, "此业务类型暂不支持单独配置会计政策")
    expected_treatment = {"EXPENSE": "EXPENSE", "ASSET_PURCHASE": "CAPITALIZE", "RENTAL": "DIRECT_EXPENSE"}[business_type]
    if body.accounting_treatment != expected_treatment:
        raise HTTPException(422, f"{business_type} 必须由财务负责人确认处理方式 {expected_treatment}")
    account_codes = [*body.allowed_account_codes, *body.input_tax_account_codes]
    accounts = db.scalars(select(ChartAccount).where(
        ChartAccount.account_set_id == principal.account_set_id,
        ChartAccount.code.in_(account_codes),
    )).all()
    by_code = {row.code: row for row in accounts}
    for code in account_codes:
        account = by_code.get(code)
        if account is None or not account.is_active or not account.is_leaf:
            raise HTTPException(422, f"科目 {code} 不存在、未启用或不是末级科目")
    previous = db.scalar(select(AccountingPolicy).where(
        AccountingPolicy.account_set_id == principal.account_set_id,
        AccountingPolicy.business_type == business_type,
    ).order_by(AccountingPolicy.version.desc()).limit(1).with_for_update())
    configuration = body.model_dump(exclude={"confirmed"})
    if previous and previous.configuration == configuration and previous.confirmed_by == principal.actor:
        return {"business_type": business_type, "version": previous.version,
            "configuration": previous.configuration, "unchanged": True}
    in_flight = db.scalars(select(VoucherDraft).join(
        BusinessEvent, BusinessEvent.id == VoucherDraft.event_id,
    ).where(
        VoucherDraft.account_set_id == principal.account_set_id,
        BusinessEvent.event_type == business_type,
        VoucherDraft.status.in_(("DRAFT", "NEEDS_REVIEW", "VALIDATED", "PENDING_APPROVAL", "APPROVED",
            "EXPORTED_PENDING_MANUAL_POST", "SUBMITTING", "POSTED_PENDING_VERIFY", "UNKNOWN", "FAILED_POSTING")),
    ).with_for_update()).all()
    removable_statuses = {"DRAFT", "NEEDS_REVIEW", "VALIDATED"}
    if any(draft.status not in removable_statuses for draft in in_flight):
        raise HTTPException(409, "当前账套有未完成凭证，请先处理后再变更会计政策")
    for draft in in_flight:
        deletion_record = {"draft_id": draft.id, "version": draft.version, "status": draft.status}
        db.add(AuditEvent(tenant_id=principal.tenant_id, actor=principal.actor,
            event_type="VOUCHER_DRAFT_DELETED_FOR_POLICY_CHANGE", entity_ref=draft.id,
            source_refs=[draft.event_id],
            before_hash=sha256(json.dumps(deletion_record, sort_keys=True).encode()).hexdigest()))
        db.delete(draft)
    version = (previous.version + 1) if previous else 1
    row = AccountingPolicy(account_set_id=principal.account_set_id, business_type=business_type,
        version=version, configuration=configuration, confirmed_by=principal.actor)
    db.add(row)
    before_hash = sha256(json.dumps(previous.configuration if previous else {}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    after_hash = sha256(json.dumps(configuration, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    db.add(AuditEvent(tenant_id=principal.tenant_id, actor=principal.actor,
        event_type="ACCOUNTING_POLICY_CONFIRMED", entity_ref=principal.account_set_id,
        source_refs=[], before_hash=before_hash, after_hash=after_hash))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "政策版本已被其他用户更新，请刷新后重试") from exc
    return {"business_type": business_type, "version": version, "configuration": configuration,
            "unchanged": False, "deleted_draft_count": len(in_flight)}


@router.put("/chart-accounts")
def import_chart_accounts(body: ChartAccountsBody, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "manage_members")
    codes=[a.code.strip() for a in body.accounts]
    if len(codes) != len(set(codes)):
        raise HTTPException(422, "同一批次存在重复科目编码")
    existing={a.code:a for a in db.scalars(select(ChartAccount).where(
        ChartAccount.account_set_id==principal.account_set_id,
    ).with_for_update()).all()}
    before_config = sorted((row.code, row.name, row.is_active, row.is_leaf,
                            row.requires_auxiliary, row.required_auxiliary_fields or []) for row in existing.values())
    for item in body.accounts:
        code=item.code.strip()
        row=existing.get(code)
        if row and (row.is_active != item.is_active or row.is_leaf != item.is_leaf
                    or row.requires_auxiliary != item.requires_auxiliary
                    or (row.required_auxiliary_fields or []) != item.required_auxiliary_fields):
            draft_id = db.scalar(select(VoucherDraft.id).join(
                VoucherDraftLine, VoucherDraftLine.draft_id == VoucherDraft.id,
            ).where(
                VoucherDraft.account_set_id == principal.account_set_id,
                VoucherDraftLine.account_code == code,
                VoucherDraft.status.in_(("DRAFT", "NEEDS_REVIEW", "VALIDATED", "PENDING_APPROVAL", "APPROVED",
                    "EXPORTED_PENDING_MANUAL_POST", "SUBMITTING", "POSTED_PENDING_VERIFY", "UNKNOWN", "FAILED_POSTING")),
            ).limit(1))
            if draft_id:
                raise HTTPException(409, f"科目 {code} 有未完成凭证，请先处理凭证再更改辅助核算政策")
        if row is None:
            row=ChartAccount(account_set_id=principal.account_set_id,code=code,name=item.name.strip())
            db.add(row)
            existing[code] = row
        row.name=item.name.strip();row.is_active=item.is_active;row.is_leaf=item.is_leaf;row.requires_auxiliary=item.requires_auxiliary
        row.required_auxiliary_fields=item.required_auxiliary_fields
    after_config = sorted((row.code, row.name, row.is_active, row.is_leaf,
                           row.requires_auxiliary, row.required_auxiliary_fields or []) for row in existing.values())
    db.add(AuditEvent(tenant_id=principal.tenant_id,actor=principal.actor,event_type="CHART_ACCOUNTS_IMPORTED",
                      entity_ref=principal.account_set_id,source_refs=[],
                      before_hash=sha256(json.dumps(before_config, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                      after_hash=sha256(json.dumps(after_config, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()))
    db.commit()
    return {"account_set_id":principal.account_set_id,"upserted":len(body.accounts)}


@router.delete("/chart-accounts/{code}")
def remove_chart_account(code: str, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "manage_members")
    row = db.scalar(select(ChartAccount).where(
        ChartAccount.account_set_id == principal.account_set_id, ChartAccount.code == code,
    ))
    if row is None:
        raise HTTPException(404, "科目不存在")
    if db.scalar(select(VoucherDraftLine.id).join(VoucherDraft).where(
        VoucherDraft.account_set_id == principal.account_set_id,
        VoucherDraftLine.account_code == code,
    ).limit(1)):
        raise HTTPException(409, "科目已被凭证使用，不能删除")
    policies = db.scalars(select(AccountingPolicy).where(
        AccountingPolicy.account_set_id == principal.account_set_id,
    )).all()
    if any(code in (policy.configuration.get("allowed_account_codes", []) +
                    policy.configuration.get("input_tax_account_codes", [])) for policy in policies):
        raise HTTPException(409, "科目已被会计政策使用，不能删除")
    db.delete(row)
    db.add(AuditEvent(tenant_id=principal.tenant_id, actor=principal.actor,
                      event_type="CHART_ACCOUNT_DELETED", entity_ref=principal.account_set_id,
                      source_refs=[], before_hash=sha256(f"{code}:{row.name}".encode()).hexdigest()))
    db.commit()
    return {"deleted": code}
