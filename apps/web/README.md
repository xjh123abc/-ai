# 账清 AI 前端

本目录包含 Vue 3 + TypeScript 工作台和浏览器端到端验收。

## 本地开发

```bash
npm install
npm run dev
```

Vite 默认把 `/api` 请求转发到 `http://127.0.0.1:8001`。生产构建使用 `npm run build`。

## 浏览器端到端验收

先安装 API 依赖及 OCR 模型，再安装 Playwright 浏览器：

```bash
cd services/api
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cd ../../apps/web
npm install
npx playwright install chromium
npm run test:e2e
```

Linux 还需要 Chromium 的系统运行库；依赖尚未安装时，可用 `npx playwright install-deps chromium` 安装。

测试会生成验收专用图片，启动隔离的 SQLite 数据库、临时文件目录、真实 FastAPI 服务和 Vite 页面，并运行本地 CPU OCR；浏览器随后完成上传、字段确认、业务关联、核对、凭证提交、另一账号审批和 CSV 导出。导出状态必须是“待人工入账”，测试不会连接或模拟 ERP 写入。服务退出后只清理本次运行创建的临时目录。

若本地存在仓库忽略的 45,200 元金标图像，Playwright 还会验证字段点击能打开正确页和 bbox 原件框；没有该授权样例时，仅跳过这项检查，合成图片的完整浏览器流程仍可运行。API 的精确 OCR 金标测试同样会在样例缺失时明确跳过。
