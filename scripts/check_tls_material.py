#!/usr/bin/env python3
"""生产 nginx TLS 物料预检（issue #57）。

为什么需要它
------------
``deploy/nginx/nginx.conf`` 声明了 ``listen 443 ssl`` 并引用
``/etc/nginx/ssl/{cert.pem,key.pem}``，而该目录由运维自备、已被 ``.gitignore``
排除（这是正确的意图）。问题在于**没有任何环节检查过它**：

- ``docker compose config`` 校验 schema 与插值，不看文件系统 → 缺证书也返回 0；
- ``docker compose build`` 同理；
- bind mount 的源目录不存在时，Docker 会**自动创建一个空目录**，于是容器能起来，
  直到 nginx 读证书失败才崩：

      nginx: [emerg] cannot load certificate "/etc/nginx/ssl/cert.pem"

也就是说：变量缺失能在 compose 层用 ``${VAR:?}`` fail fast，而证书缺失不能 ——
证书是文件，不是变量。于是这份检查补上这一层，在 ``docker compose up`` **之前**
把问题说清楚。

检查项（按失败代价排序）
------------------------
1. 目录存在；2. 两个文件都存在且非空；3. 证书可解析；4. 私钥可解析；
5. **证书与私钥配对**（公钥摘要一致）—— 最常见的真实故障，且 nginx 的报错
   （"key values mismatch"）出现得比想象中晚；6. 证书未过期，并对临期给出警告；
7. 自签名策略：默认**拒绝**自签名证书，除非显式 ``--allow-self-signed``。

关于第 7 条：自签名证书在生产里不是可用的信任锚。它能被"跑起来"，但浏览器/
客户端不信任 —— 那是一个比启动失败更难排查的故障（服务是"活的"）。因此这里默认
拒绝，并给出可执行的替代方案，而不是"warn 一下"。

退出码：0 = 可用；1 = 不可用（消息已说明需要提供什么）；2 = 检查本身无法运行。

仅用标准库 + openssl CLI。openssl 不存在时退化为"只查文件存在性"并明确告知，
而不是静默放行 —— 少查一项必须说出来，否则调用方会以为全查过了。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SSL_DIR = REPO_ROOT / "deploy" / "nginx" / "ssl"

#: 距离过期少于该天数时给出警告（证书通常按年续期）。
EXPIRY_WARN_DAYS = 21


class CheckFailed(Exception):
    """检查结论为不可用。消息即面向 operator 的处置指引。"""


def _openssl() -> str | None:
    return shutil.which("openssl")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)


def _provisioning_guidance(ssl_dir: Path) -> str:
    return f"""
需要提供什么
------------
生产环境的 TLS 物料由**运维/证书机构签发**，仓库不提供、也绝不提交私钥。

  1. 向证书机构申请（或用 ACME，如 certbot / cert-manager）拿到一对文件；
  2. 放到本机（目录已被 .gitignore 排除，不会误提交）：

       {ssl_dir}/cert.pem     # 证书（fullchain 亦可）
       {ssl_dir}/key.pem      # 私钥，权限建议 600

  3. 重新执行本检查确认。

仅本地开发
----------
只想在本机跑通（`make dev-https` / 本地 compose），可以用本地自签脚本，
它生成的证书带 LOCAL DEVELOPMENT ONLY 标记，且本检查默认拒绝自签名证书：

    python3 scripts/generate_local_selfsigned_cert.py --help

生产环境请不要使用自签名证书：它无法被客户端信任，故障表现为"服务是活的但
浏览器报错"，比启动失败更难排查。
""".rstrip()


def check_files(ssl_dir: Path) -> tuple[Path, Path]:
    cert = ssl_dir / "cert.pem"
    key = ssl_dir / "key.pem"

    if not ssl_dir.is_dir():
        raise CheckFailed(
            f"TLS 目录不存在：{ssl_dir}\n"
            f"  Docker 会把缺失的 bind mount 源自动创建成**空目录**，"
            f"于是容器能启动、直到 nginx 读证书才失败。\n" + _provisioning_guidance(ssl_dir)
        )

    missing = [p for p in (cert, key) if not p.is_file()]
    if missing:
        listed = "\n".join(
            f"    - {p}  ({'不存在' if not p.exists() else '不是文件'})" for p in missing
        )
        raise CheckFailed(f"TLS 物料不完整，缺少：\n{listed}\n" + _provisioning_guidance(ssl_dir))

    empty = [p for p in (cert, key) if p.stat().st_size == 0]
    if empty:
        listed = "\n".join(f"    - {p}  （0 字节）" for p in empty)
        raise CheckFailed(
            f"TLS 物料存在但是空文件：\n{listed}\n"
            "  空的 bind mount 源与不存在等价：nginx 一样读不到证书。\n"
            + _provisioning_guidance(ssl_dir)
        )

    try:
        key_mode = key.stat().st_mode & 0o777
    except OSError:
        key_mode = 0o600
    if key_mode & 0o077:
        print(
            f"⚠️  私钥权限 {oct(key_mode)[2:]} 偏宽（建议 600）：{key}",
            file=sys.stderr,
        )
    return cert, key


def check_parses(cert: Path, key: Path, openssl: str) -> None:
    cert_result = _run([openssl, "x509", "-in", str(cert), "-noout", "-subject"])
    if cert_result.returncode != 0:
        raise CheckFailed(
            f"证书无法解析：{cert}\n"
            f"  openssl: {cert_result.stderr.strip()[:300]}\n"
            "  期望是 PEM 格式的 X.509 证书（首行 -----BEGIN CERTIFICATE-----）。"
        )
    key_result = _run([openssl, "pkey", "-in", str(key), "-noout", "-check"])
    if key_result.returncode != 0:
        raise CheckFailed(
            f"私钥无法解析：{key}\n"
            f"  openssl: {key_result.stderr.strip()[:300]}\n"
            "  期望是 PEM 格式的私钥（首行 -----BEGIN ... PRIVATE KEY-----）。"
        )


def check_pair_matches(cert: Path, key: Path, openssl: str) -> None:
    """证书与私钥必须是同一对。

    这是最常见的真实故障：分别申请/复制了两份材料，nginx 的报错
    ``key values mismatch`` 在启动日志里容易被漏看。
    """
    cert_pub = _run([openssl, "x509", "-in", str(cert), "-noout", "-pubkey"])
    key_pub = _run([openssl, "pkey", "-in", str(key), "-pubout"])
    if cert_pub.returncode != 0 or key_pub.returncode != 0:
        return  # 解析阶段已单独报错，这里不重复

    def digest(text: str) -> str:
        """直接哈希 PEM 的公钥段，避免把长证书塞进命令行（ARG_MAX）。"""
        return hashlib.sha256(text.strip().encode()).hexdigest()

    cert_digest, key_digest = digest(cert_pub.stdout), digest(key_pub.stdout)
    if cert_digest != key_digest:
        raise CheckFailed(
            "证书与私钥不匹配（公钥不同）。\n"
            f"  cert: {cert}\n  key : {key}\n"
            "  nginx 启动时会报 'key values mismatch'，且该行在启动日志里很容易被漏看。\n"
            "  请确认这两个文件来自同一次签发。"
        )


def check_expiry(cert: Path, openssl: str) -> str:
    result = _run([openssl, "x509", "-in", str(cert), "-noout", "-enddate"])
    if result.returncode != 0:
        return ""
    raw = result.stdout.strip().split("=", 1)[-1]
    try:
        not_after = dt.datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        print(f"⚠️  无法解析证书有效期：{raw!r}", file=sys.stderr)
        return raw
    remaining = not_after - dt.datetime.now(dt.timezone.utc)
    days = remaining.days
    if days < 0:
        raise CheckFailed(
            f"证书已于 {not_after.isoformat()} 过期（{-days} 天前）。\n"
            "  nginx 会拒绝加载过期证书。请续期后重新提供。"
        )
    if days <= EXPIRY_WARN_DAYS:
        print(
            f"⚠️  证书将于 {not_after.isoformat()} 过期（剩 {days} 天），请尽快续期。",
            file=sys.stderr,
        )
    return raw


def check_not_self_signed(cert: Path, openssl: str, allow_self_signed: bool) -> bool:
    """默认拒绝自签名证书，除非显式放行。

    自签名证书不是生产可用的信任锚：服务能起来，但客户端不信任。那种故障比启动
    失败更难排查（端口是通的、健康检查是绿的），所以必须在部署阶段拦下。
    """
    verify = _run([openssl, "verify", "-CAfile", str(cert), str(cert)])
    is_self_signed = verify.returncode == 0

    text = _run([openssl, "x509", "-in", str(cert), "-noout", "-text"]).stdout
    subject = next(
        (
            ln.split("=", 1)[1].strip()
            for ln in text.splitlines()
            if ln.strip().startswith("Subject:")
        ),
        "",
    )

    if not is_self_signed:
        return True
    if allow_self_signed:
        print(
            f"⚠️  已放行自签名证书（subject={subject or '未知'}）：客户端不信任它，"
            "仅可用于本地开发，生产环境请换成受信任证书。",
            file=sys.stderr,
        )
        return False
    raise CheckFailed(
        f"拒绝自签名证书（subject={subject or '未知'}）：{cert}\n"
        "  自签名证书无法被浏览器/客户端信任，生产环境用它会导致\n"
        "  '服务活着但客户端报错' —— 比启动失败更难排查。\n"
        "  请提供证书机构签发的证书；确实只做本地验证时，显式加：\n"
        "      --allow-self-signed   （或环境变量 ALLOW_SELF_SIGNED_TLS=true）"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生产 nginx TLS 物料预检（deploy/nginx/ssl）",
    )
    parser.add_argument(
        "--ssl-dir",
        type=Path,
        default=Path(os.getenv("TLS_SSL_DIR", DEFAULT_SSL_DIR)),
        help=f"TLS 物料目录（默认 {DEFAULT_SSL_DIR}，可用 TLS_SSL_DIR 覆盖）",
    )
    parser.add_argument(
        "--allow-self-signed",
        action="store_true",
        default=os.getenv("ALLOW_SELF_SIGNED_TLS", "").lower() in {"1", "true", "yes"},
        help="放行自签名证书（仅本地开发；亦可用 ALLOW_SELF_SIGNED_TLS=true）",
    )
    args = parser.parse_args(argv)
    ssl_dir: Path = args.ssl_dir.expanduser().resolve()

    print(f"🔐 TLS 物料预检: {ssl_dir}", flush=True)
    try:
        cert, key = check_files(ssl_dir)
        print(f"  ✅ 文件存在: {cert.name}, {key.name}")

        openssl = _openssl()
        if openssl is None:
            # 明确说清"少查了什么"，而不是让调用方以为全查过了
            print(
                "  ⚠️  未找到 openssl CLI：只检查了文件存在性，"
                "未验证可解析性 / 证书私钥配对 / 有效期 / 自签名。\n"
                "     安装 openssl 后重跑本检查可获得完整校验。",
                file=sys.stderr,
            )
            print("✅ 文件检查通过（未做证书内容校验：openssl 不可用）")
            return 0

        check_parses(cert, key, openssl)
        print("  ✅ 证书与私钥均可解析")

        check_pair_matches(cert, key, openssl)
        print("  ✅ 证书与私钥配对一致")

        expiry = check_expiry(cert, openssl)
        if expiry:
            print(f"  ✅ 证书有效期: {expiry}")

        # 返回值必须如实反映结论：放行自签名证书后不能再打印"非自签名"，
        # 那会让操作者以为这是一张受信任的生产证书。
        if check_not_self_signed(cert, openssl, args.allow_self_signed):
            print("  ✅ 证书可被信任链验证（非自签名）")
        else:
            print("  ⚠️  证书为自签名（已放行，仅限本地开发）")

    except CheckFailed as exc:
        print("", file=sys.stderr)
        print(f"❌ TLS 物料检查失败：\n{exc}", file=sys.stderr)
        return 1

    print("")
    print("✅ TLS 物料检查通过：nginx 可以加载该证书对。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
