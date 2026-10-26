from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AccountSet, AppUser, AuthSession, Tenant, TenantMembership
from app.db.session import db_session

ROLE_PERMISSIONS = {
    "TENANT_ADMIN": {"read", "upload", "correct", "reconcile", "voucher_create", "voucher_submit", "approve", "export", "manage_members"},
    "ACCOUNTANT": {"read", "upload", "correct", "reconcile", "voucher_create", "voucher_submit", "export"},
    "APPROVER": {"read", "reconcile", "approve", "export"},
    "BUSINESS_USER": {"read_assigned", "upload", "correct"},
    "AUDITOR": {"read", "export"},
    "OWNER": {"read", "approve", "export"},
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    account_set_id: str
    role: str
    actor: str
    demo: bool = False


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def current_principal(
    db: Session = Depends(db_session),
    authorization: str | None = Header(default=None),
    x_actor: str | None = Header(default=None),
) -> Principal:
    if not authorization and settings.demo_mode:
        tenant = db.get(Tenant, settings.demo_tenant_id)
        account = db.get(AccountSet, settings.demo_account_set_id)
        if tenant is None or account is None or not tenant.is_demo or account.tenant_id != tenant.id:
            raise HTTPException(503, "本地演示账套未初始化")
        return Principal("demo-user", tenant.id, account.id, "DEMO", (x_actor or "local-user")[:200], True)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.removeprefix("Bearer ").strip()
    if len(token) < 40:
        raise HTTPException(401, "登录凭据无效")
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = db.execute(select(AuthSession, AppUser, TenantMembership).join(AppUser, AppUser.id == AuthSession.user_id)
        .join(TenantMembership, TenantMembership.id == AuthSession.membership_id)
        .where(AuthSession.token_digest == digest, AuthSession.revoked_at.is_(None),
               AuthSession.expires_at > datetime.now(timezone.utc), AppUser.is_active.is_(True))).first()
    if row is None:
        raise HTTPException(401, "登录已失效，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    session, user, membership = row
    if membership.user_id != user.id:
        raise HTTPException(401, "登录范围无效")
    account = db.scalar(select(AccountSet).where(AccountSet.id == membership.account_set_id,
                                                  AccountSet.tenant_id == membership.tenant_id))
    tenant = db.get(Tenant, membership.tenant_id)
    if account is None or tenant is None or tenant.is_demo:
        raise HTTPException(403, "用户账套授权已失效")
    return Principal(user.id, membership.tenant_id, membership.account_set_id, membership.role, user.email)


def require(principal: Principal, permission: str) -> None:
    if principal.demo:
        return
    if permission not in ROLE_PERMISSIONS.get(principal.role, set()):
        raise HTTPException(403, "当前角色无权执行此操作")


def issue_session(db: Session, user: AppUser, membership: TenantMembership) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(hours=8)
    db.add(AuthSession(user_id=user.id, membership_id=membership.id,
                      token_digest=hashlib.sha256(token.encode()).hexdigest(), expires_at=expires))
    return token, expires
