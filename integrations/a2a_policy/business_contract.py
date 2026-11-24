"""After-sales policy specialist: operator-approved, version-pinned procedure rules.

All incoming fields are unverified issue descriptors, not ERP evidence.
No order IDs, money amounts, customer data, or approval authority cross A2A.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from integrations.a2a_policy.contracts import (
    A2AContractError,
    A2AIntegrationDisabled,
    validate_loopback_base_url,
)

ISSUES = ("damaged", "wrong_item", "return_request", "delivery_delay", "invoice")
CHANNELS = ("online", "store", "distributor", "unknown")
STAGES = ("not_received", "received_unopened", "received_opened", "unknown")
ROUTES = ("collect_evidence", "refer_policy_owner", "explain_procedure")
CARD_NAME = "After-sales Policy Specialist"
SKILL_ID = "after_sales_procedure_review"
KEYS = {"issue", "channel", "stage"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
SHA = re.compile(r"[0-9a-f]{64}")


def require_business_config() -> tuple[str, str, str]:
    if os.getenv("A2A_AFTERSALES_ENABLED", "").lower() != "true":
        raise A2AIntegrationDisabled("After-sales A2A is disabled")
    if os.getenv("APP_ENV", "").lower() not in {
        "development",
        "local",
        "test",
        "testing",
        "staging",
    }:
        raise A2AIntegrationDisabled("After-sales peer requires explicit local/staging environment")
    token = os.getenv("A2A_AFTERSALES_TOKEN", "")
    digest = os.getenv("A2A_AFTERSALES_POLICY_SHA256", "")
    if not 32 <= len(token) <= 256 or not token.isascii() or not SHA.fullmatch(digest):
        raise A2AIntegrationDisabled("Explicit peer token and approved policy digest required")
    return validate_loopback_base_url(os.getenv("A2A_AFTERSALES_ENDPOINT", "")), token, digest


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise A2AContractError("Duplicate policy field")
        result[key] = value
    return result


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def validate_request(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != KEYS:
        raise A2AContractError("Only issue/channel/stage descriptors are permitted")
    if (
        value["issue"] not in ISSUES
        or value["channel"] not in CHANNELS
        or value["stage"] not in STAGES
    ):
        raise A2AContractError("Unrecognized issue descriptor")
    return value


def parse_date(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise A2AContractError("Invalid policy effective date")
    return date.fromisoformat(value)


def validate_result(value: Any, request: dict, digest: str) -> dict:
    if not isinstance(value, dict):
        raise A2AContractError("Invalid policy artifact")
    common = {
        "request": request,
        "policy_sha256": digest,
        "descriptor_provenance": "UNVERIFIED_REPORTED_CONDITIONS",
        "execution_authorized": False,
        "refund_eligibility_verified": False,
    }
    if any(canonical(value.get(key)) != canonical(expected) for key, expected in common.items()):
        raise A2AContractError("Policy artifact identity or authorization mismatch")
    if (
        value.get("status") == "REVIEW_REQUIRED"
        and set(value) == set(common) | {"status", "reason"}
        and value["reason"] == "no_current_matching_rule"
    ):
        return value
    if value.get("status") != "PROCEDURE_FOUND" or set(value) != set(common) | {
        "status",
        "route",
        "procedure",
        "source_id",
        "revision",
        "valid_until",
    }:
        raise A2AContractError("Invalid policy artifact fields")
    if (
        value["route"] not in ROUTES
        or not isinstance(value["procedure"], str)
        or not 1 <= len(value["procedure"].strip()) <= 1200
        or any(
            not isinstance(value[key], str) or not ID.fullmatch(value[key])
            for key in ("source_id", "revision")
        )
        or parse_date(value["valid_until"]) < datetime.now(timezone.utc).date()
    ):
        raise A2AContractError("Invalid policy artifact values")
    return value


class PolicyCatalog:
    def __init__(self, path: Path, digest: str):
        if not SHA.fullmatch(digest):
            raise A2AContractError("Pinned policy digest required")
        with path.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise A2AContractError("Policy digest mismatch")
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict) or set(value) != {"schema", "rules"}:
            raise A2AContractError("Invalid policy envelope")
        rules = value["rules"]
        if (
            value["schema"] != "after-sales-procedures/v1"
            or not isinstance(rules, list)
            or not 1 <= len(rules) <= 500
        ):
            raise A2AContractError("Invalid rule population")
        self.digest = digest
        self.rules = {}
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != KEYS | {
                "route",
                "procedure",
                "source_id",
                "revision",
                "valid_from",
                "valid_until",
                "audience",
            }:
                raise A2AContractError("Invalid policy fields")
            validate_request({key: rule[key] for key in KEYS})
            if (
                rule["audience"] != "customer_public"
                or rule["route"] not in ROUTES
                or not isinstance(rule["procedure"], str)
                or not 1 <= len(rule["procedure"].strip()) <= 1200
                or any(
                    not isinstance(rule[key], str) or not ID.fullmatch(rule[key])
                    for key in ("source_id", "revision")
                )
                or parse_date(rule["valid_from"]) > parse_date(rule["valid_until"])
            ):
                raise A2AContractError("Invalid or non-public procedure")
            key = (rule["issue"], rule["channel"], rule["stage"])
            if key in self.rules:
                raise A2AContractError("Ambiguous policy rules")
            self.rules[key] = rule

    def evaluate(self, request: dict) -> dict:
        request = validate_request(request)
        result = {
            "request": request,
            "policy_sha256": self.digest,
            "descriptor_provenance": "UNVERIFIED_REPORTED_CONDITIONS",
            "execution_authorized": False,
            "refund_eligibility_verified": False,
        }
        rule = self.rules.get((request["issue"], request["channel"], request["stage"]))
        today = datetime.now(timezone.utc).date()
        if not rule or not parse_date(rule["valid_from"]) <= today <= parse_date(
            rule["valid_until"]
        ):
            return {**result, "status": "REVIEW_REQUIRED", "reason": "no_current_matching_rule"}
        return {
            **result,
            "status": "PROCEDURE_FOUND",
            **{
                key: rule[key]
                for key in ("route", "procedure", "source_id", "revision", "valid_until")
            },
        }
