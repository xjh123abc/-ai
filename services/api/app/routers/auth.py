from datetime import datetime, timezone
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppUser, AuthSession, Tenant, AccountSet, TenantMembership, AuditEvent, AccountingPeriod, ChartAccount
from app.db.session import db_session
from app.security import current_principal, hash_password, issue_session, Principal, require, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=1, max_length=256)
    membership_id: str | None = None


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(db_session)):
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


class ChartAccountsBody(BaseModel):
    accounts: list[ChartAccountInput] = Field(min_length=1, max_length=2000)


@router.put("/chart-accounts")
def import_chart_accounts(body: ChartAccountsBody, db: Session = Depends(db_session), principal: Principal = Depends(current_principal)):
    require(principal, "manage_members")
    codes=[a.code.strip() for a in body.accounts]
    if len(codes) != len(set(codes)):
        raise HTTPException(422, "同一批次存在重复科目编码")
    existing={a.code:a for a in db.scalars(select(ChartAccount).where(ChartAccount.account_set_id==principal.account_set_id)).all()}
    for item in body.accounts:
        code=item.code.strip()
        row=existing.get(code)
        if row is None:
            row=ChartAccount(account_set_id=principal.account_set_id,code=code,name=item.name.strip())
            db.add(row)
        row.name=item.name.strip();row.is_active=item.is_active;row.is_leaf=item.is_leaf;row.requires_auxiliary=item.requires_auxiliary
    db.add(AuditEvent(tenant_id=principal.tenant_id,actor=principal.actor,event_type="CHART_ACCOUNTS_IMPORTED",
                      entity_ref=principal.account_set_id,source_refs=[]))
    db.commit()
    return {"account_set_id":principal.account_set_id,"upserted":len(body.accounts)}
