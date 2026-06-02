"""
ERP 适配器工厂（v3.0）
根据 ERP_MODE 环境变量自动选择 Mock 或真实适配器
包含配置校验与诊断日志
"""
from config import ERP_MODE, ERP_BASE_URL, ERP_APP_ID, ERP_APP_SECRET, ERP_DB_ID
from logger import get_logger

logger = get_logger("erp.factory")

# 必填配置项与说明
_REAL_REQUIRED_FIELDS = {
    "ERP_BASE_URL": ("金蝶 Cloud API 地址", ERP_BASE_URL),
    "ERP_APP_ID": ("应用 ID", ERP_APP_ID),
    "ERP_APP_SECRET": ("应用密钥", ERP_APP_SECRET),
    "ERP_DB_ID": ("账套 ID", ERP_DB_ID),
}


def _validate_real_config() -> list:
    """校验真实 ERP 模式的必填配置项，返回缺失项列表"""
    missing = []
    for field_name, (desc, value) in _REAL_REQUIRED_FIELDS.items():
        if not value or not value.strip():
            missing.append(f"{field_name}（{desc}）")
    return missing


def create_erp_adapter():
    """
    ERP 适配器工厂
    ERP_MODE=mock → KingdeeMockAdapter（模拟数据，无需额外配置）
    ERP_MODE=real → KingdeeRealAdapter（真实金蝶 API，需配置 ERP_BASE_URL / APP_ID / APP_SECRET / DB_ID）

    配置不完整时自动降级到 mock 模式并输出诊断日志。
    """
    if ERP_MODE == "real":
        # 校验必填配置
        missing = _validate_real_config()
        if missing:
            logger.warning(
                f"ERP_MODE=real 但缺少必填配置: {', '.join(missing)}。"
                f"自动降级到 mock 模式。请在 .env 或环境变量中配置以上项。"
            )
        else:
            try:
                from erp.kingdee_real_adapter import KingdeeRealAdapter
                adapter = KingdeeRealAdapter(
                    base_url=ERP_BASE_URL,
                    app_id=ERP_APP_ID,
                    app_secret=ERP_APP_SECRET,
                    db_id=ERP_DB_ID,
                )
                logger.info(
                    f"ERP 适配器: real mode | "
                    f"base_url=*** | "
                    f"app_id=*** | "
                    f"db_id=***"
                )
                return adapter
            except Exception as e:
                logger.warning(f"真实 ERP 适配器加载失败，降级到 mock: {e}")

    if ERP_MODE not in ("mock", "real"):
        logger.warning(f"未知 ERP_MODE='{ERP_MODE}'，仅支持 'mock' 或 'real'，使用 mock 模式")

    from erp.kingdee_adapter import KingdeeMockAdapter
    logger.info("ERP 适配器: mock mode（模拟数据，不连接真实 ERP）")
    return KingdeeMockAdapter()
