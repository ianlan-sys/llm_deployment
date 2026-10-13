import { defineConfig, devices } from '@playwright/test'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

const edgeExecutable = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const browserChannel = process.platform === 'win32' && existsSync(edgeExecutable) ? 'msedge' : undefined
const backendPython = path.resolve(
  '../backend/.venv/Scripts/python.exe',
)
if (!existsSync(backendPython)) {
  throw new Error(
    `Backend test environment is missing: ${backendPython}`,
  )
}

const inheritedPortBase = process.env.TASK13_E2E_PORT_BASE
const portBase = inheritedPortBase
  ? Number(inheritedPortBase)
  : 20_000 + (process.pid % 8_000)
if (!Number.isInteger(portBase) || portBase < 20_000 || portBase > 27_999) {
  throw new Error(`Invalid Task 13 Playwright port base: ${portBase}`)
}
if (!inheritedPortBase) {
  process.env.TASK13_E2E_PORT_BASE = String(portBase)
}
const fakeOllamaPort = portBase
const backendPort = portBase + 1
const frontendPort = portBase + 2
if (!inheritedPortBase) {
  const portCheck = spawnSync(
    backendPython,
    [
      '-c',
      [
        'import socket',
        `ports = [${fakeOllamaPort}, ${backendPort}, ${frontendPort}]`,
        'sockets = []',
        'try:',
        '    for port in ports:',
        '        sock = socket.socket()',
        '        sock.bind(("127.0.0.1", port))',
        '        sockets.append(sock)',
        'finally:',
        '    for sock in sockets:',
        '        sock.close()',
      ].join('\n'),
    ],
    { stdio: 'pipe' },
  )
  if (portCheck.status !== 0) {
    throw new Error(
      `Task 13 Playwright ports are unavailable: ${portCheck.stderr.toString()}`,
    )
  }
}

const backendOrigin = `http://127.0.0.1:${backendPort}`
const frontendOrigin = `http://127.0.0.1:${frontendPort}`
const fakeOllamaOrigin = `http://127.0.0.1:${fakeOllamaPort}`
const e2eRuntimeDir = path.resolve(
  '../.superpowers/sdd/tmp',
  `task13-e2e-${process.pid}-${Date.now()}`,
)
const passwordHash = 'scrypt$16384$8$1$00112233445566778899aabbccddeeff$1db1ace05728bec5a6de329c491c0432a5f4d603e70abec029cb17d9f641b908'

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  use: {
    baseURL: frontendOrigin,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: [
    {
      command: `"${backendPython}" -m uvicorn fake_ollama:app --host 127.0.0.1 --port ${fakeOllamaPort}`,
      cwd: './e2e/fixtures',
      url: `${fakeOllamaOrigin}/api/version`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `"${backendPython}" -m uvicorn app.main:app --host 127.0.0.1 --port ${backendPort}`,
      cwd: '../backend',
      url: `${backendOrigin}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...process.env,
        DEMO_RUNTIME_DIR: e2eRuntimeDir,
        DEMO_AUTH_REQUIRED: 'true',
        DEMO_AUTH_USERNAME: 'task13-demo',
        DEMO_AUTH_PASSWORD_HASH: passwordHash,
        DEMO_SESSION_SECRET: 'task13-playwright-session-secret',
        DEMO_OLLAMA_BASE_URL: fakeOllamaOrigin,
        DEMO_OLLAMA_MODEL: 'qwen3:1.7b-q4_K_M',
        DEMO_OLLAMA_TIMEOUT_SECONDS: '2',
        DEMO_PROVIDER_MODE: 'demo',
        DEMO_OPENAI_BASE_URL: '',
        DEMO_OPENAI_MODEL: '',
        DEMO_OPENAI_API_KEY: '',
      },
    },
    {
      command: `corepack pnpm exec vite --host 127.0.0.1 --port ${frontendPort}`,
      cwd: '.',
      url: `${frontendOrigin}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...process.env,
        DEMO_BACKEND_PROXY_TARGET: backendOrigin,
      },
    },
  ],
  projects: [{
    name: 'chromium',
    use: { ...devices['Desktop Chrome'], ...(browserChannel ? { channel: browserChannel } : {}) },
  }],
})
