"""Explicitly seed the non-production fixture tenant and chart of accounts."""
from sqlalchemy import select

from app.db.models import AccountSet, AccountingPeriod, ChartAccount, Tenant
from app.db.session import SessionLocal

TENANT_ID = "fixture-tenant-zhangqing"
ACCOUNT_SET_ID = "fixture-account-set-general-vat"
ACCOUNTS = [
    ("1001", "库存现金"), ("1002", "银行存款"), ("1122", "应收账款"),
    ("1123", "预付账款"), ("1221", "其他应收款"), ("1403", "原材料"),
    ("1405", "库存商品"), ("1601", "固定资产"), ("1602", "累计折旧"),
    ("1701", "无形资产"), ("2202", "应付账款"), ("2203", "预收账款"),
    ("2211", "应付职工薪酬"), ("2221", "应交税费"),
    ("222101", "应交税费—应交增值税（进项税额）"),
    ("222102", "应交税费—应交增值税（销项税额）"),
    ("2241", "其他应付款"), ("4001", "实收资本"), ("4103", "本年利润"),
    ("5001", "生产成本"), ("5301", "研发支出"),
    ("6001", "主营业务收入"), ("6051", "其他业务收入"),
    ("6401", "主营业务成本"), ("6601", "销售费用"),
    ("660101", "销售费用—业务招待费"), ("660102", "销售费用—差旅费"),
    ("6602", "管理费用"), ("660201", "管理费用—业务招待费"),
    ("660202", "管理费用—办公费"), ("660203", "管理费用—差旅费"),
    ("660204", "管理费用—职工福利费"), ("6603", "财务费用"),
    ("6801", "所得税费用"),
]
PARENT_CODES = {"2221", "6601", "6602"}

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
        existing = {row.code: row for row in db.scalars(select(ChartAccount).where(ChartAccount.account_set_id == ACCOUNT_SET_ID))}
        for code, name in ACCOUNTS:
            if code not in existing:
                db.add(ChartAccount(account_set_id=ACCOUNT_SET_ID, code=code, name=name, is_active=True, is_leaf=code not in PARENT_CODES))
            elif code in PARENT_CODES:
                existing[code].name = name
                existing[code].is_leaf = False
        if db.scalar(select(AccountingPeriod.id).where(AccountingPeriod.account_set_id == ACCOUNT_SET_ID, AccountingPeriod.period == "2024-12")) is None:
            db.add(AccountingPeriod(account_set_id=ACCOUNT_SET_ID, period="2024-12", status="OPEN", updated_by="fixture-seed"))

if __name__ == "__main__":
    seed()
    print("Fixture demo tenant seeded; this is test-only accounting configuration.")
