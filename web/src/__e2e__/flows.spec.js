import { expect, test } from '@playwright/test';

test.describe('药妆智多星 E2E 完整业务链路测试', () => {
  test.beforeEach(async ({ page }) => {
    page.on('console', (msg) => console.log('PAGE LOG:', msg.text()));
    // biome-ignore lint/suspicious/noConsole: E2E debug logs
    page.on('pageerror', (err) => console.error('PAGE ERROR:', err.message));

    // Catch-all mock for /api/ to prevent hanging/refused connections on unhandled requests.
    // Registering this first ensures that test-specific routes (registered later in each test) take precedence.
    await page.route('**/api/**', async (route) => {
      if (route.request().url().includes('/src/api/')) {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{}',
      });
    });
  });

  // ── 链路 1: 登录认证流量测试 ──
  test('用户登录与凭证保存', async ({ page }) => {
    // 拦截并模拟登录 API
    await page.route('**/api/auth/login', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          token: 'mock-jwt-token',
          refresh_token: 'mock-refresh-token',
          user_id: 1,
          username: 'admin',
          display_name: '管理员',
          role: 'admin',
        }),
      });
    });

    await page.goto('/login.html');

    // 验证表单元素
    await expect(page.locator('#username')).toBeVisible();
    await expect(page.locator('#password')).toBeVisible();

    // 模拟填充
    await page.fill('#username', 'admin');
    await page.fill('#password', 'admin123');

    // 点击提交
    await page.click('#submitBtn');

    // 等待本地存储并校验 token
    await page.waitForTimeout(500);
    const token = await page.evaluate(() => localStorage.getItem('token'));
    expect(token).toBe('mock-jwt-token');
  });

  // ── 链路 2: 智能客服对话链路测试 ──
  test('AI 对话与消息发送渲染', async ({ page }) => {
    // 初始化 localStorage 预置 Token
    await page.goto('/login.html');
    await page.evaluate(() => {
      localStorage.setItem('token', 'mock-jwt-token');
      localStorage.setItem('user', JSON.stringify({ username: 'admin', role: 'admin' }));
      localStorage.removeItem('currentSessionId');
      localStorage.removeItem('currentSessionToken');
    });

    // 拦截用户信息与初始会话
    await page.route('**/api/auth/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          user_id: 1,
          username: 'admin',
          display_name: '管理员',
          role: 'admin',
        }),
      });
    });

    await page.route('**/api/sessions', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ sessions: [] }),
      });
    });

    await page.goto('/');

    // 验证聊天输入框与欢迎语
    await expect(page.locator('.welcome-title')).toBeVisible();
    await expect(page.locator('#chatInput')).toBeVisible();

    // 拦截对话 API
    await page.route('**/api/chat', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          query: '你好',
          response: '您好！我是您的智能客服小助手，请问有什么可以帮您？',
          session_id: 'mock-session-id',
        }),
      });
    });

    // 发送消息
    await page.fill('#chatInput', '你好');
    await page.keyboard.press('Enter');

    // 验证用户消息已正确上屏渲染
    const userMsg = page.locator('.message.user .message-bubble');
    await expect(userMsg).toContainText('你好');
  });

  // ── 链路 3: 管理后台指标展示测试 ──
  test('管理后台 Dashboard 指标渲染与加载', async ({ page }) => {
    // 初始化 localStorage 预置 Token
    await page.goto('/login.html');
    await page.evaluate(() => {
      localStorage.setItem('token', 'mock-jwt-token');
      localStorage.setItem('user', JSON.stringify({ username: 'admin', role: 'admin' }));
      localStorage.removeItem('currentSessionId');
      localStorage.removeItem('currentSessionToken');
    });

    // 拦截后台各控制端点 API 响应
    await page.route('**/api/auth/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          user_id: 1,
          username: 'admin',
          display_name: '管理员',
          role: 'admin',
        }),
      });
    });

    await page.route('**/api/metrics', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          metrics: {
            total_requests: 1240,
            error_rate: 1.2,
            avg_response_time: 1.5,
            p95_response_time: 3.2,
            cache_hit_rate: 82.5,
            agent_call_counts: {
              product_agent: 15,
              tech_agent: 8,
            },
            mode_counts: {
              sequential: 18,
              parallel: 5,
            },
            sla: {
              violations_slow: 4,
              violation_rate: 3.2,
              target_min: 5,
              target_max: 20,
            },
          },
        }),
      });
    });

    await page.route('**/api/kpi', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          kpi: {
            first_resolution_rate: '85.2%',
            total_single_turn_resolved: 230,
            total_multi_turn: 45,
            ai_handled_rate: '78.1%',
            total_ai_handled: 250,
            total_escalated: 60,
          },
        }),
      });
    });

    await page.route('**/api/sessions', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          sessions: [
            {
              session_id: 'sess-123456789',
              message_count: 5,
              drift_count: 0,
              last_activity: '2026-06-10T18:00:00Z',
              drift_escalation: false,
            },
          ],
        }),
      });
    });

    await page.route('**/api/alerts?limit=20', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          alerts: [
            {
              message: '响应超时告警',
              timestamp: '2026-06-10T18:10:00Z',
              severity: 'warning',
              window_rate: 12,
            },
          ],
        }),
      });
    });

    await page.route('**/api/cache/stats', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          hit_rate: '82.5',
          l1_hits: 800,
          l2_hits: 200,
          l1_size: 1024,
          l2_size: 4096,
        }),
      });
    });

    await page.route('**/api/auth/users', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ users: [] }),
      });
    });

    await page.route('**/api/knowledge/stats', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ available: true, collections: {}, total: 0 }),
      });
    });

    await page.route('**/api/alerts/config', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ webhooks: [], email_enabled: false, email_to: [] }),
      });
    });

    await page.route('**/api/auth/audit?limit=30', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ logs: [] }),
      });
    });

    await page.route('**/api/admin/prompts/agents', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ agents: [] }),
      });
    });

    await page.goto('/admin.html');

    // 验证 SLA 达标率文字渲染是否正确（100% - 3.2% = 96.8%）
    await expect(page.locator('#slaValue')).toContainText('96.8%');

    // 验证 缓存命中率文字渲染是否正确（82.5%）
    await expect(page.locator('#cacheValue')).toContainText('82.5%');

    // 验证首次解决率与 AI 处理率渲染（85.2% & 78.1%）
    await expect(page.locator('#resolutionValue')).toContainText('85.2%');
    await expect(page.locator('#aiValue')).toContainText('78.1%');
  });
});
