# 账清 AI 本地 OCR 验收链路

当前可运行范围包括本地演示链路，以及正在补齐的生产身份底座：图片上传、RapidOCR PP-OCRv6 Tiny CPU 识别、人工确认、用户显式关联、限定的 45,200 元采购 fixture 核对、规则凭证候选、分角色审批和待人工入账 CSV。生产模式提供登录会话、账套成员角色与范围隔离；业务人员指派范围等仍待完善。

当前仍不是完整生产 MVP：`X-Actor` 仅在 `DEMO_MODE=true` 时作为本地演示身份，不能用作生产认证；演示 API 必须绑定 `127.0.0.1`。通用异常规则、业务人员指派范围、OFD、真实 AI 建议和 ERP 写回仍未完成。PDF/CSV/XLSX 已有解析能力，但格式与场景验收尚未全量完成。PyMuPDF 采用 AGPL 或商业双许可，生产分发/部署前必须由项目负责人确认许可证适用性。规则候选不称为 AI；导出不表示 ERP 已入账。

## 启动

1. 启动仓库的 PostgreSQL 服务并创建仅本地使用的 `services/api/.env`（参照 `.env.example`），不要提交真实密码。
2. 在项目根目录安装 API 依赖：

   ```bash
   services/api/.venv/bin/pip install -e . -r services/api/requirements.txt
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

浏览器打开 `http://127.0.0.1:5174`。本地演示模式进入测试账套；生产模式使用管理员分配的账号登录。上传三份样例后，逐份打开“查看并确认字段”，对照原件填写发票净额、税额、购买/销售方，入库确认状态和回单付款/收款方、已清算状态。然后分别指定发票、入库单、银行回单角色并创建核对组。审批必须由制单人以外且有审批权限的用户完成；演示环境中的审批角色只用于本地验收。

## 生产身份开发模式

生产模式默认 `DEMO_MODE=false`，未登录的业务 API 返回 401。数据库迁移后，在 `services/api` 运行 `.venv/bin/python -m app.bootstrap_admin`，交互创建首个企业、账套和管理员；密码使用本地提示输入，不作为命令行参数。之后管理员登录网页，可用登录 API 创建成员；目前成员管理接口为 `POST /api/v1/auth/memberships`，创建账套接口为 `POST /api/v1/auth/account-sets`。新建成员时管理员需给出至少 12 位初始密码，并通过受控渠道交给成员。

会话使用随机 Bearer token，数据库只保存 token 哈希，默认 8 小时过期并支持登出撤销。部署到真实环境前仍需 HTTPS、登录限速、密码重置与恢复流程、秘密轮换和业务人员指派范围；目前实现不应直接暴露到公网。

首次导入 Tiny 权重时确保模型缓存目录可写。单张上传上限 12 MiB，当前接受 JPG/PNG/PDF/CSV/XLSX；OFD 不支持。PDF 最多解析 20 页，表格文件有行列和解压大小上限。文档原件保存在配置的数据目录，数据库写入仅限测试账套。

## 已验证

- Alembic 初始迁移已应用到开发数据库；fixture seed 可重复执行。
- 45,200 元三张原始 JPG 的 API 纵向链路已跑通：上传 → 人工确认 → 金额/类型/税额/收货/付款方核对 → 40,000 + 5,200 = 45,200 规则候选 → 不同演示操作者审批 → CSV `EXPORTED_PENDING_MANUAL_POST`。
- Vue/TypeScript 前端生产构建通过。
- API 解析测试通过（6 项），覆盖 CSV/XLSX 行证据、PDF 文本层/页数上限和扫描 PDF OCR；`alembic check` 无待生成迁移。

这些验证不等于生产权限隔离或会计政策审计，也没有授权真实 ERP 写入。
