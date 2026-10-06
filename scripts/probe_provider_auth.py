"""Run a bounded, secret-safe SiliconFlow authentication and model probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.provider_adapter import (  # noqa: E402
    ProviderAdapter,
    ProviderCredentialFormatError,
    normalize_api_key,
    probe_chat_completion,
    probe_provider_auth,
)

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "Qwen/Qwen3-8B"


def _credential_metadata(raw_key: str | None) -> dict[str, Any]:
    if raw_key is None:
        return {
            "credential_present": False,
            "credential_length": 0,
            "credential_sha256_8": None,
            "credential_source_env": "OPENAI_API_KEY",
            "alternate_siliconflow_key_present": bool(os.getenv("SILICONFLOW_API_KEY")),
        }
    return {
        "credential_present": bool(raw_key.strip()),
        "credential_length": len(raw_key),
        "credential_sha256_8": hashlib.sha256(raw_key.encode()).hexdigest()[:8],
        "credential_source_env": "OPENAI_API_KEY",
        "alternate_siliconflow_key_present": bool(os.getenv("SILICONFLOW_API_KEY")),
        "leading_whitespace": raw_key != raw_key.lstrip(),
        "trailing_whitespace": raw_key != raw_key.rstrip(),
        "bearer_prefix_present": raw_key.lower().startswith("bearer "),
        "embedded_quote_or_newline": any(char in raw_key for char in ('"', "'", "\r", "\n")),
    }


def _select_model(requested: str, model_ids: tuple[str, ...]) -> tuple[str | None, str]:
    if requested in model_ids:
        return requested, "requested_model_available"
    qwen_models = [model_id for model_id in model_ids if "qwen" in model_id.lower()]
    if qwen_models:
        return qwen_models[0], "requested_model_unavailable_selected_first_visible_qwen_model"
    if model_ids:
        return model_ids[0], "requested_model_unavailable_selected_first_visible_chat_model"
    return None, "no_chat_model_returned"


def run_probe() -> tuple[int, dict[str, Any]]:
    raw_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    requested_model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    proxy_vars = {
        name: bool(os.getenv(name)) for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    }
    proxy_involved = (
        "DIRECT_SILICONFLOW"
        if base_url.rstrip("/") == DEFAULT_BASE_URL
        else "PROXY_CHAIN_OR_CUSTOM_ENDPOINT"
    )

    result: dict[str, Any] = {
        "credential": _credential_metadata(raw_key),
        "base_url": base_url,
        "base_url_mode": proxy_involved,
        "proxy_environment_present": proxy_vars,
        "http_client_trust_env": False,
        "config_loading_note": "probe reads process environment only; core/config.py uses load_dotenv(override=True)",
        "requested_model": requested_model,
    }

    try:
        normalized_key = normalize_api_key(raw_key)
    except ProviderCredentialFormatError as exc:
        result["auth_probe"] = {
            "status_code": None,
            "authenticated": False,
            "error_category": "CREDENTIAL_MISSING"
            if raw_key is None or not raw_key.strip()
            else "CREDENTIAL_FORMAT_INVALID",
            "retryable": False,
            "attempt_count": 0,
            "trace_id": None,
            "diagnostic": str(exc),
        }
        result["decision"] = "BLOCKED_BY_CREDENTIAL"
        return 2, result

    auth = probe_provider_auth(api_key=raw_key, base_url=base_url)
    result["auth_probe"] = {
        "status_code": auth.status_code,
        "authenticated": auth.authenticated,
        "error_category": auth.error_category,
        "retryable": auth.retryable,
        "attempt_count": auth.attempt_count,
        "trace_id": auth.trace_id,
        "model_count": len(auth.model_ids),
    }
    if not auth.authenticated:
        result["decision"] = (
            "BLOCKED_BY_AUTHENTICATION" if auth.status_code == 401 else "STOP_BEFORE_CHAT"
        )
        return 2, result

    effective_model, selection_reason = _select_model(requested_model, auth.model_ids)
    result["model_discovery"] = {
        "requested_model": requested_model,
        "requested_model_available": requested_model in auth.model_ids,
        "effective_model": effective_model,
        "selection_reason": selection_reason,
    }
    if effective_model is None:
        result["decision"] = "BLOCKED_BY_MODEL_AVAILABILITY"
        return 2, result

    chat = probe_chat_completion(api_key=normalized_key, base_url=base_url, model=effective_model)
    result["minimal_completion"] = {
        "status_code": chat.status_code,
        "category": chat.category,
        "retryable": chat.retryable,
        "attempt_count": chat.attempt_count,
        "trace_id": chat.trace_id,
        "response_nonempty": chat.response_nonempty,
        "model": chat.model,
        "usage": chat.usage.to_dict() if chat.usage else None,
    }
    if chat.category != "CHAT_API_OK":
        result["decision"] = "STOP_BEFORE_STREAMING"
        return 2, result

    stream = ProviderAdapter(
        api_key=normalized_key,
        base_url=base_url,
        model=effective_model,
        timeout=15.0,
        max_attempts=1,
    ).stream_chat(
        [{"role": "user", "content": "Reply with OK."}],
        max_tokens=8,
        provider=os.getenv("LLM_PROVIDER", "siliconflow"),
    )
    result["streaming_probe"] = {
        "status": stream.final_status,
        "error_category": stream.error_category,
        "error_code": stream.error_code,
        "http_status": stream.http_status,
        "attempt_count": stream.attempt_count,
        "retry_count": stream.retry_count,
        "ttft_ms": stream.ttft_ms,
        "response_nonempty": stream.response_nonempty,
        "usage": stream.usage.to_dict(),
    }
    result["decision"] = (
        "READY_FOR_CONTROLLED_STAGING"
        if stream.final_status == "SUCCESS"
        else "STOP_BEFORE_STAGING"
    )
    return (0 if result["decision"] == "READY_FOR_CONTROLLED_STAGING" else 2), result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    code, result = run_probe()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
