import { expect, test, type Page } from '@playwright/test'

const password = 'Playwright-test-password-2026!'

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

async function signIn(page: Page) {
  const response = await page.request.post('/api/v1/auth/login', {
    data: { email: 'accountant@playwright.example.test', password },
  })
  expect(response.ok()).toBeTruthy()
  const { access_token } = await response.json()
  await page.addInitScript((value: string) => sessionStorage.setItem('zq-access-token', value), access_token)
  await page.goto('/')
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()
}

test('新上传且未关联业务的票据出现在待归类列表并显示识别状态', async ({ page }) => {
  await signIn(page)
  await page.getByRole('button', { name: '单据中心' }).click()

  const filename = 'later-random-ticket.png'
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: 'image/png',
    buffer: await syntheticDocument(page, 'RANDOM RECEIPT 88.00'),
  })

  const row = page.locator('article.document-row').filter({ hasText: filename })
  await expect(row).toBeVisible({ timeout: 60_000 })
  await expect(row.getByTestId('document-association-status')).toHaveText('归类状态：待归类')
  await expect(row.getByTestId('document-recognition-status')).toContainText('识别状态：')

  await page.getByRole('button', { name: /待归类/ }).click()
  await expect(row).toBeVisible()
})
