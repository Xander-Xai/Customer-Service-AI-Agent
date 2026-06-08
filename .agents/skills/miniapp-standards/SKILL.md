# MiniApp Standards（微信小程序专项规范）

> 适用对象：`uiux-designer`（在 MiniApp Mode 下）/ `miniapp-dev` / `miniapp-code-reviewer` / `miniapp-qa-engineer`。
> 与 `coding.md` / `security.md` / `testing.md` 并列；通用规则仍在原文件，不重复。

## 1. 目录结构约定

```
miniapp/
  src/                  # Taro 源码（仅触发 Taro 场景时使用）
    pages/
    components/
    services/
  native/               # 原生 WXML/WXSS/JS（默认主体）
    pages/
    components/
    utils/
  config/               # project.config.json + sitemap.json
  dist/                 # 构建产物（gitignore）
```

## 2. 框架选型决策树（默认原生）

默认原生 WXML/WXSS/JS；以下少数场景触发 Taro：
1. 该页面 80% 以上业务逻辑已存在于 Web React 组件，需复用
2. 团队需在 Web 与小程序间快速同步同一功能（双端发布）
3. 选型分歧时由 `tech-lead` 仲裁

## 3. WeUI 设计规范要点

- 安全区：iOS 顶部 44px + 状态栏；底部 34px（iPhone X+）
- 胶囊按钮：右上 87×32px，禁止覆盖
- tabBar：最多 5 项，图标 81×81px
- 字体：苹方 / 思源黑体；正文 14sp / 标题 17sp

## 4. 微信审核红线

- **禁止**：诱导分享 / 强制关注 / 跳转非备案外链 / 未声明的数据收集
- **必备**：隐私协议弹窗、用户协议链接、`getUserProfile` 触发时机合规
- **包体积**：主包 ≤ 2MB、总包 ≤ 20MB（超限走分包加载）

## 5. 性能预算

- 首屏白屏 ≤ 2s（4G 网络）
- setData 单次 payload ≤ 256KB
- 长列表强制虚拟滚动（>100 项）

## 6. 测试矩阵

| 阶段 | 工具 | 通过门槛 | 责任角色 |
|---|---|---|---|
| **SIT** | DevTools 模拟器 + 自动化脚本 | 全部 AC 通过率 100% | **miniapp-dev 自跑**，证据进 `progress/miniapp-dev.md` 的 `**SIT 证据**` 段，由 `miniapp-code-reviewer` 在 code review 时 audit |
| **E2E** | 体验版二维码 + 真机 | iOS 最新版 + Android 主流厂商任一台均通过 | `miniapp-qa-engineer` |
| **UAT** | 体验版二维码 + 真实用户 | 业务 AC 逐条签字 | `miniapp-qa-engineer` 执行 + `product-lead` 业务签字 |

### 自动化技术栈（标准方案）

为避免依赖第三方 MCP（如 `@creatoria/miniapp-mcp` / `roy2an/minium-mcp-server` 均为社区项目），团队采用**微信官方 SDK + Jest** 组合：

- **`miniprogram-automator`**（微信官方 npm 包）：启动开发者工具、控制小程序、操作页面与元素、注入代码、截图、mock wx API、性能评分、真机调试
- **Jest**：测试框架（断言、生命周期、报告）

### 目录结构

```
miniapp/
  native/
    tests/
      e2e/                    # 端到端脚本，用 miniprogram-automator + Jest
        login.test.js
        share.test.js
      unit/                   # 组件单测，用 miniprogram-simulate + Jest
        components/
```

### 启动 DevTools 自动化端口

```bash
# macOS 示例
/Applications/wechatwebdevtools.app/Contents/MacOS/cli \
  --auto /path/to/miniapp/native --auto-port 9420
```

CI 环境用 `automator.connect({wsEndpoint: 'ws://127.0.0.1:9420'})` 接入。

### 最小可用 E2E 脚本骨架

```javascript
// miniapp/native/tests/e2e/login.test.js
const automator = require('miniprogram-automator')

let miniProgram

beforeAll(async () => {
  miniProgram = await automator.launch({
    cliPath: '/Applications/wechatwebdevtools.app/Contents/MacOS/cli',
    projectPath: __dirname + '/../../',
  })
}, 60000)

afterAll(async () => {
  await miniProgram.close()
})

test('AC-1: 用户进入登录页能看到微信登录按钮', async () => {
  const page = await miniProgram.reLaunch('/pages/login/login')
  const btn = await page.$('.btn-wx-login')
  expect(await btn.attribute('class')).toContain('btn-wx-login')
})

test('AC-2: 点击登录按钮触发 wx.login', async () => {
  const page = await miniProgram.currentPage()
  // mock wx.login，避免真实调用
  await miniProgram.mockWxMethod('login', () => ({ code: 'MOCK_CODE' }))
  const btn = await page.$('.btn-wx-login')
  await btn.tap()
  await page.waitFor(500)
  // 断言跳转到首页
  expect((await miniProgram.currentPage()).path).toBe('pages/home/home')
})
```

### 核心能力速查（基于 `miniprogram-automator` 官方 API）

| 用途 | API |
|---|---|
| 启动 / 连接 / 关闭 | `automator.launch()` / `automator.connect()` / `miniProgram.close()` |
| 页面跳转 | `reLaunch` / `navigateTo` / `redirectTo` / `switchTab` / `navigateBack` |
| 元素选择 | `page.$(selector)` / `page.$$(selector)` |
| 元素操作 | `element.tap()` / `element.input(text)` / `element.attribute()` |
| wx API 控制 | `callWxMethod` / `mockWxMethod` / `restoreWxMethod` |
| 注入与截图 | `evaluate(fn)` / `screenshot()` |
| 真机调试 | `miniProgram.remote()` |
| 性能评分 | `stopAudits()`（启动时间、setData 频率等） |

### 角色分工

| 任务 | 责任角色 |
|---|---|
| 编写 `tests/e2e/*.test.js` 脚本 | `miniapp-dev`（开发自验时一并提交） |
| 执行测试脚本、收集报告、判定通过 | `miniapp-qa-engineer`（E2E / UAT 阶段；集成层由 `miniapp-dev` 自跑） |
| Mock 数据 / mock wx API 写在脚本里 | `miniapp-dev`（脚本作者负责 mock） |
| 真机 E2E（扫码 + 多设备） | `miniapp-qa-engineer` |

> 第三方 MCP（如 `@creatoria/miniapp-mcp`）可作**可选增强**（让 AI agent 直接以自然语言驱动 DevTools），但**默认方案不依赖**——确保团队基线在断网或无第三方服务时也能跑通；如需引入须走 `tech-lead` ADR。

## 7. SendMessage 协作模板

- `uiux-designer（MiniApp Mode）→ miniapp-dev`：交付设计稿 + WeUI 标注
- `miniapp-dev → backend-dev`：对齐 wx.request API 契约
- `miniapp-dev → miniapp-code-reviewer`：完成自验后请求审查
- `miniapp-code-reviewer → miniapp-qa-engineer`：审查通过后流转 QA
- 任何阶段失败 → 回到 `product-lead` 重派

## 8. 与 Web 团队的边界

- `backend-dev`、`ai-agent-dev`、`ml-engineer` 共用，不区分 Web / MiniApp；API 层默认与 Web 共用（同一个 `backend-dev`），接口按业务域划分而非按端划分
- UI 层各自维护，不强制 Web 与小程序共享代码；如确有跨端复用需求，由 `tech-lead` 单开 ADR 约定共享目录与边界

## 9. MiniApp Mode 行为（uiux-designer 在小程序场景的 overlay）

> 适用对象：`uiux-designer`。`product-lead` 任务明确为微信小程序（提到 "小程序" / "miniapp" / "微信端"）时，本角色应用以下附加规则。

- **产出路径**：`docs/design/[feature]-miniapp/spec.md` + `index.html`（与 Web 设计区分）
- **设计规范**：遵循本文件第 3 节 WeUI 要点（导航栏、tabBar、安全区、胶囊按钮、字号字体）
- **审核红线前置**：设计阶段就标注第 4 节相关约束——隐私协议弹窗时机、用户协议链接位置、`getUserProfile` 触发时机（仅在用户主动操作时）
- **微信交互约束**：明确无 hover 态、无右键菜单、下拉刷新区域、转发卡片样式、订阅消息时机
- **视口**：仅设计 375×667 移动视口，不做桌面断点
- **交付对象**：通过 SendMessage 交付给 `miniapp-dev`（非 `frontend-dev`），消息内容同 Web 模板，路径替换为 `[feature]-miniapp/`

> 设计意图：本节集中 Designer 在小程序模式下的所有附加规则；`customize.sh --preset minimal`（仅 Web 项目）会连同本文件一并裁剪，让 `uiux-designer.md` 不必同步删除——单一来源 + 干净裁剪。
