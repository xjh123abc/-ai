"""Explicitly seed the non-production fixture tenant and chart of accounts."""
from sqlalchemy import select

from app.db.models import AccountSet, AccountingPeriod, ChartAccount, Tenant
from app.db.session import SessionLocal

TENANT_ID = "fixture-tenant-zhangqing"
ACCOUNT_SET_ID = "fixture-account-set-general-vat"
ACCOUNTS = [
    ("1002", "银行存款"), ("1405", "库存商品"),
    ("222101", "应交税费—应交增值税（进项税额）"),
    ("2202", "应付账款"), ("1601", "固定资产"), ("6602", "管理费用"),
]

def seed() -> None:
    with SessionLocal.begin() as db:
        tenant = db.get(Tenant, TENANT_ID)
        if tenant is None:
            tenant = Tenant(id=TENANT_ID, name="账清 OCR 验收演示企业（测试）", is_demo=True)
            db.add(tenant)
            db.flush()
        account_set = db.get(AccountSet, ACCOUNT_SET_ID)
        if account_set is None:
            db.add(AccountSet(id=ACCOUNT_SET_ID, tenant_id=TENANT_ID, name="一般纳税人验收账套（测试）", currency="CNY"))
            db.flush()
        existing = set(db.scalars(select(ChartAccount.code).where(ChartAccount.account_set_id == ACCOUNT_SET_ID)))
        for code, name in ACCOUNTS:
            if code not in existing:
                db.add(ChartAccount(account_set_id=ACCOUNT_SET_ID, code=code, name=name, is_active=True, is_leaf=True))
        if db.scalar(select(AccountingPeriod.id).where(AccountingPeriod.account_set_id == ACCOUNT_SET_ID, AccountingPeriod.period == "2024-12")) is None:
            db.add(AccountingPeriod(account_set_id=ACCOUNT_SET_ID, period="2024-12", status="OPEN", updated_by="fixture-seed"))

if __name__ == "__main__":
    seed()
    print("Fixture demo tenant seeded; this is test-only accounting configuration.")
