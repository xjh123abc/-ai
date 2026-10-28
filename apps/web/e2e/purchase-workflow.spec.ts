import { expect, test, type Page } from '@playwright/test'
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { basename, resolve } from 'node:path'

const useRealDeepSeek = process.env.REAL_DEEPSEEK_E2E === '1'
const acceptOriginalAiDraft = process.env.REAL_DEEPSEEK_ACCEPT_ORIGINAL === '1'
const simulatedAiFailureMode = process.env.PLAYWRIGHT_AI_FAILURE_E2E
const simulatedAiFailureMessage = simulatedAiFailureMode === 'invalid_structure'
  ? 'DeepSeek 返回凭证候选结构无效'
  : 'DeepSeek 请求超时'

const password = 'Playwright-test-password-2026!'
const privateInvoice = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/01_采购入库付款/01_数电发票_45200.jpg',
)
const privateGoodsReceipt = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/01_采购入库付款/02_入库单_45200.jpg',
)
const privateTollReceipt = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/04_补充单据/06_通行费票据_脱敏独立样本.jpg',
)
const privateBankReceipt = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/01_采购入库付款/03_银行回单_45200.jpg',
)
const privatePaymentProof = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/02_多票费用报销/03_支付证明_4448.jpg',
)
const privateExpenseInvoice = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/02_多票费用报销/01_餐饮住宿发票_4448.jpg',
)
const privateExpenseBankReceipt = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/02_多票费用报销/02_银行回单_4448.jpg',
)
const privateAssetInvoice = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/04_补充单据/01_电脑购置_数电发票_6780.jpg',
)
const privateAssetBankReceipt = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/04_补充单据/02_电脑购置_银行回单_6780.jpg',
)
const privateRentalInvoice = resolve(
  process.cwd(),
  '../../发票账单数据/zhangqing_raw_docs_20261025/04_补充单据/03_办公室租赁_数电发票_21800.jpg',
)

async function syntheticDocument(page: Page, title: string): Promise<Buffer> {
  const base64 = await page.evaluate((text) => {
    const canvas = document.createElement('canvas')
    canvas.width = 1200
    canvas.height = 260
    const context = canvas.getContext('2d')!
    context.fillStyle = '#fff'
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.fillStyle = '#111'
    context.font = 'bold 68px Arial'
    context.fillText(text, 30, 150)
    return canvas.toDataURL('image/png').split(',')[1] ?? ''
  }, title)
  return Buffer.from(base64, 'base64')
}

async function syntheticTwoPagePdf(page: Page): Promise<Buffer> {
  await page.setContent(`<!doctype html><style>@page{size:letter;margin:.5in}.page{height:9in;page-break-after:always;font:24px sans-serif}.page:last-child{page-break-after:auto}</style><div class="page">价税合计 45200.00 purchase document page one</div><div class="page">Evidence page two</div>`)
  return page.pdf({ format: 'Letter', printBackground: true })
}

async function signIn(page: Page, email: string) {
  const response = await page.request.post('/api/v1/auth/login', {
    data: { email, password },
  })
  expect(response.ok()).toBeTruthy()
  const { access_token } = await response.json()
  await page.addInitScript((value: string) => sessionStorage.setItem('zq-access-token', value), access_token)
  await page.goto('/')
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
}

async function expectMobileLayout(page: Page) {
  const width = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }))
  expect(width.content).toBeLessThanOrEqual(width.viewport + 1)
}

async function uploadAndConfirm(
  page: Page,
  filename: string,
  image: Buffer,
  documentType: string,
  fields: Record<string, string>,
) {
  await page.locator('input[type="file"]').setInputFiles({ name: filename, mimeType: filename.toLowerCase().endsWith('.jpg') ? 'image/jpeg' : 'image/png', buffer: image })
  const row = page.locator('article.document-row').filter({ hasText: filename })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  await expect(editor).toBeVisible()
  await expect(editor.locator('.ocr-meta')).toContainText('RapidOCR')
  await editor.getByLabel('单据类型').selectOption(documentType)
  for (const [field, value] of Object.entries(fields)) {
    const control = editor.getByLabel(field, { exact: true })
    if (field === 'settled') await control.selectOption(value)
    else await control.fill(value)
  }
  const saveResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/documents/') && response.url().endsWith('/extraction')
      && response.request().method() === 'PATCH',
  )
  await editor.getByRole('button', { name: '保存确认版本' }).click()
  expect((await saveResponse).status()).toBe(200)
  await expect(row.locator('.status-pill')).toHaveText('已确认')
  await expect(row).toContainText(fields.gross_amount ? '关键金额：¥' : '关键金额：未识别')
}

test('采购单据从上传、人工确认流转到审批和待人工入账导出', async ({ page, browser }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const invoiceImage = useRealDeepSeek ? await readFile(privateInvoice) : await syntheticDocument(page, 'INVOICE TOTAL 45200.00')
  const receiptImage = useRealDeepSeek ? await readFile(privateGoodsReceipt) : await syntheticDocument(page, 'GOODS RECEIPT 45200.00')
  const bankImage = useRealDeepSeek ? await readFile(privateBankReceipt) : await syntheticDocument(page, 'BANK RECEIPT 45200.00')
  const invoiceFilename = useRealDeepSeek ? 'purchase-invoice-gold.jpg' : 'purchase-invoice.png'
  const receiptFilename = useRealDeepSeek ? 'purchase-goods-receipt-gold.jpg' : 'purchase-goods-receipt.png'
  const bankFilename = useRealDeepSeek ? 'purchase-bank-receipt-gold.jpg' : 'purchase-bank-receipt.png'
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await expectMobileLayout(page)

  await uploadAndConfirm(page, invoiceFilename, invoiceImage, 'VAT_INVOICE', {
    invoice_number: '24360125413017362003',
    issue_date: '2024-12-01',
    buyer: '南昌市杰盛科技有限公司',
    seller: '南昌市视维科技公司',
    net_amount: '40000.00',
    tax_rate: '13%',
    tax_amount: '5200.00',
    gross_amount: '45200.00',
  })
  const invoiceRow = page.locator('article.document-row').filter({ hasText: invoiceFilename })
  await invoiceRow.getByRole('button', { name: '查看并确认字段' }).click()
  const invoiceEditor = page.locator('.field-editor').last()
  await invoiceEditor.locator('.field-editor-footer input').fill('复核后确认字段版本留痕')
  await invoiceEditor.getByRole('button', { name: '保存确认版本' }).click()
  const historyResponse = page.waitForResponse((response) =>
    /\/api\/v1\/documents\/[0-9a-f-]{36}$/.test(new URL(response.url()).pathname)
      && response.request().method() === 'GET',
  )
  await invoiceRow.getByRole('button', { name: '查看并确认字段' }).click()
  const invoiceHistory = await (await historyResponse).json()
  await expect(page.locator('.current-extraction-version')).toContainText('复核后确认字段版本留痕')
  await expectMobileLayout(page)
  expect(invoiceHistory.extraction_history).toHaveLength(3)
  expect(invoiceHistory.extraction_history[0].fields.invoice_number.normalized_value).toBe('24360125413017362003')
  expect(invoiceHistory.extraction_history[2].fields.invoice_number.normalized_value).toBeNull()
  await invoiceRow.getByRole('button', { name: '收起字段' }).click()
  await uploadAndConfirm(page, receiptFilename, receiptImage, 'GOODS_RECEIPT', {
    receipt_number: 'NO56784001',
    receipt_date: '2024-12-01',
    item: '教学专用VR眼镜',
    quantity: '100',
    gross_amount: '45200.00',
    received: 'true',
  })
  await uploadAndConfirm(page, bankFilename, bankImage, 'BANK_RECEIPT', {
    payer: '南昌市杰盛科技有限公司',
    payee: '南昌市视维科技公司',
    settlement_date: '2024-12-01',
    gross_amount: '45200.00',
    settled: 'SETTLED',
  })

  const rolesByFile: Record<string, string> = {
    [invoiceFilename]: 'INVOICE',
    [receiptFilename]: 'GOODS_RECEIPT',
    [bankFilename]: 'BANK_RECEIPT',
  }
  for (const filename of Object.keys(rolesByFile)) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }
  await expect(page.getByLabel('单据业务角色')).toHaveCount(0)
  const reconcileResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/business-events/') && response.url().endsWith('/reconcile') && response.request().method() === 'POST',
  )
  const eventCreateResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/v1/business-events') && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '确认关联并核对' }).click()
  const createdEvent = await (await eventCreateResponse).json()
  const reconciliation = await (await reconcileResponse).json()
  expect(reconciliation.rule_version).toBe('account-set-chart-v1')
  await test.info().attach('purchase-reconciliation-rule.json', {
    body: JSON.stringify({ ruleVersion: reconciliation.rule_version, status: reconciliation.status, findings: reconciliation.findings }, null, 2),
    contentType: 'application/json',
  })
  await expect(page.locator('.alert')).toContainText('核对完成：核对一致')
  await expect(page.getByText('业务金额 ¥45,200.00')).toBeVisible()
  await expectMobileLayout(page)
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await expect(page.locator('article.document-row').filter({ hasText: invoiceFilename })).toContainText(createdEvent.id.slice(0, 8))
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '业务中心' }).click()
  await page.locator('.business-list-item').filter({ hasText: createdEvent.id.slice(0, 8) }).click()

  const voucherResponsePromise = page.waitForResponse((response) =>
    response.url().includes('/api/v1/business-events/') && response.url().endsWith('/voucher-drafts')
      && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '生成凭证草稿' }).click()
  const generatedVoucherResponse = await voucherResponsePromise
  if (simulatedAiFailureMode) {
    expect(generatedVoucherResponse.status()).toBe(503)
    await expect(page.locator('.alert')).toContainText(simulatedAiFailureMessage)
    await expect(page.locator('.voucher-detail')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '提交审核' })).toHaveCount(0)
    return
  }
  const generatedVoucher = await generatedVoucherResponse.json()
  expect(generatedVoucherResponse.status(), JSON.stringify(generatedVoucher)).toBe(200)
  await expect(page.getByRole('heading', { name: '凭证草稿与审批' })).toBeVisible()
  await expect(page.locator('.voucher-detail')).toContainText('45,200.00')
  await expect(page.locator('.voucher-detail')).toContainText(useRealDeepSeek ? 'AI 生成' : '凭证建议')
  if (useRealDeepSeek) {
    expect(generatedVoucher.policy.provider).toMatch(/^DEEPSEEK:/)
    expect(generatedVoucher.policy.trace).toEqual({ call_status: 'SUCCEEDED', validation_status: 'PASSED' })
    expect(generatedVoucher.validations.every((validation: { passed: boolean }) => validation.passed)).toBe(true)
    await test.info().attach('real-deepseek-voucher-result.json', {
      body: JSON.stringify({
        provider: generatedVoucher.policy.provider,
        model: generatedVoucher.policy.model,
        prompt_version: generatedVoucher.policy.prompt_version,
        trace: generatedVoucher.policy.trace,
        validations: generatedVoucher.validations,
        line_count: generatedVoucher.lines.length,
        debit_total: generatedVoucher.debit_total,
        credit_total: generatedVoucher.credit_total,
      }, null, 2),
      contentType: 'application/json',
    })
  } else {
    await expect(page.locator('.voucher-detail')).toContainText('PLAYWRIGHT_TEST_STUB')
  }
  await expect(page.locator('.ai-rationale').first()).toBeVisible()
  await expect(page.locator('.voucher-sources').getByRole('button')).toHaveCount(3)
  await expect(page.locator('.entry-table').getByRole('button', { name: invoiceFilename })).toHaveCount(0)
  await page.locator('.voucher-sources').getByRole('button', { name: invoiceFilename }).click()
  await expect(page.locator('.voucher-evidence-panel')).toContainText(`原始单据：${invoiceFilename}`)
  await expect(page.locator('.voucher-evidence-panel .source-field')).toHaveCount(0)
  await expect(page.locator('.voucher-evidence-preview')).toBeVisible()
  await page.locator('.voucher-evidence-panel').getByRole('button', { name: '关闭' }).click()
  await expectMobileLayout(page)
  await expect(page.locator('.entry-table')).toContainText('1405')
  await expect(page.locator('.entry-table')).toContainText('222101')
  if (!useRealDeepSeek || !acceptOriginalAiDraft) {
    await page.getByRole('button', { name: '修正凭证' }).click()
    await page.getByRole('textbox', { name: '摘要' }).first().fill('采购入库设备（浏览器验收修正版）')
    await page.getByPlaceholder('请说明为什么修改这份凭证').fill('浏览器验收：补充清楚的分录摘要')
    await page.getByRole('textbox', { name: '辅助核算 JSON 1405' }).fill('{')
    await page.getByRole('button', { name: '保存为新版本并重新校验' }).click()
    await expect(page.locator('.alert')).toContainText('辅助核算必须是有效的 JSON 对象')
    await page.getByRole('textbox', { name: '辅助核算 JSON 1405' }).fill('{}')
    await page.getByRole('button', { name: '保存为新版本并重新校验' }).click()
    await expect(page.locator('.alert')).toContainText('修正已保存为凭证新版本 V2')
    await expect(page.locator('.voucher-detail .version-tag')).toHaveText('V2')
    await expect(page.locator('.entry-table')).toContainText('采购入库设备（浏览器验收修正版）')
    await expect(page.locator('.entry-table')).toContainText('辅助核算')
  } else {
    await expect(page.locator('.voucher-detail .version-tag')).toHaveText('V1')
    await expect(page.locator('.entry-table')).toContainText('1405')
  }
  await page.getByRole('button', { name: '提交审核' }).click()
  await expect(page.locator('.alert')).toContainText('已提交审核')

  const reviewer = await browser.newPage()
  await reviewer.setViewportSize({ width: 390, height: 844 })
  await signIn(reviewer, 'approver@playwright.example.test')
  await reviewer.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '凭证中心' }).click()
  const voucher = reviewer.locator('.voucher-list-item').first()
  await expect(voucher).toBeVisible()
  await voucher.click()
  await expect(reviewer.locator('.voucher-detail')).toContainText('待审核')
  await expect(reviewer.getByRole('button', { name: '批准凭证' })).toBeVisible()
  await expectMobileLayout(reviewer)
  await reviewer.getByRole('button', { name: '批准凭证' }).click()
  await expect(reviewer.locator('.alert')).toContainText('审批完成')
  await expect(reviewer.locator('.voucher-detail .status-pill')).toHaveText('已批准')
  await reviewer.close()

  await page.getByRole('button', { name: '刷新数据' }).click()
  await expect(page.locator('.voucher-list-item').first()).toContainText('已批准')
  await page.locator('.voucher-list-item').first().click()
  await expect(page.locator('.voucher-detail')).toContainText('已批准')
  const downloadPromise = page.waitForEvent('download')
  const exportResponsePromise = page.waitForResponse((response) =>
    response.url().includes('/api/v1/voucher-drafts/') && response.url().endsWith('/export'),
  )
  await page.getByRole('button', { name: '导出待人工入账 CSV' }).click()
  const download = await downloadPromise
  const exportResponse = await exportResponsePromise
  expect(basename(download.suggestedFilename())).toMatch(/\.csv$/)
  expect(exportResponse.headers()['x-export-format']).toBe('generic-voucher-csv-v1')
  const downloadedCsv = await readFile((await download.path())!, 'utf8')
  expect(downloadedCsv).toContain('待人工入账')
  expect(downloadedCsv).not.toContain('已核实入账')
  await expect(page.locator('.voucher-detail')).toContainText('已导出待人工入账')
  await expect(page.locator('.voucher-detail')).toContainText('尚未写入 ERP')
  await expect(page.locator('.voucher-detail')).not.toContainText('已核实入账')

  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '财务分析' }).click()
  await expect(page.getByRole('heading', { name: '业务与凭证处理摘要' })).toBeVisible()
  const matchedAmount = page.locator('.kpi-card').filter({ hasText: '已匹配业务候选金额' })
  await expect(matchedAmount).toContainText('45,200.00')
  await expect(page.locator('.source-note')).toContainText('不等同收入')
  await expect(page.locator('.source-note')).toContainText('正式入账金额')
  await expect(page.locator('.content-card').filter({ hasText: '最近业务证据' })).toContainText('全部时间')
  await expect(page.locator('.content-card').filter({ hasText: '最近业务证据' })).toContainText('最近 5 笔')
  await page.getByRole('button', { name: '查看业务档案' }).click()
  await expect(page.locator('.archive-list')).toContainText(createdEvent.id.slice(0, 8))
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '财务分析' }).click()
  await page.locator('.business-list-item').filter({ hasText: createdEvent.id.slice(0, 8) }).click()
  await expect(page.locator('.detail-card')).toContainText(createdEvent.id.slice(0, 8))
  await expectMobileLayout(page)
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '凭证中心' }).click()
  await page.locator('.voucher-list-item').first().click()
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '删除凭证' }).click()
  await expect(page.locator('.alert')).toContainText('已删除')
  await expect(page.locator('.voucher-list-item')).toHaveCount(0)
})

test('本地金标发票字段可打开带 bbox 的原件位置', async ({ page }) => {
  test.skip(!existsSync(privateInvoice), '本地金标图像未提供；该样例不会被提交到 Git。')
  await page.setViewportSize({ width: 390, height: 844 })
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await page.locator('input[type="file"]').setInputFiles(privateInvoice)

  const filename = basename(privateInvoice)
  const row = page.locator('article.document-row').filter({ hasText: filename })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  await expect(editor.locator('.document-type-suggestion')).toContainText('增值税发票')
  await expect(editor.locator('.document-type-suggestion')).toContainText('发票号码')
  await editor.getByRole('button', { name: '采用建议' }).click()
  await expect(editor.locator('.field-editor-heading select')).toHaveValue('VAT_INVOICE')
  await expect(editor.getByLabel('gross_amount')).toHaveValue('45200.00')
  await editor.locator('.ocr-text summary').click()
  const ocrMeta = await editor.locator('.ocr-meta').innerText()
  expect(ocrMeta).toContain('RapidOCR')
  expect(ocrMeta).toContain('PP-OCRv6-Tiny/CPU')
  expect(ocrMeta).toContain('max-edge=1800px')
  await test.info().attach('gold-invoice-ocr-model.json', {
    body: JSON.stringify({ model: ocrMeta, expectedField: 'gross_amount', expectedValue: '45200.00' }, null, 2),
    contentType: 'application/json',
  })
  await editor.getByLabel('gross_amount').click()
  await expect(editor.locator('.source-preview-panel')).toContainText('原件第 1 页')
  await expect(editor.locator('.source-evidence-box')).toBeVisible()
  await expectMobileLayout(page)
})

test('通行费类型建议使用票据号码字段标签而非入库单号', async ({ page }) => {
  test.skip(!existsSync(privateTollReceipt), '本地通行费金标图未提供；该样例不会被提交到 Git。')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await page.locator('input[type="file"]').setInputFiles(privateTollReceipt)
  const row = page.locator('article.document-row').filter({ hasText: basename(privateTollReceipt) })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  await expect(editor.locator('.document-type-suggestion')).toContainText('通行费票据')
  await editor.getByRole('button', { name: '采用建议' }).click()
  await expect(editor.locator('.field-editor-heading select')).toHaveValue('TOLL_RECEIPT')
  await expect(editor.locator('.field-grid label').filter({ hasText: '票据号码' })).toBeVisible()
  await expect(editor.getByText('入库单号', { exact: true })).toHaveCount(0)
  const saveResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/documents/') && response.url().endsWith('/extraction')
      && response.request().method() === 'PATCH',
  )
  await editor.getByRole('button', { name: '保存确认版本' }).click()
  expect((await saveResponse).status()).toBe(200)
  await expect(row).toContainText('票据号码')
  await expect(row).not.toContainText('入库单号')
})

test('银行回单候选显示付款方、收款方、金额及其原件证据', async ({ page }) => {
  test.skip(!existsSync(privateBankReceipt), '本地银行回单金标图未提供；该样例不会被提交到 Git。')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await page.locator('input[type="file"]').setInputFiles(privateBankReceipt)
  const row = page.locator('article.document-row').filter({ hasText: basename(privateBankReceipt) })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  await expect(editor.locator('.document-type-suggestion')).toContainText('银行回单')
  await editor.getByRole('button', { name: '采用建议' }).click()
  await expect(editor.getByLabel('payer')).toHaveValue('南昌市杰盛科技有限公司')
  await expect(editor.getByLabel('payee')).toHaveValue('南昌市视维科技公司')
  await expect(editor.getByLabel('gross_amount')).toHaveValue('45200.00')
  await expect(editor.getByLabel('settlement_date')).toHaveValue('2024-12-01')
  await expect(editor.getByLabel('settled')).toHaveValue('UNKNOWN')
  await expect(editor.getByLabel('settled').locator('option')).toHaveText(['已清算', '未清算', '无法确认'])
  await expect(editor.locator('.field-grid label').filter({ hasText: '结算日期' })).toBeVisible()
  await editor.getByLabel('payer').click()
  await expect(editor.locator('.source-preview-panel')).toContainText('原件第 1 页')
  await expect(editor.locator('.source-evidence-box')).toBeVisible()
  const saveResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/documents/') && response.url().endsWith('/extraction')
      && response.request().method() === 'PATCH',
  )
  await editor.getByLabel('settled').selectOption('NOT_SETTLED')
  await editor.getByRole('button', { name: '保存确认版本' }).click()
  expect((await saveResponse).status()).toBe(200)
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  await expect(page.locator('.field-editor').last().getByLabel('settled')).toHaveValue('NOT_SETTLED')
})

test('银行回单审核页分层展示辅助字段并默认脱敏账号', async ({ page }) => {
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  const filename = 'layered-bank-receipt.png'
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: 'image/png',
    buffer: await syntheticDocument(page, 'BANK RECEIPT'),
  })
  const row = page.locator('article.document-row').filter({ hasText: filename })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await page.route(/\/api\/v1\/documents\/[0-9a-f-]+$/, async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    body.extraction.document_type = 'BANK_RECEIPT'
    body.extraction.fields = {
      ...body.extraction.fields,
      payer_account_number: { normalized_value: '6222001234567890', confidence: 0.45, status: 'NEEDS_REVIEW', evidence_text: '付款人账号 6222001234567890' },
      bank_name: { normalized_value: '招商银行南昌分行', confidence: 0.45, status: 'NEEDS_REVIEW', evidence_text: '开户银行 招商银行南昌分行' },
      electronic_receipt_number: { normalized_value: 'DZHD20241201001', confidence: 0.45, status: 'NEEDS_REVIEW', evidence_text: '电子回单号 DZHD20241201001' },
      transaction_id: { normalized_value: 'JYLS20241201002', confidence: 0.45, status: 'NEEDS_REVIEW', evidence_text: '交易流水号 JYLS20241201002' },
    }
    body.extraction.full_text = '网上银行电子回单\n付款人账号 6222001234567890\n开户银行 招商银行南昌分行\n电子回单号 DZHD20241201001\n交易流水号 JYLS20241201002'
    await route.fulfill({ response, json: body })
  })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  await expect(editor.locator('.field-tier')).toContainText('第一层 · 记账关键字段')
  const auxiliary = editor.locator('.auxiliary-fields')
  await expect(auxiliary.locator('summary')).toContainText('第二层 · 业务辅助字段')
  await expect(auxiliary).not.toHaveAttribute('open', '')
  await auxiliary.locator('summary').click()
  await expect(auxiliary).toContainText('****7890')
  await expect(auxiliary).not.toContainText('6222001234567890')
  const rawText = editor.locator('.ocr-text')
  await expect(rawText.locator('summary')).toContainText('第三层 · 查看 OCR 识别原文')
  await expect(rawText).not.toHaveAttribute('open', '')
  await rawText.locator('summary').click()
  await expect(rawText.locator('pre')).toContainText('付款人账号 6222001234567890')
})

test('银行回单三种清算状态都能从审核页保存', async ({ page }) => {
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  const filename = 'settlement-state-bank-receipt.png'
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: 'image/png',
    buffer: await syntheticDocument(page, 'BANK RECEIPT'),
  })
  const row = page.locator('article.document-row').filter({ hasText: filename })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  await editor.locator('.field-editor-heading select').selectOption('BANK_RECEIPT')
  for (const [index, state] of ['SETTLED', 'NOT_SETTLED', 'UNKNOWN'].entries()) {
    await editor.getByLabel('settled').selectOption(state)
    const saveResponse = page.waitForResponse((response) =>
      response.url().includes('/api/v1/documents/') && response.url().endsWith('/extraction')
        && response.request().method() === 'PATCH',
    )
    await editor.getByRole('button', { name: '保存确认版本' }).click()
    expect((await saveResponse).status()).toBe(200)
    if (index < 2) {
      await row.getByRole('button', { name: '查看并确认字段' }).click()
      await expect(editor.getByLabel('settled')).toHaveValue(state)
    }
  }
})

test('支付证明候选保留报销事由、证明日期和证明金额语义并可人工确认', async ({ page }) => {
  test.skip(!existsSync(privatePaymentProof), '本地支付证明金标图未提供；该样例不会被提交到 Git。')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await page.locator('input[type="file"]').setInputFiles(privatePaymentProof)
  const row = page.locator('article.document-row').filter({ hasText: basename(privatePaymentProof) })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  await expect(editor.locator('.document-type-suggestion')).toContainText('支付证明')
  await expect(editor.getByLabel('单据类型')).toHaveValue('PAYMENT_PROOF')
  await expect(editor.getByLabel('purpose')).toHaveValue('报销业务招待费')
  await expect(editor.getByLabel('payment_date')).toHaveValue('2024-12-07')
  await expect(editor.getByLabel('gross_amount')).toHaveValue('4448.00')
  await expect(editor.locator('.field-grid label').filter({ hasText: '证明日期' })).toBeVisible()
  await expect(editor.locator('.field-grid label').filter({ hasText: '证明金额' })).toBeVisible()
  const saveResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/documents/') && response.url().endsWith('/extraction')
      && response.request().method() === 'PATCH',
  )
  await editor.getByRole('button', { name: '保存确认版本' }).click()
  expect((await saveResponse).status()).toBe(200)
  await expect(row.locator('.status-pill')).toHaveText('已确认')
})

test('4,448元费用报销将发票、银行回单和支付证明分角色关联并等待费用政策', async ({ page }) => {
  test.skip(![privateExpenseInvoice, privateExpenseBankReceipt, privatePaymentProof].every(existsSync), '本地4,448元费用报销金标材料未齐；文件不会被提交到 Git。')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()

  const invoiceName = basename(privateExpenseInvoice)
  const bankName = basename(privateExpenseBankReceipt)
  const proofName = basename(privatePaymentProof)
  await uploadAndConfirm(page, invoiceName, await readFile(privateExpenseInvoice), 'VAT_INVOICE', {
    invoice_number: '24360123413017362987', issue_date: '2024-12-07',
    buyer: '南昌市杰盛科技有限公司', seller: '南昌市露森酒店',
    net_amount: '4196.23', tax_rate: '6%', tax_amount: '251.77', gross_amount: '4448.00',
  })
  await uploadAndConfirm(page, bankName, await readFile(privateExpenseBankReceipt), 'BANK_RECEIPT', {
    payer: '南昌市杰盛科技有限公司', payee: '南昌市露森酒店',
    settlement_date: '2024-12-07', gross_amount: '4448.00',
  })
  await uploadAndConfirm(page, proofName, await readFile(privatePaymentProof), 'PAYMENT_PROOF', {
    payment_date: '2024-12-07', purpose: '报销业务招待费', gross_amount: '4448.00',
  })

  const expenseRoles: Record<string, string> = {
    [invoiceName]: 'INVOICE', [bankName]: 'BANK_RECEIPT', [proofName]: 'PAYMENT_PROOF',
  }
  for (const filename of Object.keys(expenseRoles)) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }
  await page.getByLabel('业务类型').selectOption('EXPENSE')
  const reconciliationResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/business-events/') && response.url().endsWith('/reconcile')
      && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '确认关联并核对' }).click()
  const reconciliation = await (await reconciliationResponse).json()
  expect(reconciliation.rule_version).toBe('unconfigured-expense-v1')
  expect(reconciliation.status).toBe('NEEDS_REVIEW')
  const findings = reconciliation.findings.map((finding: { code: string }) => finding.code)
  expect(findings).toContain('EXPENSE_ACCOUNT_POLICY_REVIEW_REQUIRED')
  expect(findings).toContain('BANK_SETTLEMENT_NOT_CONFIRMED')
  expect(findings).not.toContain('GROSS_AMOUNT_MISMATCH')
  expect(findings).not.toContain('INVOICE_TAX_OR_NET_MISMATCH')
  expect(findings).not.toContain('PAYER_BUYER_MISMATCH')
  expect(findings).not.toContain('PAYEE_SELLER_MISMATCH')
  await expect(page.locator('.business-list-item').first()).toContainText('费用报销')
  await expect(page.locator('.detail-card h2')).toContainText('费用报销')
  const policyFinding = page.locator('.finding-item').filter({ hasText: '费用科目政策尚未确认' })
  await expect(policyFinding.locator('.attention-mark')).toHaveText('?')
  await expect(page.getByRole('button', { name: '生成凭证草稿' })).toBeDisabled()
})

test('4,448元费用报销补齐管理费用科目和政策后可生成凭证', async ({ page }) => {
  const invoice = await syntheticDocument(page, 'INVOICE TOTAL 4448.00')
  const bank = await syntheticDocument(page, 'BANK RECEIPT 4448.00')
  const proof = await syntheticDocument(page, 'PAYMENT PROOF 4448.00')
  await signIn(page, 'admin@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await uploadAndConfirm(page, 'policy-expense-invoice.png', invoice, 'VAT_INVOICE', {
    invoice_number: 'POLICY-EXPENSE-4448', issue_date: '2024-12-08', buyer: 'Buyer Ltd', seller: 'Seller Ltd',
    net_amount: '4196.23', tax_rate: '6%', tax_amount: '251.77', gross_amount: '4448.00',
  })
  await uploadAndConfirm(page, 'policy-expense-bank.png', bank, 'BANK_RECEIPT', {
    payer: 'Buyer Ltd', payee: 'Seller Ltd', settlement_date: '2024-12-08', gross_amount: '4448.00', settled: 'SETTLED',
  })
  await uploadAndConfirm(page, 'policy-expense-proof.png', proof, 'PAYMENT_PROOF', {
    payment_date: '2024-12-08', payer: 'Buyer Ltd', payee: 'Seller Ltd', purpose: '业务招待费',
    gross_amount: '4448.00', approved: 'true',
  })
  for (const filename of Object.keys({
    'policy-expense-invoice.png': 'INVOICE', 'policy-expense-bank.png': 'BANK_RECEIPT',
    'policy-expense-proof.png': 'PAYMENT_PROOF',
  })) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }
  await page.getByLabel('业务类型').selectOption('EXPENSE')
  const eventCreated = page.waitForResponse((response) =>
    response.url().endsWith('/api/v1/business-events') && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '确认关联并核对' }).click()
  const eventId = (await (await eventCreated).json()).id as string
  await expect(page.locator('.finding-list')).toContainText('费用科目政策尚未确认')

  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '连接与设置' }).click()
  await expect(page.getByRole('heading', { name: '账套设置', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '收起账套设置' }).click()
  await expect(page.getByRole('heading', { name: '账套科目与辅助核算' })).toBeHidden()
  await page.getByRole('button', { name: '展开账套设置' }).click()
  await page.getByRole('button', { name: /资产类/ }).click()
  await expect(page.getByRole('cell', { name: '1002', exact: true })).toBeVisible()
  await page.getByRole('button', { name: /资产类/ }).click()
  await expect(page.getByRole('cell', { name: '1002', exact: true })).toBeHidden()
  await page.getByLabel('新科目编码').fill('660201')
  await page.getByLabel('新科目名称').fill('管理费用—业务招待费')
  await page.getByRole('button', { name: '添加科目' }).click()
  await page.getByRole('button', { name: '保存科目配置' }).click()
  await expect(page.locator('.alert')).toContainText('已保存 6 个科目配置')
  const expensePolicy = page.locator('.policy-editor').filter({ hasText: '费用报销' })
  await expensePolicy.getByLabel('允许科目范围（可多选）').selectOption(['660201', '1002', '222101'])
  await expensePolicy.getByLabel('进项税处理').selectOption('DEDUCTIBLE')
  await expensePolicy.getByLabel('进项税科目范围（不可抵扣时禁止用于分录）').selectOption('222101')
  await expensePolicy.getByRole('checkbox').check()
  await expensePolicy.getByRole('button', { name: '保存费用报销政策' }).click()
  await expect(page.locator('.alert')).toContainText('费用报销政策已保存为 V1')

  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '业务中心' }).click()
  await page.locator('.business-list-item').filter({ hasText: eventId.slice(0, 8) }).click()
  const reconciliationResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/business-events/${eventId}/reconcile`) && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '重新运行核对' }).click()
  const reconciliation = await (await reconciliationResponse).json()
  expect(reconciliation.status).toBe('MATCHED')
  expect(reconciliation.rule_version).toBe('accounting-policy:EXPENSE:v1')
  const voucherResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/v1/business-events/${eventId}/voucher-drafts`) && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '生成凭证草稿' }).click()
  const voucher = await (await voucherResponse).json()
  expect(voucher.status).toBe('VALIDATED')
  expect(voucher.policy.policy_ref).toBe('accounting-policy:EXPENSE:v1')
  expect(voucher.debit_total).toBe('4448.00')
  expect(voucher.credit_total).toBe('4448.00')
  expect(voucher.lines[0].account_code).toBe('660201')
  await expect(page.locator('.voucher-detail .source-note')).toContainText('accounting-policy:EXPENSE:v1')
  await expect(page.locator('.voucher-detail .source-note')).toContainText('admin@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '财务工作台' }).click()
  await expect(page.locator('.recent-card')).not.toContainText(eventId.slice(0, 8))
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await expect(page.locator('article.document-row').filter({ hasText: 'policy-expense-invoice.png' })).toHaveCount(0)
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '业务档案' }).click()
  await page.getByLabel('搜索业务档案').fill('policy-expense-invoice.png')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(page.locator('.archive-list-item')).toHaveCount(1)
  await page.locator('.archive-list-item').click()
  await expect(page.locator('.archive-detail')).toContainText('policy-expense-invoice.png')
  await expect(page.locator('.archive-detail')).toContainText('policy-expense-bank.png')
  await expect(page.locator('.archive-detail')).toContainText('policy-expense-proof.png')
  await expect(page.locator('.archive-detail')).toContainText('660201')
  await expect(page.locator('.archive-detail')).toContainText('柠檬云未连接')
  await page.locator('.archive-document').first().getByText('查看 OCR 结果').click()
  await expect(page.locator('.archive-document').first()).toContainText('4448.00')
})

test('6,780元设备购置核对发票和回单并等待资本化政策', async ({ page }) => {
  test.skip(![privateAssetInvoice, privateAssetBankReceipt].every(existsSync), '本地6,780元设备购置金标材料未齐；文件不会被提交到 Git。')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  const invoiceName = basename(privateAssetInvoice)
  const bankName = basename(privateAssetBankReceipt)
  await uploadAndConfirm(page, invoiceName, await readFile(privateAssetInvoice), 'VAT_INVOICE', {
    invoice_number: '244234139350671047', issue_date: '2024-12-01', buyer: '南昌市杰盛科技有限公司',
    seller: '南昌市归鸿商贸有限公司', net_amount: '6000.00', tax_rate: '13%', tax_amount: '780.00', gross_amount: '6780.00',
  })
  await uploadAndConfirm(page, bankName, await readFile(privateAssetBankReceipt), 'BANK_RECEIPT', {
    payer: '南昌市杰盛科技有限公司', payee: '南昌市归鸿商贸有限公司', gross_amount: '6780.00', settlement_date: '2024-12-01',
  })
  for (const filename of [invoiceName, bankName]) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }
  await page.getByLabel('业务类型').selectOption('ASSET_PURCHASE')
  const reconciliationResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/business-events/') && response.url().endsWith('/reconcile')
      && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '确认关联并核对' }).click()
  const reconciliation = await (await reconciliationResponse).json()
  expect(reconciliation.rule_version).toBe('unconfigured-asset-v1')
  expect(reconciliation.status).toBe('NEEDS_REVIEW')
  const findings = reconciliation.findings.map((finding: { code: string }) => finding.code)
  expect(findings).toContain('ASSET_CAPITALIZATION_POLICY_REVIEW_REQUIRED')
  expect(findings).toContain('BANK_SETTLEMENT_NOT_CONFIRMED')
  expect(findings).not.toContain('GROSS_AMOUNT_MISMATCH')
  expect(findings).not.toContain('PAYER_BUYER_MISMATCH')
  expect(findings).not.toContain('PAYEE_SELLER_MISMATCH')
  await expect(page.locator('.business-list-item').first()).toContainText('设备购置')
  await expect(page.locator('.detail-card h2')).toContainText('设备购置')
  const policyFinding = page.locator('.finding-item').filter({ hasText: '固定资产资本化政策尚未确认' })
  await expect(policyFinding.locator('.attention-mark')).toHaveText('?')
  await expect(page.getByRole('button', { name: '生成凭证草稿' })).toBeDisabled()
})

test('21,800元办公室租赁核对发票和回单并等待租赁政策及合同', async ({ page }) => {
  test.skip(!existsSync(privateRentalInvoice), '本地21,800元办公室租赁金标发票未找到；文件不会被提交到 Git。')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()

  const invoiceName = basename(privateRentalInvoice)
  const bankName = '办公室租赁银行回单_合成测试.png'
  await uploadAndConfirm(page, invoiceName, await readFile(privateRentalInvoice), 'VAT_INVOICE', {
    invoice_number: '244278928656838066', issue_date: '2024-12-01',
    buyer: '南昌市杰盛科技有限公司', seller: '南昌市中智集团地产有限公司',
    net_amount: '20000.00', tax_rate: '9%', tax_amount: '1800.00', gross_amount: '21800.00',
  })
  await uploadAndConfirm(page, bankName, await syntheticDocument(page, 'BANK RECEIPT 21800.00'), 'BANK_RECEIPT', {
    payer: '南昌市杰盛科技有限公司', payee: '南昌市中智集团地产有限公司',
    settlement_date: '2024-12-01', gross_amount: '21800.00',
  })
  for (const filename of [invoiceName, bankName]) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }
  await page.getByLabel('业务类型').selectOption('RENTAL')
  await expect(page.locator('.rental-confirmation-hint')).toContainText('请先在设置中确认租赁直接费用政策')
  const reconciliationResponse = page.waitForResponse((response) =>
    response.url().includes('/api/v1/business-events/') && response.url().endsWith('/reconcile')
      && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '确认关联并核对' }).click()
  const reconciliation = await (await reconciliationResponse).json()
  expect(reconciliation.rule_version).toBe('unconfigured-rental-v1')
  expect(reconciliation.status).toBe('NEEDS_REVIEW')
  const findings = reconciliation.findings.map((finding: { code: string }) => finding.code)
  expect(findings).toContain('RENTAL_ACCOUNT_POLICY_REVIEW_REQUIRED')
  expect(findings).toContain('RENTAL_CONTRACT_MISSING')
  expect(findings).toContain('BANK_SETTLEMENT_NOT_CONFIRMED')
  expect(findings).not.toContain('GROSS_AMOUNT_MISMATCH')
  expect(findings).not.toContain('PAYER_BUYER_MISMATCH')
  expect(findings).not.toContain('PAYEE_SELLER_MISMATCH')
  await expect(page.locator('.business-list-item').first()).toContainText('租赁业务')
  await expect(page.locator('.detail-card h2')).toContainText('租赁业务')
  const policyFinding = page.locator('.finding-item').filter({ hasText: '租赁会计政策尚未确认' })
  await expect(policyFinding.locator('.attention-mark')).toHaveText('?')
  await expect(page.getByRole('button', { name: '生成凭证草稿' })).toBeDisabled()
})

test('管理员可以维护账套科目和必填辅助核算维度', async ({ page }) => {
  await signIn(page, 'admin@playwright.example.test')
  const navigation = page.getByRole('navigation', { name: '主导航' })
  await navigation.getByRole('button', { name: '连接与设置' }).click()
  await expect(page.getByRole('heading', { name: '账套科目与辅助核算' })).toBeVisible()

  await page.getByLabel('新科目编码').fill('6602')
  await page.getByLabel('新科目名称').fill('销售费用')
  await page.getByRole('button', { name: '添加科目' }).click()
  const row = page.getByRole('row').filter({ has: page.getByRole('cell', { name: '6602', exact: true }) })
  await expect(row).toBeVisible()
  await row.getByLabel('6602 辅助核算维度').fill('部门、员工')
  await page.getByRole('button', { name: '保存科目配置' }).click()
  await expect(page.locator('.alert')).toContainText(/已保存 \d+ 个科目配置/)
  await expect(row.getByLabel('6602 辅助核算维度')).toHaveValue('部门、员工')

  await page.getByLabel('新科目编码').fill('9998')
  await page.getByLabel('新科目名称').fill('待删除测试科目')
  await page.getByRole('button', { name: '添加科目' }).click()
  await page.getByRole('button', { name: '保存科目配置' }).click()
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '删除科目 9998' }).click()
  await expect(page.locator('tr').filter({ hasText: '9998' })).toHaveCount(0)
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.locator('tr').filter({ hasText: '9998' })).toHaveCount(0)

  const assetPolicy = page.locator('.policy-editor').filter({ hasText: '设备购置' })
  await assetPolicy.getByLabel('允许科目范围（可多选）').selectOption(['1601', '1002', '222101'])
  await assetPolicy.getByLabel('进项税处理').selectOption('DEDUCTIBLE')
  await assetPolicy.getByLabel('进项税科目范围（不可抵扣时禁止用于分录）').selectOption('222101')
  await assetPolicy.getByRole('checkbox').check()
  await assetPolicy.getByRole('button', { name: '保存设备购置政策' }).click()
  await expect(page.locator('.alert')).toContainText('设备购置政策已保存为 V1')
})

test('发票与入库单金额冲突时显示待复核并阻止制证', async ({ page }) => {
  const invoiceImage = await syntheticDocument(page, 'INVOICE CONFLICT 45200.00')
  const receiptImage = await syntheticDocument(page, 'GOODS RECEIPT TOTAL 44000.00')
  const bankImage = await syntheticDocument(page, 'BANK RECEIPT TOTAL 45200.00')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()

  await uploadAndConfirm(page, 'conflict-invoice.png', invoiceImage, 'VAT_INVOICE', {
    invoice_number: '24360125413017362004', issue_date: '2024-12-01', buyer: '南昌市杰盛科技有限公司',
    seller: '南昌市视维科技公司', net_amount: '40000.00', tax_rate: '13%', tax_amount: '5200.00', gross_amount: '45200.00',
  })
  await uploadAndConfirm(page, 'conflict-goods-receipt.png', receiptImage, 'GOODS_RECEIPT', {
    receipt_number: 'NO56784002', receipt_date: '2024-12-01', item: '教学专用VR眼镜', quantity: '100',
    gross_amount: '44000.00', received: 'true',
  })
  await uploadAndConfirm(page, 'conflict-bank-receipt.png', bankImage, 'BANK_RECEIPT', {
    payer: '南昌市杰盛科技有限公司', payee: '南昌市视维科技公司', settlement_date: '2024-12-01',
    gross_amount: '45200.00', settled: 'SETTLED',
  })

  const rolesByFile: Record<string, string> = {
    'conflict-invoice.png': 'INVOICE',
    'conflict-goods-receipt.png': 'GOODS_RECEIPT',
    'conflict-bank-receipt.png': 'BANK_RECEIPT',
  }
  for (const filename of Object.keys(rolesByFile)) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }
  const createEventResponse = page.waitForResponse((response) =>
    response.url().endsWith('/api/v1/business-events') && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: '确认关联并核对' }).click()
  const createdEvent = await (await createEventResponse).json()
  await expect(page.locator('.alert')).toContainText('核对完成：待复核')
  await expect(page.locator('.detail-card .status-pill')).toHaveText('待复核')
  await expect(page.locator('.finding-list')).toContainText('发票、入库单与回单金额不一致')
  await expect(page.getByRole('button', { name: '生成凭证草稿' })).toBeDisabled()

  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '核对与异常' }).click()
  await expect(page.locator('.case-list')).toContainText('业务核对异常')
  const caseRow = page.locator('.case-row').filter({ hasText: '发票、入库单与回单金额不一致' })
  await caseRow.getByRole('button', { name: '查看详情' }).click()
  await expect(page.locator('.case-detail')).toContainText('发票、入库单与回单金额不一致')
  await expect(page.locator('.case-detail')).toContainText('conflict-invoice.png')
  await expect(page.locator('.case-detail .audit-timeline')).toContainText('核对')
  await page.locator('.case-detail').getByRole('button', { name: '返回关联业务处理' }).click()
  await expect(page.locator('.detail-card')).toBeVisible()
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '业务中心' }).click()
  const businessRow = page.locator('.business-list-item').filter({ hasText: createdEvent.id.slice(0, 8) })
  await expect(businessRow).toContainText('待复核')
})

test('缺少银行回单时进入待复核并说明缺失单据', async ({ page }) => {
  const invoiceImage = await syntheticDocument(page, 'MISSING BANK INVOICE 45200.00')
  const receiptImage = await syntheticDocument(page, 'MISSING BANK GOODS RECEIPT 45200.00')
  const proofImage = await syntheticDocument(page, 'MISSING BANK PAYMENT PROOF 45200.00')
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()

  await uploadAndConfirm(page, 'missing-bank-invoice.png', invoiceImage, 'VAT_INVOICE', {
    invoice_number: '24360125413017362005', issue_date: '2024-12-01', buyer: '南昌市杰盛科技有限公司',
    seller: '南昌市视维科技公司', net_amount: '40000.00', tax_rate: '13%', tax_amount: '5200.00', gross_amount: '45200.00',
  })
  await uploadAndConfirm(page, 'missing-bank-goods-receipt.png', receiptImage, 'GOODS_RECEIPT', {
    receipt_number: 'NO56784003', receipt_date: '2024-12-01', item: '教学专用VR眼镜', quantity: '100',
    gross_amount: '45200.00', received: 'true',
  })
  await uploadAndConfirm(page, 'missing-bank-payment-proof.png', proofImage, 'PAYMENT_PROOF', {
    proof_number: 'PAY-2024-12-001', payment_date: '2024-12-01', payer: '南昌市杰盛科技有限公司',
    payee: '南昌市视维科技公司', gross_amount: '45200.00', approved: 'true',
  })
  for (const filename of Object.keys({
    'missing-bank-invoice.png': 'INVOICE',
    'missing-bank-goods-receipt.png': 'GOODS_RECEIPT',
    'missing-bank-payment-proof.png': 'PAYMENT_PROOF',
  })) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }

  await page.getByRole('button', { name: '确认关联并核对' }).click()
  await expect(page.locator('.alert')).toContainText('核对完成：待复核')
  await expect(page.locator('.finding-list')).toContainText('缺少银行回单')
  await expect(page.getByRole('button', { name: '生成凭证草稿' })).toBeDisabled()
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '核对与异常' }).click()
  await expect(page.locator('.case-list')).toContainText('业务核对异常')
})

test('多页 PDF 原件可以在页面里逐页浏览', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const pdf = await syntheticTwoPagePdf(page)
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()
  await page.locator('input[type="file"]').setInputFiles({
    name: 'two-page-evidence.pdf', mimeType: 'application/pdf', buffer: pdf,
  })
  const row = page.locator('article.document-row').filter({ hasText: 'two-page-evidence.pdf' })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await page.route(/\/api\/v1\/documents\/[^/]+$/, async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    body.extraction.fields.gross_amount = {
      ...body.extraction.fields.gross_amount,
      page: 1,
      bbox: [72, 72, 180, 96],
      coordinate_space: 'pdf_points',
      page_size_points: [612, 792],
    }
    await route.fulfill({ response, json: body })
  })
  await row.getByRole('button', { name: '查看并确认字段' }).click()
  const editor = page.locator('.field-editor').last()
  const grossAmount = editor.locator('.field-grid label').filter({ hasText: '价税合计' })
  await grossAmount.click()
  const preview = editor.locator('.source-preview-panel')
  await expect(preview).toContainText('第 1 / 2 页')
  await expect(preview.locator('.source-evidence-box')).toBeVisible()
  await editor.getByRole('button', { name: '浏览原件' }).click()
  await expect(preview).toContainText('第 1 / 2 页')
  await preview.getByRole('button', { name: '下一页' }).click()
  await expect(preview).toContainText('第 2 / 2 页')
  await expectMobileLayout(page)
  await expect(preview.locator('img')).toHaveAttribute('alt', '单据第 2 页')
  await expect(preview.locator('.source-evidence-box')).toHaveCount(0)
  await preview.getByRole('button', { name: '上一页' }).click()
  await expect(preview).toContainText('第 1 / 2 页')
})

test('合同订购数量多于入库数量时进入待复核并禁止生成凭证', async ({ page }) => {
  await signIn(page, 'accountant@playwright.example.test')
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '单据中心' }).click()

  await uploadAndConfirm(page, 'shortfall-contract.png', await syntheticDocument(page, 'CONTRACT ORDER 100'), 'CONTRACT', {
    contract_number: 'CON-100', item: '教学专用VR眼镜', ordered_quantity: '100',
  })
  await uploadAndConfirm(page, 'shortfall-invoice.png', await syntheticDocument(page, 'SHORTFALL INVOICE TOTAL 45200.00'), 'VAT_INVOICE', {
    invoice_number: 'INV-SHORTFALL-01', issue_date: '2024-12-01', buyer: 'Buyer Ltd', seller: 'Seller Ltd',
    net_amount: '40000.00', tax_rate: '13%', tax_amount: '5200.00', gross_amount: '45200.00',
  })
  await uploadAndConfirm(page, 'shortfall-receipt.png', await syntheticDocument(page, 'SHORTFALL GOODS RECEIVED 90'), 'GOODS_RECEIPT', {
    receipt_number: 'GR-SHORTFALL-01', receipt_date: '2024-12-01', item: '教学专用VR眼镜', quantity: '90',
    gross_amount: '45200.00', received: 'true',
  })
  await uploadAndConfirm(page, 'shortfall-bank.png', await syntheticDocument(page, 'SHORTFALL BANK RECEIPT 45200.00'), 'BANK_RECEIPT', {
    payer: 'Buyer Ltd', payee: 'Seller Ltd', settlement_date: '2024-12-01', gross_amount: '45200.00', settled: 'SETTLED',
  })

  const rolesByFile: Record<string, string> = {
    'shortfall-contract.png': 'CONTRACT',
    'shortfall-invoice.png': 'INVOICE',
    'shortfall-receipt.png': 'GOODS_RECEIPT',
    'shortfall-bank.png': 'BANK_RECEIPT',
  }
  for (const filename of Object.keys(rolesByFile)) {
    const row = page.locator('article.document-row').filter({ hasText: filename })
    await row.locator('input[type="checkbox"]').check()
  }
  await page.getByRole('button', { name: '确认关联并核对' }).click()

  await expect(page.locator('.alert')).toContainText('核对完成：待复核')
  await expect(page.locator('.evidence-timeline')).toContainText('合同')
  await expect(page.locator('.finding-list')).toContainText('实际收货少于合同数量（90/100）')
  await expect(page.getByRole('button', { name: '生成凭证草稿' })).toBeDisabled()
})
