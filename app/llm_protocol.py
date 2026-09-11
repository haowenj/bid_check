"""Provider-specific request options for OpenAI-compatible LLM endpoints."""

from __future__ import annotations

from typing import Any

DEFAULT_LLM_PROVIDER = "dashscope"
SUPPORTED_LLM_PROVIDERS = frozenset({"dashscope", "vllm"})


def normalize_llm_provider(value: str | None) -> str:
    """Normalize and validate the configured LLM provider name."""

    provider = (value or "").strip().lower() or DEFAULT_LLM_PROVIDER
    if provider not in SUPPORTED_LLM_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_LLM_PROVIDERS))
        raise ValueError(
            f"Unsupported LLM provider {provider!r}; expected one of: {supported}"
        )
    return provider


def build_thinking_params(
    provider: str | None,
    enable_thinking: bool,
) -> dict[str, Any]:
    """Return the thinking-control fields expected by the selected provider.

    DashScope consumes ``enable_thinking`` at the request top level.  vLLM's
    Qwen chat template consumes the same flag inside ``chat_template_kwargs``.
    """

    normalized_provider = normalize_llm_provider(provider)
    if normalized_provider == "dashscope":
        return {"enable_thinking": bool(enable_thinking)}
    return {"chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}
