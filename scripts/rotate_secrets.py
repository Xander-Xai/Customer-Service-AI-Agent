#!/usr/bin/env python3
"""
生产环境密钥轮换脚本 (v5.3)
用法:
    python3 scripts/rotate_secrets.py [env_file_path]    # 交互式轮换
    python3 scripts/rotate_secrets.py --check            # 仅检查过期状态
    python3 scripts/rotate_secrets.py --rotate-all      # 轮换所有可轮换密钥

说明:
    - 自动轮换 JWT_SECRET, SESSION_TOKEN_SECRET, ADMIN_PASSWORD 等内部高风险密码密钥
    - 保留外部 API Key (OpenAI/SiliconFlow) 不变（由供应商管理）
    - 维护 secrets/keys.json 台账，记录轮换历史
    - 支持过期告警（基于密钥上次轮换时间估算）
"""

import base64
import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

# 默认轮换周期（天）
DEFAULT_ROTATION_DAYS = 90
HIGH_RISK_ROTATION_DAYS = 30


def generate_key(nbytes: int, encoding: str = "base64url") -> str:
    raw = secrets.token_bytes(nbytes)
    if encoding == "hex":
        return raw.hex()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def get_ledger_path() -> Path:
    """获取台账文件路径"""
    script_dir = Path(__file__).parent
    project_dir = script_dir.parent
    ledger_dir = project_dir / "secrets"
    ledger_dir.mkdir(exist_ok=True)
    return ledger_dir / "keys.json"


def load_ledger() -> dict:
    """加载密钥台账"""
    ledger_path = get_ledger_path()
    if ledger_path.exists():
        with open(ledger_path, encoding="utf-8") as f:
            return json.load(f)
    return {"keys": {}, "last_rotation": None}


def save_ledger(ledger: dict) -> None:
    """保存密钥台账"""
    ledger_path = get_ledger_path()
    ledger["last_rotation"] = datetime.now().isoformat()
    with open(ledger_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)


def check_key_expiry(
    ledger: dict, key_name: str, rotation_days: int = DEFAULT_ROTATION_DAYS
) -> tuple[bool, int]:
    """检查密钥是否过期或即将过期

    Returns:
        (is_expired_or_near_expiry, days_until_expiry)
    """
    if key_name not in ledger.get("keys", {}):
        return True, -1  # 从未轮换过

    last_rotated = ledger["keys"][key_name].get("last_rotated")
    if not last_rotated:
        return True, -1

    try:
        last_date = datetime.fromisoformat(last_rotated)
        days_elapsed = (datetime.now() - last_date).days
        days_remaining = rotation_days - days_elapsed
        return days_remaining <= 7, days_remaining
    except (ValueError, TypeError):
        return True, -1


def main() -> None:
    script_dir = Path(__file__).parent
    project_dir = script_dir.parent

    # 解析命令行参数
    check_only = "--check" in sys.argv
    # rotate_all = "--rotate-all" in sys.argv  # 保留供将来扩展

    target_file = None
    for arg in sys.argv[1:]:
        if arg.startswith("--") or arg == "python3":
            continue
        if os.path.isfile(arg):
            target_file = arg
            break

    if target_file is None:
        target_file = project_dir / ".env.prod.generated"
        if not target_file.exists():
            target_file = project_dir / ".env"

    if not os.path.isfile(target_file):
        print(f"错误: 未找到目标环境配置文件 '{target_file}'")
        print("用法: python3 scripts/rotate_secrets.py [env_file_path]")
        sys.exit(1)

    # 加载台账
    ledger = load_ledger()
    ledger_path = get_ledger_path()

    # 定义轮换规则
    rotation_rules = {
        "JWT_SECRET": {"bytes": 48, "encoding": "base64url", "risk": "high"},
        "SESSION_TOKEN_SECRET": {"bytes": 48, "encoding": "base64url", "risk": "high"},
        "MONITORING_ADMIN_TOKEN": {"bytes": 32, "encoding": "hex", "risk": "medium"},
        "ADMIN_PASSWORD": {"bytes": 24, "encoding": "base64url", "risk": "high"},
        "GRAFANA_PASSWORD": {"bytes": 16, "encoding": "base64url", "risk": "medium"},
        "REDIS_PASSWORD": {"bytes": 24, "encoding": "base64url", "risk": "high"},
        # 会话加密密钥：可轮换，但**不能直接替换**。
        #
        # 没有 key-id 时，直接换掉当前密钥 = 一次性作废全部既有密文（读不开了）。
        # 正确做法（本脚本生成的新值写入 SESSION_ENCRYPTION_KEY，同时要求把**旧值**
        # 挪进 SESSION_ENCRYPTION_KEY_PREVIOUS）：旧密文仍可解密，新写入用新密钥，
        # 旧会话在被改写时自动升级为密文 —— 无破坏轮换，不需要导出任何会话内容。
        #
        # 因此这里只**生成**新密钥并提示后续步骤；替换与"旧值入 previous"由操作者
        # 按 docs/operations/production-operations-guide.md §会话数据加密 执行。
        "SESSION_ENCRYPTION_KEY": {"bytes": 32, "encoding": "base64url", "risk": "high"},
    }

    if check_only:
        # 仅检查模式
        print("=" * 60)
        print("密钥过期检查")
        print("=" * 60)
        print(f"台账文件: {ledger_path}")
        print()

        with open(target_file, encoding="utf-8") as f:
            content = f.read()

        expired_or_near = []
        for match in re.finditer(r"^([A-Z0-9_]+)=(.*)$", content, re.MULTILINE):
            key = match.group(1)
            if key in rotation_rules:
                days = rotation_rules[key]["days"] = (
                    HIGH_RISK_ROTATION_DAYS
                    if rotation_rules[key]["risk"] == "high"
                    else DEFAULT_ROTATION_DAYS
                )
                is_near, remaining = check_key_expiry(ledger, key, days)
                if is_near:
                    status = "🔴 已过期" if remaining <= 0 else "🟡 即将过期"
                    expired_or_near.append((key, status, remaining))

        if expired_or_near:
            print("需要轮换的密钥:")
            for key, status, remaining in expired_or_near:
                print(f"  {status} {key} (剩余 {remaining} 天)")
            print()
            print("运行 'python3 scripts/rotate_secrets.py --rotate-all' 进行轮换")
        else:
            print("✅ 所有密钥状态正常")
        return

    # 读取目标文件
    with open(target_file, encoding="utf-8") as f:
        content = f.read()

    # 备份原文件
    backup_file = f"{target_file}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    with open(backup_file, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"已创建备份文件: {backup_file}")

    # 执行轮换
    lines = content.splitlines(keepends=True)
    new_lines = []
    rotated_keys = []
    now = datetime.now().isoformat()
    # 会话加密密钥的旧值：轮换时必须挪进 SESSION_ENCRYPTION_KEY_PREVIOUS，
    # 否则既有密文一次性作废（没有 key-id 时无法定向重加密）。见下方 special case。
    previous_session_key: str | None = None

    for line in lines:
        stripped = line.strip()
        new_line = line

        match = re.match(r"^([A-Z0-9_]+)=(.*)$", stripped)
        if match:
            key, val = match.groups()
            if key in rotation_rules:
                rule = rotation_rules[key]
                new_val = generate_key(rule["bytes"], rule["encoding"])
                if key == "SESSION_ENCRYPTION_KEY" and val.strip():
                    previous_session_key = val.strip()
                new_line = f"{key}={new_val}\n"
                rotated_keys.append(key)

                # 更新台账
                if key not in ledger["keys"]:
                    ledger["keys"][key] = {}
                ledger["keys"][key]["last_rotated"] = now
                ledger["keys"][key]["rotation_days"] = (
                    HIGH_RISK_ROTATION_DAYS if rule["risk"] == "high" else DEFAULT_ROTATION_DAYS
                )
                ledger["keys"][key]["risk_level"] = rule["risk"]

        new_lines.append(new_line)

    # 会话加密密钥：把**旧值**写进 SESSION_ENCRYPTION_KEY_PREVIOUS（保留原有历史项）。
    # 这是"无破坏轮换"的关键一步 —— 旧密文仍可解密，新写入用新密钥，旧会话在被改写
    # 时自动升级为密文。不做这一步，轮换 = 一次性作废全部既有会话密文。
    if previous_session_key is not None:
        _previous_line_idx = None
        for idx, line in enumerate(new_lines):
            if line.strip().startswith("SESSION_ENCRYPTION_KEY_PREVIOUS="):
                _previous_line_idx = idx
                break
        existing = ""
        if _previous_line_idx is not None:
            existing = new_lines[_previous_line_idx].split("=", 1)[1].strip()
        merged = [item for item in existing.split(",") if item]
        if previous_session_key not in merged:
            merged.insert(0, previous_session_key)
        merged_value = ",".join(merged)
        if _previous_line_idx is not None:
            new_lines[_previous_line_idx] = f"SESSION_ENCRYPTION_KEY_PREVIOUS={merged_value}\n"
        else:
            new_lines.append(f"SESSION_ENCRYPTION_KEY_PREVIOUS={merged_value}\n")
        print(
            "🔄 SESSION_ENCRYPTION_KEY 已轮换；旧值已写入 SESSION_ENCRYPTION_KEY_PREVIOUS。"
            "既有密文仍可解密（旧会话在被改写时自动升级为新密钥）。"
            "排空后（session_encryption_key_fallback_total 归零）可清空该历史项。"
        )

    # 保存新配置
    result = "".join(new_lines)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(result)

    # 保存台账
    save_ledger(ledger)

    print("=" * 60)
    print("密钥轮换成功完成！")
    print("=" * 60)
    print("已轮换的密钥:")
    for k in rotated_keys:
        print(f"  [Rotated] {k}")
    print()
    print(f"台账已更新: {ledger_path}")
    print("请记得重启受影响的服务以应用新的配置。")


if __name__ == "__main__":
    main()
