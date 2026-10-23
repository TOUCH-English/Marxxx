import { expect, test, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const PASSWORD = process.env.E2E_PASSWORD ?? 'e2e-pass-12345';
const SHOTS = process.env.E2E_SCREENSHOTS ?? '/tmp/claude-0/fe-screens';

async function shot(page: Page, name: string) {
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

async function overviewJson(page: Page) {
  const r = await page.request.get('/api/sim/overview');
  expect(r.ok()).toBeTruthy();
  return r.json();
}

test('冒烟：设置密码 → 登录 → 向导启动 DEMO → 浏览全部页面 → 暂停新开仓', async ({ page }) => {
  mkdirSync(SHOTS, { recursive: true });
  const problems: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error') problems.push(`console.error: ${m.text()}`);
  });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));

  // 1. 首次设置密码（全新数据目录）
  await page.goto('/');
  await expect(page.getByText('首次使用：设置本机网页密码')).toBeVisible();
  await expect(page.getByText('请不要在这里输入 moomoo 账号密码或验证码')).toBeVisible();
  await page.getByLabel('新密码（至少 8 位）').fill(PASSWORD);
  await page.getByLabel('再次输入新密码').fill(PASSWORD);
  await page.getByRole('button', { name: '设置密码并进入' }).click();
  await expect(page.getByTestId('status-bar')).toBeVisible();

  // 2. 退出后用密码登录
  await page.getByTestId('logout').click();
  await page.locator('.ant-modal-confirm').getByRole('button', { name: '退出登录' }).click();
  await expect(page.locator('.ant-modal-confirm')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible();
  await page.getByLabel('本机网页密码').fill(PASSWORD);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByTestId('status-bar')).toBeVisible();
  await expect(page.getByTestId('status-bar')).toContainText('尚未启动运行');
  await shot(page, '01-overview-empty');

  // 3. 启动向导：DEMO，默认资金/亏损/股票池
  await page.getByTestId('go-wizard').click();
  await expect(page.getByTestId('wizard')).toBeVisible();
  await page.getByTestId('mode-DEMO').click();
  for (let i = 0; i < 3; i++) await page.getByTestId('wizard-next').click();
  await expect(page.getByText('按当前数据预览')).toBeVisible();
  await expect(page.getByText('成员（')).toBeVisible({ timeout: 20_000 });
  await shot(page, '02-wizard-pool');
  await page.getByTestId('wizard-next').click();
  await page.getByTestId('wizard-next').click();
  const summary = page.getByTestId('auth-summary');
  await expect(summary).toContainText('模式：演示', { timeout: 20_000 });
  await expect(page.getByText('停止线（可承受亏损）不保证最大实际亏损')).toBeVisible();
  await shot(page, '03-wizard-confirm');
  await page.getByTestId('wizard-agree').check();
  await page.getByTestId('wizard-start').click();
  await expect(page).toHaveURL(/\/overview$/);
  await expect(page.getByTestId('run-state')).toContainText('运行中');

  // 4. 等待后台工作进程产生决策/持仓（演示回放每次推进 30 分钟）
  await expect
    .poll(
      async () => {
        const o = await overviewJson(page);
        return o.positions.length + Object.values((o.today?.decisions ?? {}) as Record<string, number>).reduce((a, b) => a + b, 0);
      },
      { timeout: 150_000, intervals: [2000] },
    )
    .toBeGreaterThan(0);
  await expect
    .poll(
      async () => {
        const rows = await page.getByTestId('positions-card').locator('tr.ant-table-row').count();
        const today = (await page.getByTestId('today-card').innerText()) ?? '';
        return rows > 0 || !today.includes('今天还没有决策');
      },
      { timeout: 30_000 },
    )
    .toBe(true);
  await shot(page, '04-overview-running');
  // 成绩单：关卡 A/B 与大盘比较（演示没有 SPY 数据，应显示「样本不足」而不是通过）
  await expect(page.getByTestId('scorecard-card')).toBeVisible();
  await expect(page.getByTestId('scorecard-card')).toContainText('关卡 B');

  // 5. 浏览全部页面
  const pages: [string, string, string][] = [
    ['启动向导', '启动向导', '05-wizard'],
    ['连接与权限诊断', '连接与权限诊断', '06-connection'],
    ['自选股与股票详情', '自选股与股票详情', '07-instruments'],
    ['AI 分析与决策', 'AI 分析与决策', '08-decisions'],
    ['策略与风控设置', '策略与风控设置', '09-risk'],
    ['订单与持仓', '订单与持仓', '10-orders'],
    ['模拟、回测和复盘', '模拟、回测和复盘', '11-sim'],
    ['复盘与改进', '复盘与改进', '12-learning'],
    ['日志与设置', '日志与设置', '13-logs'],
  ];
  for (const [menu, heading, name] of pages) {
    await page.locator('.ant-menu').getByText(menu, { exact: true }).click();
    await expect(page.locator('h3').filter({ hasText: heading }).first()).toBeVisible();
    await page.waitForTimeout(1200);
    await shot(page, name);
  }

  // 连接诊断：只读检测（OpenD 未运行时应给出中文原因与登录步骤）
  await page.locator('.ant-menu').getByText('连接与权限诊断', { exact: true }).click();
  await page.getByTestId('diagnose-btn').click();
  await expect(page.getByText('OpenD 连接').first()).toBeVisible();
  await expect(page.getByTestId('gate-card')).toContainText('部署开关');

  // 股票详情
  await page.goto('/instruments/MY.D002');
  await expect(page.getByText('证券资料（请核对名称与类型）')).toBeVisible();
  await expect(page.locator('.recharts-surface').first()).toBeVisible({ timeout: 20_000 });
  await shot(page, '14-instrument-detail');

  // 决策详情抽屉：事实 / 规则计算 / 模型判断 / 证据 分开显示
  await page.locator('.ant-menu').getByText('AI 分析与决策', { exact: true }).click();
  await page.getByText('包含例行「无需交易」').click();
  const firstCard = page.getByTestId('decision-card').first();
  await expect(firstCard).toBeVisible({ timeout: 30_000 });
  await firstCard.click();
  const drawer = page.locator('.ant-drawer').filter({ hasText: '决策详情' });
  await expect(drawer.getByText('① 事实（当时可见的数据）')).toBeVisible();
  await expect(drawer.getByText('③ 模型判断（模型自报判断，不是获胜概率）')).toBeVisible();
  await shot(page, '15-decision-drawer');
  await page.keyboard.press('Escape');

  // 回测
  await page.locator('.ant-menu').getByText('模拟、回测和复盘', { exact: true }).click();
  await page.getByTestId('run-backtest').click();
  await expect(page.getByTestId('backtest-detail')).toContainText('模拟报告', { timeout: 60_000 });
  await shot(page, '16-backtest');

  // 6. 暂停新开仓：确认后后端状态改变；再恢复
  await page.locator('.ant-menu').getByText('总览', { exact: true }).click();
  await expect(page.getByTestId('pause-btn')).toHaveText(/暂停新开仓/);
  await page.getByTestId('pause-btn').click();
  await page.locator('.ant-modal-confirm').getByRole('button', { name: '暂停新开仓' }).click();
  await expect.poll(async () => (await overviewJson(page)).portfolio.pause_new_entries, { timeout: 15_000 }).toBe(true);
  await expect(page.getByTestId('run-state')).toContainText('已暂停新开仓');
  await expect(page.getByTestId('allow-card')).toContainText('已暂停新开仓');
  await shot(page, '17-overview-paused');
  await page.getByTestId('pause-btn').click();
  await page.locator('.ant-modal-confirm').getByRole('button', { name: '恢复新开仓' }).click();
  await expect.poll(async () => (await overviewJson(page)).portfolio.pause_new_entries, { timeout: 15_000 }).toBe(false);

  // 7. 切换到「真实自动交易」：显著区分 + 部署关闭提示
  await page.getByTestId('ns-switch').getByText('真实自动交易').click();
  await expect(page.getByText('真实交易在部署配置中关闭（默认）')).toBeVisible();
  await expect(page.getByText('尚未启动真实自动交易')).toBeVisible();
  await shot(page, '18-live-overview');
  await page.getByTestId('ns-switch').getByText('自动模拟').click();
  await expect(page.getByTestId('run-state')).toBeVisible();

  expect(problems, problems.join('\n')).toEqual([]);
});
