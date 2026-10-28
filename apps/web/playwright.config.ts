import { defineConfig, devices } from '@playwright/test'
import { createServer, type AddressInfo } from 'node:net'

async function availablePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const port = (server.address() as AddressInfo).port
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  return port
}

const apiPort = process.env.E2E_API_PORT ? Number(process.env.E2E_API_PORT) : await availablePort()
let webPort = process.env.E2E_WEB_PORT ? Number(process.env.E2E_WEB_PORT) : await availablePort()
while (webPort === apiPort) webPort = await availablePort()
process.env.E2E_API_PORT = String(apiPort)
process.env.E2E_WEB_PORT = String(webPort)

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 240_000,
  expect: { timeout: 15_000 },
  reporter: 'list',
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'services/api/.venv/bin/python tests/e2e/serve_api.py',
      cwd: '../..',
      url: `http://127.0.0.1:${apiPort}/health`,
      env: { E2E_API_PORT: String(apiPort), PYTHONPATH: 'services/api:packages' },
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      cwd: '.',
      url: `http://127.0.0.1:${webPort}`,
      env: { VITE_API_PROXY_TARGET: `http://127.0.0.1:${apiPort}` },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
})
