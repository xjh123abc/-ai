# 账清 AI 本地 OCR 验收链路

当前可运行范围为本地免登录演示链路：图片上传、RapidOCR PP-OCRv6 Tiny CPU 识别、人工确认、用户显式关联、限定的 45,200 元采购 fixture 核对、规则凭证候选、审批和待人工入账 CSV。业务核对可关联合同，并对已确认的合同订购数量与入库数量差异进行阻断。

当前仍不是完整生产 MVP：网页直接进入测试账套，不要求账号或密码；API 与前端必须仅绑定本机 `127.0.0.1`，不得暴露到公网或作为生产系统使用。通用异常规则、OFD、真实 AI 建议和 ERP 写回仍未完成。PDF/CSV/XLSX 已有解析能力，但格式与场景验收尚未全量完成。PyMuPDF 采用 AGPL 或商业双许可，生产分发/部署前必须由项目负责人确认许可证适用性。规则候选不称为 AI；导出不表示 ERP 已入账。

## 启动

1. 启动仓库的 PostgreSQL 服务并创建仅本地使用的 `services/api/.env`（参照 `.env.example`），不要提交真实密码。
2. 在项目根目录安装 API 依赖：

   ```bash
   services/api/.venv/bin/pip install -e . -r services/api/requirements-lock.txt
   ```

   `requirements-lock.txt` 由 `requirements-dev.txt` 编译，包含运行和测试依赖的完整精确版本。更新直接依赖后，可从项目根目录运行以下命令更新锁文件：

   ```bash
   python3 -m venv /tmp/zq-lock-tools
   /tmp/zq-lock-tools/bin/pip install -r services/api/requirements-lock-tools.txt
   cd services/api
   /tmp/zq-lock-tools/bin/pip-compile --resolver=backtracking --generate-hashes --output-file=requirements-lock.txt requirements-dev.txt
   ```

3. 将 PP-OCRv6 Tiny 检测和识别模型放在 `OCR_MODEL_ROOT` 指向的目录。模型首次初始化也可由 RapidOCR 下载；运行机器需有网络，或提前放好模型文件。
4. 迁移并初始化**测试**账套：

   ```bash
   cd services/api
   .venv/bin/alembic upgrade head
   .venv/bin/python -m app.seed_demo
   .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001
   ```

5. 另一个终端启动前端：

   ```bash
   cd apps/web
   npm install
   npm run dev -- --host 127.0.0.1
   ```

浏览器打开 `http://127.0.0.1:5174` 即可进入本地测试账套，无需登录。上传三份样例后，逐份打开“查看并确认字段”，对照原件填写发票净额、税额、购买/销售方，入库确认状态和回单付款/收款方、已清算状态。然后分别指定发票、入库单、银行回单角色并创建核对组。审批只用于本地验收。

## 增量归集：乱序、单张到达的业务单据

新入口让采购、费用报销、设备购置和租赁业务可以**只收到一张单据时就建立待补充业务**。在单据中心逐张上传、人工确认，再选业务类型并关联；后续收到新的发票、入库单、银行回单、合同、付款证明、验收报告、费用报销单、出租车票据或通行费票据时，进入业务中心选中原业务，即可人工追加并重新核对。现有不同业务类型的必备凭证规则没有放宽，缺少关键证据时始终停留“待复核”，不能生成凭证。

业务中心也会展示少量“可能属于该业务”的单据候选：仅依据已确认的金额、交易主体、日期和合同编号，至少两项线索达到阈值才给出解释性建议；排序分不等于概率，**不会自动把票据挂到业务或自动制证**。已有非拒绝凭证的业务不允许直接追加新证据，以免静默改写历史版本。该功能不依赖柠檬云或任何 ERP 收费接口。

求职演示建议：先上传一张发票并建立待补充采购 → 模拟银行回单提前到达、入库单最后到达 → 展示候选匹配理由和人工追加 → 每次重跑核对 → 证据齐全后才允许生成凭证。再用费用报销单及后到的支付证明展示第二种业务类型。实现说明见 `docs/adr/0002-incremental-document-association.md`。

## 数据备份

`scripts/backup_postgres_and_files.py` 将 PostgreSQL custom dump 和 `STORAGE_DIR` 原件目录打包为同一备份集，生成 SHA-256 清单。两部分顺序生成，因此运行前必须停止全部 API 实例、后台作业及其他数据库/原件写入者；必填的 `--writes-quiesced` 是操作员确认，不会自动停止服务。运行环境需提供 PostgreSQL 客户端工具 `pg_dump` / `pg_restore`，并从受控环境变量传入连接串；密码会经临时权限为 `0600` 的 pgpass 文件传递，不会放在 `pg_dump` 参数中。

```bash
export DATABASE_URL='postgresql://backup-user:PASSWORD@db-host:5432/zhangqing'
export BACKUP_DIR='/mnt/approved-backup-volume/zhangqing'
export STORAGE_DIR='/srv/zhangqing/storage'
# 仅在企业管理员确认备份保留期限后设置；未设置时不会自动删除旧备份。
export BACKUP_RETENTION_DAYS=30
services/api/.venv/bin/python scripts/backup_postgres_and_files.py backup --writes-quiesced
services/api/.venv/bin/python scripts/backup_postgres_and_files.py verify "$BACKUP_DIR/backup-<UTC时间戳>"
```

备份目录应位于原件存储目录之外，完成后还需复制到异机/对象存储并按部署方密钥管理要求加密。当前脚本不负责定时调度、异机复制、数据/审计法定保留期限，也没有替生产数据库执行恢复；上线前需在隔离数据库演练 `pg_restore` 和原件解包，并记录恢复点与恢复耗时。不得把本地备份成功当成灾备验收。

## 生产身份开发模式

生产模式默认 `DEMO_MODE=false`，未登录的业务 API 返回 401。数据库迁移后，在 `services/api` 运行 `.venv/bin/python -m app.bootstrap_admin`，交互创建首个企业、账套和管理员；密码使用本地提示输入，不作为命令行参数。当前网页仅提供本地测试账套入口，不提供生产登录页面；生产模式下，管理员需使用受控 API 客户端调用登录 API，再创建成员。成员管理接口为 `POST /api/v1/auth/memberships`，创建账套接口为 `POST /api/v1/auth/account-sets`。新建成员时管理员需给出至少 12 位初始密码，并通过受控渠道交给成员。

会话使用随机 Bearer token，数据库只保存 token 哈希，默认 8 小时过期并支持登出撤销。登录失败会限速。若成员忘记密码，管理员可在可信 API 主机的 `services/api` 目录运行 `.venv/bin/python -m app.recover_account --email member@example.test`，按提示输入两次新密码；该操作会撤销此用户的现有会话，并向其所属企业写入审计记录，操作者记录为当前进程的操作系统 UID。命令不提供自助找回功能。真实环境部署仍需 HTTPS 和秘密轮换；目前实现不应直接暴露到公网。

密码恢复命令只应由可信主机上的管理员执行，避免通过命令行参数或聊天工具传递新密码。

首次导入 Tiny 权重时确保模型缓存目录可写。单张上传上限 12 MiB，图片还限制在 2,000 万像素以内，并将 OCR 输入长边缩至 1,800 像素；超限时保留原件并显示失败原因。当前接受 JPG/PNG/PDF/CSV/XLSX；OFD 不支持。PDF 最多解析 20 页，扫描页也执行像素上限检查；表格文件有行列和解压大小上限。文档原件保存在配置的数据目录，数据库写入仅限测试账套。

## 已验证

- Alembic 初始迁移已应用到开发数据库；fixture seed 可重复执行。
- 45,200 元三张原始 JPG 的 API 纵向链路已跑通：上传 → 人工确认 → 金额/类型/税额/收货/付款方核对 → 40,000 + 5,200 = 45,200 规则候选 → 不同演示操作者审批 → CSV `EXPORTED_PENDING_MANUAL_POST`。
- Vue/TypeScript 前端生产构建通过。
- API 全套测试通过（62 项通过、1 项跳过；跳过项依赖未提供的正式样例）；6 项 Playwright 浏览器验收覆盖采购主流程、凭证修正、冲突/缺回单、两页 PDF 浏览及合同数量短收阻断。OCR 与上传 API 的 15 张样例基准和限制见实施方案第 18 步。
- 前端生产构建、`git diff --check`、Python `compileall` 和 Alembic 单一 head 检查通过。

这些验证不等于生产权限隔离或会计政策审计，也没有授权真实 ERP 写入。
