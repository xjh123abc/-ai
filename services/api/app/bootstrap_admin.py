"""Create the first production tenant/account set/admin without a public signup route."""
from getpass import getpass

from app.db.models import AccountSet, AppUser, Tenant, TenantMembership
from app.db.session import SessionLocal
from app.security import hash_password


def main() -> None:
    name = input("企业名称: ").strip()
    account_name = input("账套名称: ").strip()
    email = input("管理员邮箱: ").strip().lower()
    password = getpass("管理员初始密码（至少 12 位）: ")
    confirm = getpass("再次输入密码: ")
    if not name or not account_name or not email or password != confirm:
        raise SystemExit("输入不完整或两次密码不一致")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    with SessionLocal.begin() as db:
        if db.query(AppUser).filter_by(email=email).first():
            raise SystemExit("该邮箱已经存在")
        tenant = Tenant(name=name, is_demo=False)
        db.add(tenant)
        db.flush()
        account = AccountSet(tenant_id=tenant.id, name=account_name, currency="CNY")
        db.add(account)
        db.flush()
        user = AppUser(email=email, display_name=name + " 管理员", password_hash=password_hash)
        db.add(user)
        db.flush()
        db.add(TenantMembership(user_id=user.id, tenant_id=tenant.id,
                                account_set_id=account.id, role="TENANT_ADMIN"))
    print("首个企业管理员已创建。请保存好企业 ID、账套 ID 和管理员邮箱；密码未写入日志。")
    print(f"tenant_id={tenant.id} account_set_id={account.id} email={email}")


if __name__ == "__main__":
    main()
