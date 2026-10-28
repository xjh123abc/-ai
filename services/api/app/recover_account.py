"""Reset a user's password from a trusted API host console."""

import argparse
import os
from getpass import getpass

from app.admin_users import reset_user_password
from app.db.session import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Existing account email address")
    args = parser.parse_args()
    password = getpass("新密码（至少 12 位）: ")
    confirmation = getpass("再次输入新密码: ")
    if password != confirmation:
        raise SystemExit("两次输入的密码不一致")

    try:
        with SessionLocal.begin() as db:
            revoked_count = reset_user_password(db, args.email, password, actor=f"admin-cli:uid-{os.getuid()}")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"已重设 {args.email.strip().lower()} 的密码，并撤销 {revoked_count} 个现有登录会话。")
    print("密码未写入命令行参数或日志；重置操作已按当前进程的操作系统 UID 写入所属企业审计记录。")


if __name__ == "__main__":
    main()
