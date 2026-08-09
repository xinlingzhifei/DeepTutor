"""Math animator agents and pipeline."""

from importlib import import_module
from typing import Any

__all__ = [
    "MathAnimatorPipeline",
    "MathAnimatorRequestConfig",
    "validate_math_animator_request_config",
]


def __getattr__(name: str) -> Any:
    if name == "MathAnimatorPipeline":
        return getattr(import_module(f"{__name__}.pipeline"), name)
    if name in {"MathAnimatorRequestConfig", "validate_math_animator_request_config"}:
        return getattr(import_module(f"{__name__}.request_config"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
