"""
金蝶 ERP 真实适配器验证工具（v3.0 新增）
用于对接金蝶 Cloud API 后的连通性测试和数据验证

使用方式:
  1. 确保 .env 中配置了 ERP_MODE=real 及相关凭证
  2. 运行: python erp/test_kingdee_real.py
  3. 或指定参数: python erp/test_kingdee_real.py --product 精华 --order ORD001 --customer C001

验证内容:
  - 连接认证（token 获取）
  - 商品信息查询
  - 库存余量查询
  - 订单状态查询
  - 客户资料查询
  - 错误处理与降级
"""
import os
import sys
import asyncio
import argparse
import time

# 确保项目根目录在 sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from logger import get_logger

logger = get_logger("erp.test")


class ERPRealAdapterValidator:
    """金蝶 ERP 真实适配器验证器"""

    def __init__(self, adapter):
        self.adapter = adapter
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.results = []

    def _record(self, name: str, status: str, detail: str = ""):
        self.results.append({"name": name, "status": status, "detail": detail})
        if status == "PASS":
            self.passed += 1
            print(f"    ✅ {name}")
        elif status == "FAIL":
            self.failed += 1
            print(f"    ❌ {name}: {detail}")
        elif status == "SKIP":
            self.skipped += 1
            print(f"    ⏭️  {name}: {detail}")

    async def test_connection(self):
        """验证金蝶 API 连接和认证"""
        print("\n[1] 连接认证测试")
        if not hasattr(self.adapter, '_ensure_token'):
            self._record("Token 获取", "SKIP", "Mock 适配器无认证接口")
            return
        try:
            start = time.time()
            await self.adapter._ensure_token()
            elapsed = time.time() - start
            if self.adapter._token:
                self._record("Token 获取", "PASS", f"耗时 {elapsed:.2f}s")
            else:
                self._record("Token 获取", "FAIL", "Token 为空")
        except Exception as e:
            self._record("Token 获取", "FAIL", str(e))

    async def test_query_product(self, keyword: str = "精华"):
        """验证商品信息查询"""
        print(f"\n[2] 商品信息查询 (keyword='{keyword}')")
        try:
            start = time.time()
            products = await self.adapter.query_product(keyword)
            elapsed = time.time() - start

            if products:
                self._record("商品查询", "PASS", f"返回 {len(products)} 条, 耗时 {elapsed:.2f}s")
                # 验证字段完整性
                required_fields = ["id", "name", "price"]
                for field in required_fields:
                    if field in products[0]:
                        self._record(f"商品字段[{field}]", "PASS")
                    else:
                        self._record(f"商品字段[{field}]", "FAIL", "字段缺失")
                # 打印示例
                p = products[0]
                print(f"    📦 示例: {p.get('name', 'N/A')} | ¥{p.get('price', 'N/A')} | {p.get('category', 'N/A')}")
            else:
                self._record("商品查询", "FAIL", "返回空列表")
        except Exception as e:
            self._record("商品查询", "FAIL", str(e))

    async def test_query_inventory(self, keyword: str = "精华"):
        """验证库存余量查询"""
        print(f"\n[3] 库存余量查询 (keyword='{keyword}')")
        try:
            start = time.time()
            inventory = await self.adapter.query_inventory(keyword=keyword)
            elapsed = time.time() - start

            if inventory:
                self._record("库存查询", "PASS", f"返回 {len(inventory)} 条, 耗时 {elapsed:.2f}s")
                required_fields = ["product_name", "stock", "warehouse"]
                for field in required_fields:
                    if field in inventory[0]:
                        self._record(f"库存字段[{field}]", "PASS")
                    else:
                        self._record(f"库存字段[{field}]", "FAIL", "字段缺失")
                inv = inventory[0]
                print(f"    📦 示例: {inv.get('product_name', 'N/A')} | 库存: {inv.get('stock', 'N/A')} | 仓库: {inv.get('warehouse', 'N/A')}")
            else:
                self._record("库存查询", "FAIL", "返回空列表")
        except Exception as e:
            self._record("库存查询", "FAIL", str(e))

    async def test_query_order(self, order_id: str = "", customer_id: str = ""):
        """验证订单状态查询"""
        query_desc = f"order_id='{order_id}'" if order_id else f"customer_id='{customer_id}'"
        print(f"\n[4] 订单状态查询 ({query_desc})")
        try:
            start = time.time()
            orders = await self.adapter.query_order(order_id=order_id, customer_id=customer_id)
            elapsed = time.time() - start

            if orders:
                self._record("订单查询", "PASS", f"返回 {len(orders)} 条, 耗时 {elapsed:.2f}s")
                required_fields = ["order_id", "status"]
                for field in required_fields:
                    if field in orders[0]:
                        self._record(f"订单字段[{field}]", "PASS")
                    else:
                        self._record(f"订单字段[{field}]", "FAIL", "字段缺失")
                o = orders[0]
                print(f"    📋 示例: {o.get('order_id', 'N/A')} | 状态: {o.get('status', 'N/A')} | 金额: ¥{o.get('total', 'N/A')}")
            else:
                self._record("订单查询", "FAIL", "返回空列表")
        except Exception as e:
            self._record("订单查询", "FAIL", str(e))

    async def test_query_customer(self, customer_id: str = "C001"):
        """验证客户资料查询"""
        print(f"\n[5] 客户资料查询 (customer_id='{customer_id}')")
        try:
            start = time.time()
            customer = await self.adapter.query_customer(customer_id)
            elapsed = time.time() - start

            if customer:
                self._record("客户查询", "PASS", f"耗时 {elapsed:.2f}s")
                required_fields = ["id", "name", "phone", "level"]
                for field in required_fields:
                    if field in customer:
                        self._record(f"客户字段[{field}]", "PASS")
                    else:
                        self._record(f"客户字段[{field}]", "FAIL", "字段缺失")
                print(f"    👤 示例: {customer.get('name', 'N/A')} | 等级: {customer.get('level', 'N/A')} | 电话: {customer.get('phone', 'N/A')}")
            else:
                self._record("客户查询", "FAIL", "返回 None（客户不存在？）")
        except Exception as e:
            self._record("客户查询", "FAIL", str(e))

    async def test_error_handling(self):
        """验证错误处理和降级"""
        print("\n[6] 错误处理与降级测试")
        try:
            # 查询不存在的商品
            products = await self.adapter.query_product("zzz_nonexistent_product_zzz")
            self._record("不存在商品查询", "PASS", f"返回 {len(products)} 条（应为 0）")
        except Exception as e:
            self._record("不存在商品查询", "FAIL", f"应返回空列表而非异常: {e}")

        try:
            # 查询不存在的客户
            customer = await self.adapter.query_customer("NONEXISTENT")
            if customer is None:
                self._record("不存在客户查询", "PASS")
            else:
                self._record("不存在客户查询", "FAIL", "应返回 None")
        except Exception as e:
            self._record("不存在客户查询", "FAIL", f"应返回 None 而非异常: {e}")

    async def test_connection_pool(self):
        """验证连接池复用"""
        print("\n[7] 连接池复用测试")
        if not hasattr(self.adapter, '_get_client'):
            self._record("连接池复用", "SKIP", "Mock 适配器无连接池接口")
            return
        try:
            client = await self.adapter._get_client()
            clients = []
            for _ in range(5):
                c = await self.adapter._get_client()
                clients.append(c)
            all_same = all(c is clients[0] for c in clients)
            if all_same:
                self._record("连接池复用", "PASS")
            else:
                self._record("连接池复用", "FAIL", "多次获取的 client 不一致")
        except Exception as e:
            self._record("连接池复用", "FAIL", str(e))

    async def test_concurrent_queries(self):
        """验证并发查询稳定性"""
        print("\n[8] 并发查询测试")
        try:
            start = time.time()
            results = await asyncio.gather(
                self.adapter.query_product("精华"),
                self.adapter.query_inventory(keyword="精华"),
                self.adapter.query_order(),
                self.adapter.query_customer("C001"),
                return_exceptions=True,
            )
            elapsed = time.time() - start

            errors = [r for r in results if isinstance(r, Exception)]
            if not errors:
                self._record("并发查询", "PASS", f"4 路并发, 耗时 {elapsed:.2f}s")
            else:
                self._record("并发查询", "FAIL", f"{len(errors)} 个异常: {errors[0]}")
        except Exception as e:
            self._record("并发查询", "FAIL", str(e))

    def print_summary(self):
        """打印测试摘要"""
        print("\n" + "=" * 60)
        print("金蝶 ERP 真实适配器验证报告")
        print("=" * 60)
        print(f"  通过: {self.passed}")
        print(f"  失败: {self.failed}")
        print(f"  跳过: {self.skipped}")
        print(f"  总计: {self.passed + self.failed + self.skipped}")
        print("=" * 60)

        if self.failed > 0:
            print("\n❌ 失败项:")
            for r in self.results:
                if r["status"] == "FAIL":
                    print(f"  - {r['name']}: {r['detail']}")

        return self.failed == 0


async def run_validation(args):
    """运行完整验证流程"""
    from config import ERP_MODE

    print("=" * 60)
    print("金蝶 ERP 真实适配器验证工具")
    print("=" * 60)
    print(f"  ERP_MODE = {ERP_MODE}")

    if ERP_MODE != "real":
        print("\n⚠️  当前 ERP_MODE 不是 'real'")
        print("   请在 .env 中设置 ERP_MODE=real 并配置相关凭证")
        print("   将使用 Mock 适配器进行功能验证...\n")
        from erp.kingdee_adapter import KingdeeMockAdapter
        adapter = KingdeeMockAdapter()
    else:
        from erp.factory import create_erp_adapter
        adapter = create_erp_adapter()
        print(f"  适配器类型: {type(adapter).__name__}")

    validator = ERPRealAdapterValidator(adapter)

    # 执行测试
    await validator.test_connection()
    await validator.test_query_product(args.product)
    await validator.test_query_inventory(args.product)
    await validator.test_query_order(order_id=args.order)
    await validator.test_query_customer(args.customer)
    await validator.test_error_handling()
    await validator.test_connection_pool()
    await validator.test_concurrent_queries()

    # 清理
    if hasattr(adapter, "close"):
        await adapter.close()

    success = validator.print_summary()
    return success


def main():
    parser = argparse.ArgumentParser(description="金蝶 ERP 真实适配器验证工具")
    parser.add_argument("--product", default="精华", help="商品查询关键词 (默认: 精华)")
    parser.add_argument("--order", default="", help="订单号 (默认: 空，查询所有)")
    parser.add_argument("--customer", default="C001", help="客户ID (默认: C001)")
    args = parser.parse_args()

    success = asyncio.run(run_validation(args))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
