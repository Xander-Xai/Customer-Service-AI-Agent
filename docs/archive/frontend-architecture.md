# 前端架构摘要（2026-06-08 优化设计）

> 状态：已全部执行 | 原始设计文档 647 行，此为压缩摘要

## 架构决策

**构建工具：** Vite（ES Module + Rollup 多入口打包）
- 入口：`index.html`（主聊天）、`login.html`（登录）、`admin.html`（管理）
- 输出：`web/static/dist/`（开发时 `npm run dev`，生产 `npm run build`）
- 代理：`/api` → localhost:8000，`/ws` → ws://localhost:8000

## 模块划分

```
web/src/
├── main.js          # 主入口，组装各模块
├── login.js         # 登录页逻辑
├── admin.js         # 管理后台逻辑
├── api/             # API 层
│   ├── index.js     # 统一导出
│   ├── rest.js      # REST 客户端
│   ├── sse.js       # SSE 流式客户端
│   ├── websocket.js # WebSocket 客户端
│   └── events.js    # 事件总线
├── auth/            # 认证（Token 管理 + 刷新）
├── chat/            # 对话核心
│   ├── messages.js  # 消息渲染（Markdown + 代码高亮）
│   ├── input.js     # 输入框（快捷键 + 文件上传）
│   ├── sessions.js  # 会话列表
│   ├── search.js    # 消息搜索
│   ├── voice.js     # 语音输入/输出
│   ├── shortcuts.js # 键盘快捷键
│   └── welcome.js   # 欢迎页模板
├── monitor/         # 监控仪表盘
└── utils/           # 工具函数
    ├── dom.js       # DOM 操作
    ├── format.js    # 格式化
    ├── markdown.js  # Markdown 渲染（marked.js）
    ├── toast.js     # 通知提示
    └── agents.js    # Agent 状态映射
```

## CSS 模块化

```
web/styles/
├── variables.css    # CSS 变量（颜色/间距/字体）
├── layout.css       # 布局（三栏/侧边栏/主区域）
├── components.css   # 组件（消息气泡/输入框/按钮/卡片）
├── animations.css   # 动画（打字指示器/淡入/骨架屏）
├── responsive.css   # 响应式（移动端抽屉/折叠侧边栏）
├── login.css        # 登录页
├── admin.css        # 管理后台
└── monitor.css      # 监控仪表盘
```

## 关键 UX 改善

- **SSE 流式输出：** 逐字显示 + 打字指示器动画
- **Markdown 渲染：** marked.js + 代码高亮 + 表格/列表支持
- **消息搜索：** Ctrl+F 搜索历史消息
- **语音输入：** Web Speech API（需 HTTPS）
- **ARIA 无障碍：** 焦点管理 + 屏幕阅读器支持 + 对比度修复
- **移动端：** 抽屉式侧边栏 + 触摸友好

> 原始设计文档可通过 git 历史追溯。
