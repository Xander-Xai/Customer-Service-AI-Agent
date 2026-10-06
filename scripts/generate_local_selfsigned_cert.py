#!/usr/bin/env python3
"""生成**本地开发专用**自签证书 —— LOCAL DEVELOPMENT ONLY。

⚠️  这里生成的东西**不是**生产证书，也不应被误认为生产证书。
    原因见下：签发者/主题/有效期/预检策略四处都把它标成非生产。

为什么还需要它
--------------
issue #57 的结论是：生产 TLS 物料必须由运维/证书机构签发，仓库不提供。
但本地跑 ``make dev-https``（走 uvicorn 的 ``.certs/``）或本地 compose
（走 ``deploy/nginx/ssl/``）同样需要一对文件，否则会卡在"证书不存在"。
让每个开发者手抄 openssl 命令、并自行判断该放到哪个目录，容易把临时材料
误放到生产路径上。所以这里把"本地"这件事**写进证书本身**，并让生产预检
（``scripts/check_tls_material.py``）默认拒绝自签名证书 —— 误用的成本因此
落在流程上，而不是靠人记住。

刻意做的事
----------
* 主题写明 ``LOCAL DEVELOPMENT ONLY``，签发者同理 —— 任何
  ``openssl x509 -noout -subject`` 都能一眼看出不是生产证书；
* SAN 只含 ``localhost`` / ``127.0.0.1``，无法用于任何真实域名；
* 有效期默认 30 天（``--days`` 可调）—— 临时材料不会在仓库里"陈年"；
* 拒绝覆盖已存在的文件，避免把运维签发的证书顶掉；
* 写到已被 ``.gitignore`` 排除的目录（``deploy/nginx/ssl/`` 或 ``.certs/``），
  私钥不会被提交。仅依赖标准库 + openssl CLI。
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BANNER = """
╔══════════════════════════════════════════════════════════════════╗
║  LOCAL DEVELOPMENT ONLY — NOT A PRODUCTION CERTIFICATE            ║
║  仅用于本地开发。生产环境请用证书机构/ACME 签发的证书，            ║
║  否则客户端不信任，故障表现为"服务活着但浏览器报错"。             ║
╚══════════════════════════════════════════════════════════════════╝
"""


def fail(message: str) -> int:
    print(f"❌ {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 LOCAL DEVELOPMENT ONLY 自签证书（禁止用于生产）",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="输出目录。默认按 --target 选择：nginx → deploy/nginx/ssl，dev-https → .certs",
    )
    parser.add_argument(
        "--target",
        choices=["nginx", "dev-https"],
        default="nginx",
        help="nginx = deploy/nginx/ssl（compose 栈）；dev-https = .certs（make dev-https）",
    )
    parser.add_argument("--days", type=int, default=30, help="有效期天数（默认 30）")
    parser.add_argument("--cn", default="localhost", help="证书主题 CN（默认 localhost）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的文件（默认拒绝，避免顶掉运维签发的证书）",
    )
    args = parser.parse_args(argv)

    if args.days <= 0:
        return fail("--days 必须为正整数")
    if args.days > 365:
        return fail(
            f"拒绝生成有效期 {args.days} 天的本地证书（上限 365）。"
            "长期有效的本地证书容易被误当成生产材料。"
        )

    openssl = shutil.which("openssl")
    if openssl is None:
        return fail("未找到 openssl CLI，无法生成证书。请安装 openssl 后重试。")

    if args.out_dir:
        out_dir = args.out_dir.expanduser()
    elif args.target == "nginx":
        out_dir = REPO_ROOT / "deploy" / "nginx" / "ssl"
    else:
        out_dir = REPO_ROOT / ".certs"
    out_dir = out_dir.resolve()

    cert, key = out_dir / "cert.pem", out_dir / "key.pem"

    existing = [p for p in (cert, key) if p.exists()]
    if existing and not args.force:
        listed = "\n".join(f"    - {p}" for p in existing)
        return fail(
            f"目标位置已有文件，拒绝覆盖：\n{listed}\n"
            "  这些可能是运维签发的生产证书，本脚本只负责本地临时材料。\n"
            "  确实要替换请显式加 --force。"
        )

    print(BANNER.strip())
    print(f"输出目录: {out_dir}")
    print(f"有效期  : {args.days} 天")

    out_dir.mkdir(parents=True, exist_ok=True)
    # openssl 需要相对路径或绝对路径均可；用绝对路径并显式指定 -keyout/-out
    subject = f"/CN={args.cn}/O=LOCAL DEVELOPMENT ONLY"
    san = "DNS:localhost,IP:127.0.0.1"

    cmd = [
        openssl,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        str(args.days),
        "-nodes",
        "-subj",
        subject,
        "-addext",
        f"subjectAltName={san}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0:
        return fail(f"openssl 生成失败：\n{result.stderr.strip()[:500]}")

    # 权限收紧失败不致命：openssl 已用 600 创建，这里只是 best-effort 兜底
    with contextlib.suppress(OSError):
        key.chmod(0o600)

    # 自检：确认产物确实是"本地"证书，而不是某个别的东西被写进来了
    subject_out = subprocess.run(
        [openssl, "x509", "-in", str(cert), "-noout", "-subject", "-enddate"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if "LOCAL DEVELOPMENT ONLY" not in subject_out.stdout:
        return fail("自检失败：证书主题未包含 LOCAL DEVELOPMENT ONLY 标记，已中止。")

    print("")
    print("已生成（LOCAL DEVELOPMENT ONLY）：")
    print(f"  证书: {cert}")
    print(f"  私钥: {key}  (chmod 600)")
    print("")
    print("自检输出:")
    for line in subject_out.stdout.strip().splitlines():
        print(f"  {line}")
    print("")
    print("下一步：")
    print(f"  验证: python3 scripts/check_tls_material.py --ssl-dir {out_dir} --allow-self-signed")
    print("  生产: 请改用证书机构/ACME 签发的证书；上面的自签证书不可用于生产。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
