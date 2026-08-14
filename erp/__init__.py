"""金蝶 ERP 模块（v3.4: 输入净化防 SQL 注入）"""

import re
from abc import ABC, abstractmethod
from typing import Any


def sanitize_erp_input(value: str, max_length: int = 100) -> str:
    """v3.4: 净化 ERP 查询输入，防止 SQL 注入 / FilterString 注入
    白名单策略：仅允许字母数字、中文、空格和少量安全标点。
    单引号转义为双单引号（SQL 标准）。
    """
    if not value:
        return ""
    # 白名单：仅保留安全字符
    cleaned = re.sub(r"[^\w\s一-鿿.,+]", "", value)
    # SQL 标准转义单引号
    cleaned = cleaned.replace("'", "''")
    # 移除多余空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length]


class KingdeeAdapterBase(ABC):
    """金蝶 ERP 适配器基类"""

    @abstractmethod
    async def query_product(self, keyword: str) -> list[dict[str, Any]]:
        """查询商品信息"""

    @abstractmethod
    async def query_inventory(
        self, product_id: str = "", keyword: str = ""
    ) -> list[dict[str, Any]]:
        """查询库存余量"""

    @abstractmethod
    async def query_order(self, order_id: str = "", customer_id: str = "") -> list[dict[str, Any]]:
        """查询订单状态"""

    @abstractmethod
    async def query_customer(self, customer_id: str) -> dict[str, Any] | None:
        """查询客户资料"""

    async def get_order_owner(self, order_id: str) -> str | None:
        """P0-03: 最小归属元数据查询 — 仅返回订单归属的 customer_id，不取完整
        订单正文（customer_name / total / tracking 等敏感字段）。

        供 ErpAuthorizationService 在披露完整 payload 前做 ownership 判定
        （AUTHZ-6 / Section 10）：拒绝时完整订单 payload 永不进入 authz /
        Agent / LLM 内存。

        默认 fail-closed（返回 None）：子类应覆盖以提供最小 metadata 查询；
        未覆盖时授权服务一律拒绝，绝不泄露完整订单正文。
        订单不存在、查询失败或未实现时均返回 None。
        """
        return None

    async def resolve_customer_by_user(self, user_id: str | int | None) -> str | None:
        """P0-03: 将可信的 authenticated user_id 解析为 ERP customer_id。

        默认 fail-closed（返回 None）：当适配器未提供可信 user→customer 映射时，
        ErpAuthorizationService 不会授权任何私人 ERP 资源。
        映射必须来自服务端权威数据（ERP 客户记录 / 认证系统绑定），
        不得来自 prompt、LLM 或被查询资源自身的声明。

        子类应在拥有权威映射时覆盖此方法。
        """
        return None
