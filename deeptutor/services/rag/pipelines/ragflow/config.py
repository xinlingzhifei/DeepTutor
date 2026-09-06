"""Validated connection settings; credentials are resolved by the owning service."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class RagflowConfig:
    base_url: str
    api_key: str = field(repr=False)
    connect_timeout: float = 5
    read_timeout: float = 60
    upload_timeout: float = 300
    allow_http: bool = False

    def __post_init__(self) -> None:
        invalid = "Invalid RAGFlow connection settings."
        if not isinstance(self.base_url, str) or re.search(r"[\s\\]", self.base_url):
            raise ValueError(invalid)
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
            allowed_schemes = ("https", "http") if self.allow_http is True else ("https",)
            if (
                parsed.scheme not in allowed_schemes
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or "?" in self.base_url
                or "#" in self.base_url
                or (port is not None and not 1 <= port <= 65535)
                or any(part in (".", "..") for part in unquote(parsed.path).split("/"))
                or "%" in parsed.netloc
                or "%" in parsed.path
            ):
                raise ValueError(invalid)
        except ValueError:
            raise ValueError(invalid) from None
        if not isinstance(self.api_key, str) or not re.fullmatch(r"[\x21-\x7e]+", self.api_key):
            raise ValueError(invalid)
        for timeout in (self.connect_timeout, self.read_timeout, self.upload_timeout):
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError(invalid)
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
