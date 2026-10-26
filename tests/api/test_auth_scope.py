from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.models import AccountSet, AppUser, AuditEvent, DocumentAsset, Tenant, TenantMembership
from app.db.session import SessionLocal
from app.main import app
from app.security import hash_password


def test_login_scopes_documents_and_revokes_session():
    tag = uuid4().hex[:12]
    password = "Temporary-test-password-2026"
    with SessionLocal() as db:
        tenant_a = Tenant(name="pytest tenant A", is_demo=False)
        tenant_b = Tenant(name="pytest tenant B", is_demo=False)
        db.add_all([tenant_a, tenant_b])
        db.flush()
        set_a = AccountSet(tenant_id=tenant_a.id, name="A", currency="CNY")
        set_b = AccountSet(tenant_id=tenant_b.id, name="B", currency="CNY")
        db.add_all([set_a, set_b])
        db.flush()
        user_a = AppUser(email=f"a-{tag}@example.test", display_name="A", password_hash=hash_password(password))
        user_b = AppUser(email=f"b-{tag}@example.test", display_name="B", password_hash=hash_password(password))
        db.add_all([user_a, user_b])
        db.flush()
        db.add_all([
            TenantMembership(user_id=user_a.id, tenant_id=tenant_a.id, account_set_id=set_a.id, role="ACCOUNTANT"),
            TenantMembership(user_id=user_b.id, tenant_id=tenant_b.id, account_set_id=set_b.id, role="BUSINESS_USER"),
        ])
        doc_a = DocumentAsset(tenant_id=tenant_a.id, account_set_id=set_a.id, sha256="a" * 64,
            original_filename="a.jpg", mime_type="image/jpeg", byte_size=10, storage_key="/tmp/a", uploaded_by=user_a.email)
        doc_b = DocumentAsset(tenant_id=tenant_b.id, account_set_id=set_b.id, sha256="b" * 64,
            original_filename="b.jpg", mime_type="image/jpeg", byte_size=10, storage_key="/tmp/b", uploaded_by=user_b.email)
        db.add_all([doc_a, doc_b])
        db.commit()
        tenant_ids, document_ids = (tenant_a.id, tenant_b.id), (doc_a.id, doc_b.id)
        email_a, email_b = user_a.email, user_b.email

    client = TestClient(app)
    login_a = client.post("/api/v1/auth/login", json={"email": email_a, "password": password})
    login_b = client.post("/api/v1/auth/login", json={"email": email_b, "password": password})
    assert login_a.status_code == login_b.status_code == 200
    auth_a = {"Authorization": f"Bearer {login_a.json()['access_token']}"}
    auth_b = {"Authorization": f"Bearer {login_b.json()['access_token']}"}
    try:
        assert [d["id"] for d in client.get("/api/v1/documents", headers=auth_a).json()] == [document_ids[0]]
        assert [d["id"] for d in client.get("/api/v1/documents", headers=auth_b).json()] == [document_ids[1]]
        assert client.get(f"/api/v1/documents/{document_ids[1]}", headers=auth_a).status_code == 404
        assert client.post("/api/v1/auth/memberships", headers=auth_b, json={
            "email": f"blocked-{uuid4().hex[:8]}@example.test", "display_name": "No admin",
            "initial_password": password, "role": "APPROVER",
        }).status_code == 403
        assert client.post("/api/v1/auth/logout", headers=auth_a).status_code == 200
        assert client.get("/api/v1/auth/me", headers=auth_a).status_code == 401
    finally:
        with SessionLocal.begin() as db:
            db.delete(db.get(Tenant, tenant_ids[0]))
            db.delete(db.get(Tenant, tenant_ids[1]))


def test_audit_events_reject_update_and_delete():
    db=SessionLocal()
    tenant=db.get(Tenant,settings.demo_tenant_id)
    if tenant is None:
        db.close()
        pytest.skip("requires migrated fixture tenant")
    db.rollback()
    outer=db.begin()
    row=AuditEvent(tenant_id=tenant.id,actor="pytest",event_type="IMMUTABILITY_PROBE",entity_ref="probe",source_refs=[])
    db.add(row);db.flush();event_id=row.id
    rejected=[]
    try:
        for mutate in (lambda: db.execute(update(AuditEvent).where(AuditEvent.id==event_id).values(event_type="CHANGED")),
                       lambda: db.execute(AuditEvent.__table__.delete().where(AuditEvent.id==event_id))):
            savepoint=db.begin_nested()
            try:
                mutate()
                rejected.append(False)
            except SQLAlchemyError:
                rejected.append(True)
                savepoint.rollback()
    finally:
        outer.rollback();db.close()
    assert rejected==[True,True]
