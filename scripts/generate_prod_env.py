#!/usr/bin/env python3
"""
生产环境密钥生成脚本
用法: python3 scripts/generate_prod_env.py
输出: .env.prod.generated（自动替换所有 CHANGE_ME_* 占位符）
"""
import base64
import os
import re
import secrets


def generate_key(nbytes: int, encoding: str = "base64url") -> str:
    """生成密码学安全的随机密钥。

    Args:
        nbytes: 随机字节数
        encoding: 编码方式，"base64url" 或 "hex"
    """
    raw = secrets.token_bytes(nbytes)
    if encoding == "hex":
        return raw.hex()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    input_path = os.path.join(project_dir, ".env.prod")
    output_path = os.path.join(project_dir, ".env.prod.generated")

    if not os.path.exists(input_path):
        print(f"错误: 未找到模板文件 {input_path}")
        raise SystemExit(1)

    # 定义占位符到生成密钥的映射
    replacements: dict[str, str] = {
        "CHANGE_ME_TO_SECURE_RANDOM_JWT_SECRET": generate_key(48, "base64url"),
        "CHANGE_ME_TO_SECURE_RANDOM_SECRET": generate_key(48, "base64url"),
        "CHANGE_ME_TO_SECURE_RANDOM_KEY": "sk-" + generate_key(32, "hex"),
        "CHANGE_ME_TO_SECURE_RANDOM_TOKEN": generate_key(32, "hex"),
        "CHANGE_ME_TO_SECURE_PASSWORD": generate_key(24, "base64url"),
        "CHANGE_ME_TO_STRONG_PASSWORD": generate_key(24, "base64url"),
    }

    # GRAFANA_PASSWORD 使用独立的占位符（与 POSTGRES 不同行上可能同名，需按上下文处理）
    grafana_password = generate_key(16, "base64url")
    postgres_password = replacements["CHANGE_ME_TO_STRONG_PASSWORD"]

    # ERP 占位符
    erp_app_secret = generate_key(32, "hex")

    with open(input_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 按行处理，根据上下文替换同名占位符
    lines = content.splitlines(keepends=True)
    new_lines: list[str] = []
    replaced_keys: list[str] = []

    for line in lines:
        stripped = line.strip()
        new_line = line

        # 优先处理上下文相关的占位符（同一占位符在不同行需要不同值）
        # GRAFANA_PASSWORD=CHANGE_ME_TO_STRONG_PASSWORD 使用独立密钥
        if stripped.startswith("GRAFANA_PASSWORD=CHANGE_ME"):
            new_line = re.sub(r"CHANGE_ME\w*", grafana_password, new_line)
            if "GRAFANA_PASSWORD" not in replaced_keys:
                replaced_keys.append("GRAFANA_PASSWORD")
        # ERP_APP_SECRET=CHANGE_ME — 随机生成
        elif stripped.startswith("ERP_APP_SECRET="):
            new_line = re.sub(r"CHANGE_ME\b", erp_app_secret, new_line, count=1)
            if "ERP_APP_SECRET" not in replaced_keys:
                replaced_keys.append("ERP_APP_SECRET")
        else:
            # 替换唯一确定的占位符
            for placeholder, value in replacements.items():
                if placeholder in new_line:
                    new_line = new_line.replace(placeholder, value)
                    if placeholder not in replaced_keys:
                        replaced_keys.append(placeholder)

        # ERP_APP_ID=CHANGE_ME — 保留不变（需要用户提供真实 ERP App ID）
        # ERP_DB_ID=CHANGE_ME — 保留不变（需要用户提供真实 ERP DB ID）
        # OPENAI_API_KEY=CHANGE_ME_TO_REAL_KEY — 保留不变（需要用户提供真实 API Key）
        # CORS_ORIGINS / ALLOWED_ORIGINS / DOMAIN — 保留不变（需要用户提供真实域名）

        new_lines.append(new_line)

    result = "".join(new_lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)

    # 打印摘要
    print("=" * 60)
    print("生产环境密钥生成完成")
    print("=" * 60)
    print(f"输入文件:  {input_path}")
    print(f"输出文件:  {output_path}")
    print()
    print("已替换的密钥占位符:")
    for key in replaced_keys:
        print(f"  [OK] {key}")
    print()

    # 检查残留的 CHANGE_ME 占位符
    remaining = re.findall(r"CHANGE_ME\w*", result)
    if remaining:
        unique_remaining = sorted(set(remaining))
        print("以下占位符未替换（需要手动填写）:")
        for ph in unique_remaining:
            print(f"  [!] {ph}")
    else:
        print("所有 CHANGE_ME 占位符均已替换。")

    print()
    print("警告: .env.prod.generated 包含敏感密钥，请妥善保管！")
    print("建议: 使用后删除本文件，仅保留 .env.prod 模板。")


if __name__ == "__main__":
    main()
