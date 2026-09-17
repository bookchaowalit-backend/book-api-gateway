"""Configuration and allow-list validation for the API Gateway pilot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit


DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    """Raised when gateway configuration would be unsafe or unusable."""


def _normalise_hosts(values: frozenset[str] | set[str] | tuple[str, ...] | list[str]) -> frozenset[str]:
    hosts = frozenset(value.strip().casefold() for value in values if value and value.strip())
    if not hosts:
        raise ConfigError("UPSTREAM_ALLOWED_HOSTS must contain at least one host")
    return hosts


def validate_upstream_base_url(value: str, allowed_hosts: frozenset[str]) -> str:
    """Validate an upstream URL and return a normalized form without a trailing slash."""

    if not isinstance(value, str) or not value.strip():
        raise ConfigError("UPSTREAM_BASE_URL must be a non-empty URL")
    value = value.strip()
    if any(char.isspace() or ord(char) == 0x7F for char in value):
        raise ConfigError("UPSTREAM_BASE_URL contains control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("UPSTREAM_BASE_URL has an invalid host or port") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ConfigError("UPSTREAM_BASE_URL must use http or https")
    if hostname is None:
        raise ConfigError("UPSTREAM_BASE_URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("UPSTREAM_BASE_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("UPSTREAM_BASE_URL must not contain a query or fragment")
    normalized_host = hostname.casefold()
    if normalized_host not in allowed_hosts:
        raise ConfigError("UPSTREAM_BASE_URL host is not in UPSTREAM_ALLOWED_HOSTS")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("UPSTREAM_BASE_URL port must be between 1 and 65535")
    # urlsplit drops the brackets from IPv6 hostnames; reconstructing from the
    # original netloc keeps valid IPv6 URLs intact while preserving the path.
    normalized = SplitResult(parsed.scheme.casefold(), parsed.netloc, parsed.path.rstrip("/"), "", "")
    return urlunsplit(normalized)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return parsed


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True)
class GatewayConfig:
    """Validated settings needed to start one gateway process."""

    api_token: str = ""
    upstream_base_url: str = "http://127.0.0.1:9000"
    upstream_timeout_seconds: float = 2.0
    rate_limit_per_minute: int = 60
    max_body_bytes: int = 1_048_576
    allowed_hosts: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_HOSTS)

    def __post_init__(self) -> None:
        if not isinstance(self.api_token, str):
            raise ConfigError("BOOK_API_GATEWAY_TOKEN must be a string")
        if len(self.api_token) > 4096:
            raise ConfigError("BOOK_API_GATEWAY_TOKEN is too long")
        if not isinstance(self.upstream_timeout_seconds, (int, float)) or self.upstream_timeout_seconds <= 0:
            raise ConfigError("UPSTREAM_TIMEOUT_SECONDS must be greater than zero")
        if not isinstance(self.rate_limit_per_minute, int) or self.rate_limit_per_minute <= 0:
            raise ConfigError("RATE_LIMIT_PER_MINUTE must be greater than zero")
        if not isinstance(self.max_body_bytes, int) or self.max_body_bytes <= 0:
            raise ConfigError("MAX_BODY_BYTES must be greater than zero")
        hosts = _normalise_hosts(self.allowed_hosts)
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(
            self,
            "upstream_base_url",
            validate_upstream_base_url(self.upstream_base_url, hosts),
        )

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        """Build settings from environment variables without printing secrets."""

        raw_hosts = os.getenv("UPSTREAM_ALLOWED_HOSTS", "")
        allowed_hosts = (
            _normalise_hosts(raw_hosts.split(",")) if raw_hosts.strip() else DEFAULT_ALLOWED_HOSTS
        )
        return cls(
            api_token=os.getenv("BOOK_API_GATEWAY_TOKEN", ""),
            upstream_base_url=os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:9000"),
            upstream_timeout_seconds=_env_float("UPSTREAM_TIMEOUT_SECONDS", 2.0),
            rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 60),
            max_body_bytes=_env_int("MAX_BODY_BYTES", 1_048_576),
            allowed_hosts=allowed_hosts,
        )
