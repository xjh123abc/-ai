# DeepSeek 实际制证验收

- 2026-10-28，演示账套中 7 张旧规则凭证已删除；旧 `fixture-purchase-v1` 业务标签已清零。
- 三张 45,200 元样例单据均使用最新已确认的第 9 版字段：数电发票、入库单、银行回单。
- 经 `POST /api/v1/business-events/880d8054-8ab3-4d6d-b236-2b75ebae3db8/voucher-drafts` 真实调用 DeepSeek，保存凭证 `2254c6c1-a6da-464d-bac0-a5193611460d`。
- 已保存记录：`provider=DEEPSEEK:deepseek-flash`，`call_status=SUCCEEDED`，`validation_status=PASSED`，来源版本 `[9,9,9]`；借贷各 45,200.00 元。
- DeepSeek 选择 1405 库存商品借方 40,000.00 元、222101 进项税借方 5,200.00 元、1002 银行存款贷方 45,200.00 元。模型提示入库单货物名称和数量为空，且进项税抵扣资格需人工确认。
- 运行后数据库只有这一张 DeepSeek 候选凭证；旧 `LOCAL_FIXTURE_RULES` / `RULE_CANDIDATE` 凭证数为 0。
- 验证通过：`tests/api/test_ai_suggestions.py`、`tests/api/test_workflow_roles.py`、前端 `npm run build`、`git diff --check`。
