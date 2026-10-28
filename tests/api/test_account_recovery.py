from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.admin_users import reset_user_password
from app.db.base import Base
from app.db.models import AccountSet, AppUser, AuditEvent, AuthSession, Tenant, TenantMembership
from app.security import hash_password, verify_password


def test_admin_password_reset_revokes_sessions_and_audits_each_tenant():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = AppUser(email="reset@example.test", display_name="Reset user", password_hash=hash_password("Initial-password-2026!"))
        tenant_a, tenant_b = Tenant(name="A"), Tenant(name="B")
        db.add_all([user, tenant_a, tenant_b])
        db.flush()
        account_a = AccountSet(tenant_id=tenant_a.id, name="A", currency="CNY")
        account_b = AccountSet(tenant_id=tenant_b.id, name="B", currency="CNY")
        db.add_all([account_a, account_b])
        db.flush()
        membership_a = TenantMembership(user_id=user.id, tenant_id=tenant_a.id, account_set_id=account_a.id, role="ACCOUNTANT")
        membership_b = TenantMembership(user_id=user.id, tenant_id=tenant_b.id, account_set_id=account_b.id, role="APPROVER")
        db.add_all([membership_a, membership_b])
        db.flush()
        db.add_all([
            AuthSession(user_id=user.id, membership_id=membership_a.id, token_digest="a" * 64,
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1)),
            AuthSession(user_id=user.id, membership_id=membership_b.id, token_digest="b" * 64,
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1)),
        ])
        db.commit()

        revoked_count = reset_user_password(db, user.email, "Recovered-password-2026!", actor="admin-cli:test-operator")

        assert revoked_count == 2
        assert verify_password("Recovered-password-2026!", db.get(AppUser, user.id).password_hash)
        assert not verify_password("Initial-password-2026!", db.get(AppUser, user.id).password_hash)
        assert db.scalar(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))) is None
        audit_rows = db.scalars(select(AuditEvent).where(AuditEvent.entity_ref == user.id)).all()
        assert {row.tenant_id for row in audit_rows} == {tenant_a.id, tenant_b.id}
        assert all(row.event_type == "USER_PASSWORD_RESET" and row.actor == "admin-cli:test-operator" for row in audit_rows)
