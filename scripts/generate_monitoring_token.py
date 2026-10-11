#!/usr/bin/env python3
"""生成 Prometheus 抓取凭据文件（fail-closed，不静默降级）。

为什么需要它
------------
``monitoring/prometheus.yml`` 的两个 scrape job 都用标准的
``bearer_token_file``，指向容器内 ``/etc/prometheus/secrets/monitoring_admin_token``。
应用的 ``/metrics`` 与 ``/metrics/prometheus`` 都在 supervisor/admin 认证面内
（``api.utils.check_admin_token``），没有凭据一律 401。因此该文件缺失时
Prometheus 拒绝启动 —— 收集不到指标必须**响亮地失败**。

它做什么 / 不做什么
-------------------
- 做：把 ``MONITORING_ADMIN_TOKEN`` 写入目标文件（0600），供 Prometheus 读取。
- 不做：不联系 Prometheus、不验证抓取、不声称「告警闭环已验证」。
  真正的闭环验证必须制造一次 dead-letter 并在 Alertmanager 看到通知。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "deploy" / "monitoring" / "secrets" / "monitoring_admin_token"

#: 与 core/config.py 启动诊断共用同一组「这不是真凭据」的判据。
PLACEHOLDER_VALUES = {
    "",
    "change-me-monitoring-token",
    "change-me",
    "changeme",
    "your-monitoring-token",
}


def _min_token_length() -> int:
    """与应用启动诊断共用同一阈值（``core.config.MONITORING_ADMIN_TOKEN_MIN_LENGTH``）。

    import core.config 触发整条配置校验链（含 validate_required_config 的 fail-closed），
    因此这里**不** import，只在失败时回落到脚本内的同一常量 —— 两处数值相同，
    且都有注释互相指认。
    """
    try:
        from core.config import MONITORING_ADMIN_TOKEN_MIN_LENGTH

        return int(MONITORING_ADMIN_TOKEN_MIN_LENGTH)
    except Exception:
        return 16


def _load_token_from_env_file() -> str | None:
    """从仓库根 ``.env`` 读 ``MONITORING_ADMIN_TOKEN``（不改写 .env）。"""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "MONITORING_ADMIN_TOKEN":
                return value.strip().strip('"').strip("'")
    except OSError as exc:  # pragma: no cover - 读不到就当没配
        print(f"读取 .env 失败: {exc}", file=sys.stderr)
    return None


def main() -> int:
    token = os.environ.get("MONITORING_ADMIN_TOKEN", "").strip()
    if not token:
        token = (_load_token_from_env_file() or "").strip()

    if token.lower() in PLACEHOLDER_VALUES:
        print(
            "🚨 MONITORING_ADMIN_TOKEN 未设置或仍是占位符。\n"
            "   Prometheus 无法抓取指标（/metrics 与 /metrics/prometheus 都要认证），\n"
            "   DLQ 告警链会静默失效。\n"
            '   生成一个：python3 -c "import secrets;print(secrets.token_urlsafe(32))"',
            file=sys.stderr,
        )
        return 1
    min_length = _min_token_length()
    if len(token) < min_length:
        print(
            f"🚨 MONITORING_ADMIN_TOKEN 长度不足（{len(token)} < {min_length}）。"
            "弱凭据同样会让抓取配置形同虚设。",
            file=sys.stderr,
        )
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(token, encoding="utf-8")
    TARGET.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600：只有属主可读

    print(f"✅ 已生成 Prometheus 抓取凭据：{TARGET.relative_to(REPO_ROOT)}（0600）")
    print("   下一步：make monitoring-up   # 启动 prometheus + grafana + alertmanager")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
