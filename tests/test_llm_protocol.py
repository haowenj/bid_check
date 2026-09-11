import pytest

from app.llm_protocol import build_thinking_params, normalize_llm_provider


def test_build_thinking_params_uses_dashscope_top_level_flag():
    assert build_thinking_params("dashscope", False) == {"enable_thinking": False}
    assert build_thinking_params("dashscope", True) == {"enable_thinking": True}


def test_build_thinking_params_uses_vllm_chat_template_kwargs():
    assert build_thinking_params("vllm", False) == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert build_thinking_params("vllm", True) == {
        "chat_template_kwargs": {"enable_thinking": True}
    }


def test_normalize_llm_provider_defaults_and_rejects_unknown_values():
    assert normalize_llm_provider(None) == "dashscope"
    assert normalize_llm_provider(" ") == "dashscope"
    assert normalize_llm_provider(" VLLM ") == "vllm"
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        normalize_llm_provider("unknown")
