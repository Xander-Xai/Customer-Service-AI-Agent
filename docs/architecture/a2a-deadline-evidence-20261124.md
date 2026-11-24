# A2A deadline and evidence boundary (2026-11-24)

Earlier-employer customer service portfolio retrofit only. The SDK client now applies a single cooperative three-second deadline to discovery, RPC, artifact validation and cleanup, in addition to transport timeouts. Cancellation is propagated; no retry or substitute answer is generated. Cooperative cancellation is not a hard process kill or a remote cancellation acknowledgement.

The fictional specialist has a frozen topic-to-answer contract. A valid-looking synthetic prefix on the wrong topic is insufficient: exact answer matching is required for this demo. This is not a generic semantic verifier for real external agents. Existing opt-in, local endpoint, bearer, tool allowlist and no-write boundaries remain unchanged.

Validation: `pytest -q tests/unit/test_a2a_contract.py tests/integration/test_a2a_sdk_interop.py -o addopts=''` with the optional SDK installed. The new tests exercise wrong-topic rejection via the official SDK and a stalled discovery via a controlled async transport. Exact-head CI required; no production or real customer outcomes claimed.
