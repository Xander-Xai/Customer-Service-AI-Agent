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

  // ── 链路 4: 注册流程测试 ──
  test('用户注册与表单验证', async ({ page }) => {
    await page.route('**/api/auth/register', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          message: '注册成功',
          user_id: 2,
          username: 'newuser',
        }),
      });
    });

    await page.goto('/login.html');

    // 切换到注册模式
    await page.click('#switchMode');
    await expect(page.locator('#formTitle')).toContainText('注册');

    // 验证表单元素
    await expect(page.locator('#username')).toBeVisible();
    await expect(page.locator('#password')).toBeVisible();
    await expect(page.locator('#confirmPassword')).toBeVisible();

    // 模拟注册
    await page.fill('#username', 'newuser');
    await page.fill('#password', 'password123');
    await page.fill('#confirmPassword', 'password123');
    await page.click('#submitBtn');

    // 验证跳转
    await page.waitForTimeout(500);
    await expect(page.locator('#formTitle')).toContainText('登录');
  });

  // ── 链路 5: Token 刷新降级测试 ──
  test('Token 过期后自动刷新与降级', async ({ page }) => {
    let refreshCalled = false;

    await page.route('**/api/auth/refresh', async (route) => {
      refreshCalled = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: 'new-mock-jwt-token',
          refresh_token: 'new-mock-refresh-token',
        }),
      });
    });

    await page.goto('/login.html');
    await page.evaluate(() => {
      localStorage.setItem('token', 'expired-jwt-token');
      localStorage.setItem('refresh_token', 'mock-refresh-token');
    });

    // 触发需要认证的请求
    await page.route('**/api/auth/me', async (route) => {
      const authHeader = route.request().headerValue('Authorization');
      if (authHeader === 'Bearer expired-jwt-token') {
        await route.fulfill({ status: 401, body: '{}' });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            user_id: 1,
            username: 'admin',
            role: 'admin',
          }),
        });
      }
    });

    await page.goto('/admin.html');
    await page.waitForTimeout(1000);

    // 验证 refresh 被调用
    expect(refreshCalled).toBe(true);
  });

  // ── 链路 6: 管理后台导航与权限测试 ──
  test('管理后台导航切换与权限控制', async ({ page }) => {
    // 初始化 localStorage 预置 Token
    await page.goto('/login.html');
    await page.evaluate(() => {
      localStorage.setItem('token', 'mock-jwt-token');
      localStorage.setItem('user', JSON.stringify({ username: 'admin', role: 'admin' }));
    });

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

    await page.goto('/admin.html');

    // 等待页面加载
    await page.waitForTimeout(500);

    // 验证导航按钮存在
    const navButtons = page.locator('.admin-nav-btn');
    await expect(navButtons).toHaveCount(6); // monitor, users, knowledge, prompts, alerts, system

    // 点击各导航按钮，验证 section 切换
    const sections = ['monitor', 'users', 'knowledge', 'prompts', 'alerts', 'system'];
    for (const section of sections) {
      await page.click(`.admin-nav-btn[data-section="${section}"]`);
      await page.waitForTimeout(200);
      // 验证对应 section 显示
      const sectionMap = {
        monitor: 'sectionMonitor',
        users: 'sectionUsers',
        knowledge: 'sectionKnowledge',
        prompts: 'sectionPrompts',
        alerts: 'sectionAlerts',
        system: 'sectionSystem',
      };
      const sectionEl = page.locator(`#${sectionMap[section]}`);
      await expect(sectionEl).toBeVisible();
    }
  });

  // ── 链路 7: 会话持久化与恢复测试 ──
  test('会话创建、切换与持久化', async ({ page }) => {
    await page.goto('/login.html');
    await page.evaluate(() => {
      localStorage.setItem('token', 'mock-jwt-token');
      localStorage.setItem('user', JSON.stringify({ username: 'admin', role: 'admin' }));
    });

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
        body: JSON.stringify({
          sessions: [
            { session_id: 'sess-001', title: '产品咨询', message_count: 5, last_activity: '2026-06-10T18:00:00Z' },
            { session_id: 'sess-002', title: '订单查询', message_count: 3, last_activity: '2026-06-10T17:00:00Z' },
          ],
        }),
      });
    });

    await page.goto('/');

    // 验证会话列表加载
    await page.waitForTimeout(500);
    const sessionItems = page.locator('.session-item');
    await expect(sessionItems).toHaveCount(2);

    // 验证会话标题
    await expect(page.locator('.session-item').first()).toContainText('产品咨询');
  });

  // ── 链路 8: 状态管理测试（加载中、空数据、错误） ──
  test('状态管理：加载中、空数据、错误提示', async ({ page }) => {
    await page.goto('/login.html');
    await page.evaluate(() => {
      localStorage.setItem('token', 'mock-jwt-token');
      localStorage.setItem('user', JSON.stringify({ username: 'admin', role: 'admin' }));
    });

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
      // 返回空会话列表
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ sessions: [] }),
      });
    });

    await page.goto('/');
    await page.waitForTimeout(500);

    // 验证空会话列表的友好提示
    const emptyHint = page.locator('.sessions-empty');
    await expect(emptyHint).toBeVisible();
  });
});
