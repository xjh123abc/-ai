"""Administrative user recovery operations that require trusted server access."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppUser, AuditEvent, AuthSession, TenantMembership
from app.security import hash_password


def reset_user_password(db: Session, email: str, new_password: str, actor: str) -> int:
    normalized_email = email.strip().lower()
    user = db.scalar(select(AppUser).where(AppUser.email == normalized_email))
    if user is None:
        raise ValueError("找不到该邮箱对应的用户")
    if not user.is_active:
        raise ValueError("该用户已停用，请先由管理员检查账号状态")

    user.password_hash = hash_password(new_password)
    now = datetime.now(timezone.utc)
    sessions = db.scalars(select(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None),
    )).all()
    for session in sessions:
        session.revoked_at = now

    tenant_ids = set(db.scalars(select(TenantMembership.tenant_id).where(
        TenantMembership.user_id == user.id,
    )).all())
    for tenant_id in tenant_ids:
        db.add(AuditEvent(
            tenant_id=tenant_id,
            actor=actor,
            event_type="USER_PASSWORD_RESET",
            entity_ref=user.id,
            source_refs=[],
        ))
    db.flush()
    return len(sessions)
