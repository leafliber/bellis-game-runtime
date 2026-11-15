"""对调用方公开的无内容错误。"""

from enum import StrEnum


class ModelFailure(StrEnum):
    CLOSED = "closed"
    CLEANUP_PENDING = "cleanup-pending"
    MODEL_DENIED = "model-denied"
    PERMISSION_DENIED = "permission-denied"
    CREDENTIAL_MISSING = "credential-missing"
    CREDENTIAL_INVALID = "credential-invalid"
    CREDENTIAL_UNAVAILABLE = "credential-unavailable"
    UNSUPPORTED = "unsupported"
    CONCURRENCY_LIMIT = "concurrency-limit"
    RATE_LIMIT = "rate-limit"
    INVALID_REQUEST = "invalid-request"
    SIZE_LIMIT = "size-limit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    NETWORK = "network"
    HTTP = "http"
    PROTOCOL = "protocol"
    PROVIDER = "provider"
    INVALID_OUTPUT = "invalid-output"


class ModelCallError(Exception):
    def __init__(self, reason: ModelFailure, *, http_status: int | None = None) -> None:
        self.reason = reason
        self.http_status = http_status
        status = f"，HTTP {http_status}" if http_status is not None else ""
        super().__init__(f"模型调用失败（{reason}{status}）")
