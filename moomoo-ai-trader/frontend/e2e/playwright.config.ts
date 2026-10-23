import { defineConfig } from '@playwright/test';

/**
 * 端到端冒烟测试：针对「后端在 8710 端口同源提供的已构建前端」运行。
 * 运行前需要：后端（uvicorn）与 sim 后台工作进程已启动，且使用一个全新的 DATA_DIR。
 *   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers npm run e2e
 * 可选环境变量：E2E_BASE_URL、E2E_PASSWORD、E2E_SCREENSHOTS、E2E_CHROMIUM（自定义 chromium 可执行文件）。
 */
export default defineConfig({
  testDir: '.',
  testMatch: /.*\.spec\.ts$/,
  timeout: 300_000,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8710',
    viewport: { width: 1366, height: 900 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Kuala_Lumpur',
    launchOptions: process.env.E2E_CHROMIUM ? { executablePath: process.env.E2E_CHROMIUM } : {},
  },
});
