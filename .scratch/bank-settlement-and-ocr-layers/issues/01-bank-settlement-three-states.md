# 01: 银行回单清算状态支持三态

**What to build:** 财务人员可在银行回单中选择并保存“已清算”“未清算”或“无法确认”，且业务核对按三态正确判断清算证据。

**Blocked by:** None

**Status:** Implemented; browser E2E unverified because Chromium cannot start without `libnspr4.so`.

- [x] 银行回单抽取和确认字段以显式三态值表示清算状态，并兼容读取旧 true / false / null 值。
- [x] 界面提供三个中文选项，保存后能在 API 读取到相同状态。
- [x] 只有“已清算”满足清算核对；“未清算”与“无法确认”均不能通过。
- [x] API 回归覆盖候选、保存与核对结果；浏览器验收已写，运行受环境阻塞。
