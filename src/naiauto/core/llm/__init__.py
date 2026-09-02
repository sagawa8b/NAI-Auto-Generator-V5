"""로컬 LLM(LM Studio) 연동 — 자연어 프롬프트 생성.

Qt와 무관한 코어. UI(`ui/assistant_dialog.py`)는 여기의 순수 로직을 백그라운드
스레드에서 호출한다. WD14 자동 태깅(`core/wd14_tagger.py`) 경로와 같은 구조다:
가용성 판정(`runtime_error`) → 무거운 작업 본체(`generate`) → 결과를 프롬프트에
반영하는 순수 함수(`apply_generated_prompt`).
"""

from __future__ import annotations

from .lmstudio_client import (
    DEFAULT_HOST,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TIMEOUT,
    LENGTH_LONG,
    LENGTH_MEDIUM,
    LENGTH_SHORT,
    OUTPUT_LENGTHS,
    PROMPT_STYLES,
    STYLE_DANBOORU,
    STYLE_NATURAL,
    SYSTEM_PROMPT_DANBOORU,
    SYSTEM_PROMPT_NATURAL,
    CancelCheck,
    LMStudioCancelled,
    LMStudioConfig,
    LMStudioConnectionError,
    LMStudioError,
    LMStudioNoModelError,
    LMStudioNotInstalled,
    LMStudioPromptGenerator,
    LMStudioResponseError,
    LMStudioTimeoutError,
    LMStudioVisionUnsupported,
    PromptResult,
    max_tokens_for_length,
    runtime_error,
    system_prompt_for_style,
)
from .parsing import parse_prompt_result
from .prompt_apply import apply_generated_prompt

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TIMEOUT",
    "LENGTH_LONG",
    "LENGTH_MEDIUM",
    "LENGTH_SHORT",
    "OUTPUT_LENGTHS",
    "PROMPT_STYLES",
    "STYLE_DANBOORU",
    "STYLE_NATURAL",
    "SYSTEM_PROMPT_DANBOORU",
    "SYSTEM_PROMPT_NATURAL",
    "CancelCheck",
    "LMStudioCancelled",
    "LMStudioConfig",
    "LMStudioConnectionError",
    "LMStudioError",
    "LMStudioNoModelError",
    "LMStudioNotInstalled",
    "LMStudioPromptGenerator",
    "LMStudioResponseError",
    "LMStudioTimeoutError",
    "LMStudioVisionUnsupported",
    "PromptResult",
    "apply_generated_prompt",
    "max_tokens_for_length",
    "parse_prompt_result",
    "runtime_error",
    "system_prompt_for_style",
]
