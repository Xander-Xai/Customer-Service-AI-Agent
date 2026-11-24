"""ERP adapter factory: explicit mode selection, no real-to-mock fallback."""

from core.config import ERP_APP_ID, ERP_APP_SECRET, ERP_BASE_URL, ERP_DB_ID, ERP_MODE
from core.logger import get_logger

logger = get_logger("erp.factory")


class ERPConfigurationError(RuntimeError):
    """The requested ERP adapter cannot be initialized safely."""


def _validate_real_config() -> list[str]:
    fields = {
        "ERP_BASE_URL": ERP_BASE_URL,
        "ERP_APP_ID": ERP_APP_ID,
        "ERP_APP_SECRET": ERP_APP_SECRET,
        "ERP_DB_ID": ERP_DB_ID,
    }
    return [name for name, value in fields.items() if not value or not value.strip()]


def create_erp_adapter():
    """Only an explicit mock mode may select synthetic data."""
    if ERP_MODE == "mock":
        from erp.kingdee_adapter import KingdeeMockAdapter

        logger.info("ERP adapter: mock mode (synthetic data only)")
        return KingdeeMockAdapter()

    if ERP_MODE != "real":
        raise ERPConfigurationError("Unsupported ERP_MODE; expected mock or real")

    missing = _validate_real_config()
    if missing:
        raise ERPConfigurationError("Missing real ERP configuration: " + ", ".join(missing))

    try:
        from erp.kingdee_real_adapter import KingdeeRealAdapter

        adapter = KingdeeRealAdapter(
            base_url=ERP_BASE_URL,
            app_id=ERP_APP_ID,
            app_secret=ERP_APP_SECRET,
            db_id=ERP_DB_ID,
        )
    except Exception as exc:
        logger.error("Real ERP adapter initialization failed: %s", type(exc).__name__)
        raise ERPConfigurationError("Real ERP adapter initialization failed") from None

    logger.info("ERP adapter: real mode")
    return adapter
