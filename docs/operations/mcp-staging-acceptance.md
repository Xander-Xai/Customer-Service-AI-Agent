# MCP 第三方 staging 验收入口

适用范围：智能客服项目的工具接入验收。已有官方 SDK + 本地模拟协议对端的 CI 契约；本入口用于项目所有者另行授权的真实测试服务。当前未执行任何真实第三方连接，外部服务兼容性仍为 NOT_VERIFIED。

## 前置条件

- 确认服务及工具归属、授权范围、版本、只读语义和独立期望结果。仅使用合成数据，不得传入真实客户信息、订单或凭据。
- 安装仓库声明的可选 MCP SDK；只支持现有适配器的 stdio / SSE，不代表 Streamable HTTP 或 OAuth 兼容。
- stdio 只运行已审计、预安装的可执行文件；不从未知来源下载。SSE 只连接已审核的测试端点，不在 URL 中嵌入凭据。
- 私有 manifest 不提交 Git。以下示例是假设的本地工具，不表示存在真实服务。

```json
{
  "server": {
    "name": "staging",
    "transport": "stdio",
    "command": "/absolute/path/to/reviewed-mcp-server",
    "args": [],
    "risk_level": "low",
    "allowed_tools": ["lookup"],
    "timeout_seconds": 5
  },
  "tool": "lookup",
  "arguments": {"id": "synthetic"},
  "expected_result": {"stock": 3}
}
```

独立期望结果应在调用前由可信测试数据或独立查询确定，不得把实际输出复制为期望。

Linux 上为 SDK 生命周期设置外部进程超时，避免 close 卡死时误认为成功：

```bash
timeout -k 5s 60s python -m scripts.verify_mcp_staging \
  --manifest /private/staging-case.json \
  --out artifacts/mcp/staging-report.json \
  --permit-staging-mcp
```

退出码 0 且报告 PASS 仅表示该工具注册、白名单拒绝、执行回执与独立期望结果匹配。超时、缺少报告或任何检查失败都不能通过。报告不保存原始参数、输出、凭据或远端异常。外部服务日志仍应由运维隔离与审查。报告不证明第三方身份、生产鉴权、写操作幂等性或业务 SLA。应同时记录服务版本、被测 Git SHA 和脱敏授权证据。
