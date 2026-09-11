"""LLM 시스템 프롬프트 세트 — 항목별로 쪼개 JSON 파일로 빼낸다.

프롬프트 어시스턴트가 LM Studio에 보내는 지시문은 지금까지 `lmstudio_client.py`의
상수로 박혀 있었다. 모델마다 잘 듣는 문구가 달라서 사용자가 직접 손보고 싶은 부분인데,
코드를 고치지 않으면 바꿀 수 없었다. 이 모듈은 그 지시문을 **항목별로** 나눠 JSON 한
파일로 만들고, 사용자가 메모장 같은 외부 편집기로 고칠 수 있게 한다.

항목 (JSON 최상위 키):

- `common_rules` — 두 스타일 공통 출력 규약 (JSON으로만 답하기 등)
- `styles.<스타일>.system_prompt` — 스타일별 역할 지시 (`natural` / `danbooru`)
- `styles.<스타일>.output_kind` — 사용자 메시지의 `{kind}` 자리에 들어갈 말
- `assistant_instruction` — 어시스턴트(이미지+지시 변형) 모드에서 덧붙는 지시
- `length_hints.<길이>` — 출력 길이(짧게/중간/길게) 분량 지시
- `user_messages.<상황>` — 시스템 프롬프트와 함께 보내는 사용자 메시지 틀

**부분 파일이 정상이다.** 고치고 싶은 항목만 적으면 나머지는 내장 기본값이 채운다
(`merge_prompt_config`). 그래서 새 항목이 생겨도 예전 파일이 깨지지 않는다.

Qt·SDK와 무관한 순수 로직이라 core에 둔다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_CONFIG_FILENAME",
    "LENGTH_KEYS",
    "MESSAGE_KEYS",
    "STYLE_KEYS",
    "PromptConfig",
    "PromptConfigError",
    "StylePrompt",
    "config_to_dict",
    "default_prompt_config",
    "default_prompt_config_path",
    "load_prompt_config",
    "load_prompt_config_or_default",
    "merge_prompt_config",
    "write_prompt_config",
]

#: 파일 형식 버전. 지금은 기록용이다 — 부분 파일을 기본값과 합치므로 버전이 달라도 읽는다.
CONFIG_VERSION = 1

#: 기본 파일 이름 (설정에서 경로를 비워 두면 이 이름을 앱 데이터 폴더에서 찾는다).
DEFAULT_CONFIG_FILENAME = "llm_prompts.json"

#: 스타일 키 — `lmstudio_client.STYLE_NATURAL` / `STYLE_DANBOORU`와 같은 문자열.
#: 상수를 여기서 다시 적는 이유는 순환 import를 피하기 위해서다 (client가 이 모듈을 쓴다).
STYLE_KEYS = ("natural", "danbooru")

#: 길이 키 — `lmstudio_client.LENGTH_*`와 같은 문자열.
LENGTH_KEYS = ("short", "medium", "long")

#: 사용자 메시지 상황 키. `<모드>_<입력 조합>`.
MESSAGE_KEYS = (
    "text_only",  # 태거: 텍스트만
    "image_only",  # 태거: 이미지만
    "image_with_text",  # 태거: 이미지 + 의도 텍스트
    "assistant_text_only",  # 어시스턴트: 이미지 없이 지시만
    "assistant_image_only",  # 어시스턴트: 지시 없이 이미지만
    "assistant_image_with_text",  # 어시스턴트: 이미지 + 변형 지시
)


class PromptConfigError(Exception):
    """프롬프트 세트 파일을 읽지 못했다 (없음/JSON 오류/타입 오류). 메시지는 사용자에게 보인다."""


@dataclass(frozen=True)
class StylePrompt:
    """스타일 하나의 지시문."""

    system_prompt: str
    output_kind: str


@dataclass(frozen=True)
class PromptConfig:
    """LLM에 보내는 지시문 한 벌. 항목이 비어 있으면 그 항목만 기본값이 채운다."""

    common_rules: str
    styles: dict[str, StylePrompt]
    assistant_instruction: str
    length_hints: dict[str, str]
    user_messages: dict[str, str]
    #: 사용자가 붙인 이름 (파일을 여러 벌 만들어 쓸 때 어느 것인지 알아보라고).
    name: str = ""
    #: 이 설정이 어느 파일에서 왔는지. 내장 기본값이면 None.
    source: Path | None = field(default=None, compare=False)

    def style(self, style: str) -> StylePrompt:
        """스타일 지시문. 모르는 스타일은 자연어로 본다 (기존 동작과 같다)."""
        found = self.styles.get(style)
        if found is not None:
            return found
        return self.styles.get(STYLE_KEYS[0], _DEFAULT_STYLES[STYLE_KEYS[0]])

    def system_prompt(self, style: str, *, assistant: bool = False, length: str = "medium") -> str:
        """스타일 지시 + 공통 규약 (+ 어시스턴트 지시) (+ 길이 지시)를 이어 붙인다.

        비어 있는 항목은 건너뛴다 — 사용자가 어떤 항목을 통째로 지우는 것도 유효한 편집이다.
        """
        parts = [self.style(style).system_prompt, self.common_rules]
        if assistant:
            parts.append(self.assistant_instruction)
        parts.append(self.length_hints.get(length, ""))
        return "\n\n".join(part.strip() for part in parts if part and part.strip())

    def user_message(self, key: str, *, kind: str, text: str) -> str:
        """사용자 메시지 틀에 `{kind}` / `{text}`를 넣는다. 모르는 키는 빈 문자열."""
        template = self.user_messages.get(key, "")
        return _fill(template, kind=kind, text=text)


def _fill(template: str, *, kind: str, text: str) -> str:
    """`{kind}` / `{text}`만 바꾼다.

    `str.format`을 쓰지 않는 이유: 사용자가 손으로 쓴 문장에 중괄호가 하나라도 섞이면
    (예: JSON 예시 `{"prompt": ...}`) `format`이 예외를 던지거나 엉뚱하게 지운다.
    치환은 프롬프트 생성 도중에 일어나므로 **절대 실패하면 안 된다**.
    """
    return template.replace("{kind}", kind).replace("{text}", text).strip()


# ── 내장 기본값 ──────────────────────────────────────────────
#
# 문안은 `lmstudio_client`가 쓰던 상수와 같다. 여기서 스타일 본문과 공통 규약을 나눠 둔
# 이유는 사용자가 둘 중 한쪽만 고칠 수 있게 하기 위해서다 (합칠 때 같은 문장이 된다).

DEFAULT_COMMON_RULES = (
    'Respond ONLY with a JSON object of the form {"prompt": "...", "negative_prompt": "..."} '
    'and nothing else. Put the main description in "prompt". Use "negative_prompt" only for '
    "things to avoid (leave it an empty string if you have no suggestions). Do not add "
    "explanations, headings, or code fences. "
    "When an image is provided, base the prompt on what you actually see in it; when text is also "
    "provided, treat that text as the user's intent and let it steer the result."
)

DEFAULT_STYLE_NATURAL = (
    "You are an assistant that writes image-generation prompts for NovelAI Diffusion V5. "
    "V5 has a strong natural-language text encoder, so write the prompt as vivid, flowing "
    "natural-language sentences that describe the subject, appearance, action, composition, "
    "setting, lighting and mood. "
    "Do NOT output comma-separated Danbooru tags (e.g. do not write things like "
    "'1girl, solo, long hair'); write real descriptive prose instead."
)

DEFAULT_STYLE_DANBOORU = (
    "You are an assistant that writes image-generation prompts as Danbooru-style tags. "
    "Output ONLY lowercase, comma-separated Danbooru tags (e.g. '1girl, solo, long hair, "
    "school uniform, classroom, sitting, looking at viewer'). "
    "Use tags for subject count, appearance, clothing, pose, expression, setting and style. "
    "Do NOT write natural-language sentences and do not add articles or punctuation other than "
    "the commas separating tags."
)

DEFAULT_ASSISTANT_INSTRUCTION = (
    "IMPORTANT — assistant/transform mode: an image is provided together with a text "
    "instruction. Do not simply describe the image as-is. Apply the changes the instruction "
    "asks for (for example replacing an outfit, changing the pose or camera angle, swapping the "
    "background or time of day) and write the prompt for the RESULTING, modified image. Keep "
    "everything the instruction does not mention faithful to the original image. Follow the "
    "output style stated above (natural-language sentences or Danbooru tags)."
)

DEFAULT_LENGTH_HINTS = {
    "short": (
        "Length: keep it short and concise — roughly 200 characters or fewer, only the most "
        "important elements. Still finish as one complete, self-contained prompt."
    ),
    "medium": (
        "Length: keep it moderate — roughly 200 to 500 characters, covering the key elements "
        "without going overboard. Finish as one complete prompt."
    ),
    "long": (
        "Length: be thorough and detailed — you may use up to roughly 1000 characters, covering "
        "subject, appearance, action, composition, setting, lighting and mood."
    ),
}

DEFAULT_USER_MESSAGES = {
    "text_only": "Write {kind} for a NovelAI prompt for: {text}",
    "image_only": "Write {kind} for a NovelAI prompt based on this image.",
    "image_with_text": (
        "Using this image and the following intent, write {kind} for a NovelAI prompt: {text}"
    ),
    "assistant_text_only": "Write {kind} for a NovelAI prompt for: {text}",
    "assistant_image_only": "Write {kind} for a NovelAI prompt based on this image.",
    "assistant_image_with_text": (
        "Apply this change to the image and write {kind} for the modified image: {text}"
    ),
}

_DEFAULT_STYLES = {
    "natural": StylePrompt(
        system_prompt=DEFAULT_STYLE_NATURAL,
        output_kind="a natural-language description",
    ),
    "danbooru": StylePrompt(
        system_prompt=DEFAULT_STYLE_DANBOORU,
        output_kind="Danbooru tags",
    ),
}


def default_prompt_config() -> PromptConfig:
    """내장 기본 지시문 한 벌 (파일이 없을 때 쓰는 값이자, 부분 파일을 채우는 바탕)."""
    return PromptConfig(
        common_rules=DEFAULT_COMMON_RULES,
        styles=dict(_DEFAULT_STYLES),
        assistant_instruction=DEFAULT_ASSISTANT_INSTRUCTION,
        length_hints=dict(DEFAULT_LENGTH_HINTS),
        user_messages=dict(DEFAULT_USER_MESSAGES),
        name="",
        source=None,
    )


def default_prompt_config_path() -> Path:
    """설정에서 경로를 비워 뒀을 때 찾아보는 위치 (앱 데이터 폴더 안).

    `settings.schema` import는 함수 안에서 한다 — 이 모듈은 프롬프트 문안만 보려는
    곳에서도 읽히므로, 그때 pydantic/platformdirs까지 끌어올 이유가 없다.
    """
    from ..settings.schema import default_data_dir

    return default_data_dir() / DEFAULT_CONFIG_FILENAME


# ── 읽기 ─────────────────────────────────────────────────────


def merge_prompt_config(data: object, base: PromptConfig | None = None) -> PromptConfig:
    """JSON에서 읽은 dict를 기본값 위에 덮어쓴다. 적힌 항목만 반영한다.

    Raises
    ------
    PromptConfigError
        최상위가 오브젝트가 아니거나, 알려진 항목의 타입이 문자열/오브젝트가 아닐 때.
        (모르는 키는 조용히 무시한다 — 주석 대용 `_readme` 같은 것을 허용하기 위해서다.)
    """
    base = base or default_prompt_config()
    if not isinstance(data, dict):
        raise PromptConfigError("the top level of the file must be a JSON object ({ ... })")

    name = _opt_str(data, "name", base.name)
    common = _opt_str(data, "common_rules", base.common_rules)
    assistant = _opt_str(data, "assistant_instruction", base.assistant_instruction)

    styles = dict(base.styles)
    raw_styles = data.get("styles")
    if raw_styles is not None:
        if not isinstance(raw_styles, dict):
            raise PromptConfigError("'styles' must be a JSON object")
        for key, value in raw_styles.items():
            fallback = styles.get(str(key), _DEFAULT_STYLES.get(str(key)))
            styles[str(key)] = _merge_style(str(key), value, fallback)

    length_hints = dict(base.length_hints)
    length_hints.update(_str_map(data, "length_hints"))

    user_messages = dict(base.user_messages)
    user_messages.update(_str_map(data, "user_messages"))

    return PromptConfig(
        common_rules=common,
        styles=styles,
        assistant_instruction=assistant,
        length_hints=length_hints,
        user_messages=user_messages,
        name=name,
        source=base.source,
    )


def load_prompt_config(path: str | Path) -> PromptConfig:
    """JSON 파일을 읽어 기본값과 합친다.

    Raises
    ------
    PromptConfigError
        파일이 없거나, 읽을 수 없거나, JSON이 아니거나, 항목 타입이 틀렸을 때.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise PromptConfigError(f"file not found: {target}") from e
    except OSError as e:
        raise PromptConfigError(f"cannot read {target}: {e}") from e
    except UnicodeDecodeError as e:
        raise PromptConfigError(f"{target} is not UTF-8 text: {e}") from e

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PromptConfigError(f"{target} is not valid JSON (line {e.lineno}: {e.msg})") from e

    merged = merge_prompt_config(data)
    return replace(merged, source=target)


def load_prompt_config_or_default(path: str | Path | None) -> tuple[PromptConfig, str]:
    """`(설정, 오류 문구)`. 경로가 비었으면 기본 위치를, 그것도 없으면 내장 기본값을 쓴다.

    프롬프트 생성이 파일 하나 때문에 막히면 안 되므로 **절대 예외를 던지지 않는다** —
    실패하면 내장 기본값과 함께 이유를 돌려주고, UI가 그 문구를 보여 준다.
    """
    text = str(path or "").strip()
    if not text:
        fallback = default_prompt_config_path()
        if not fallback.is_file():
            return default_prompt_config(), ""
        target: Path = fallback
    else:
        target = Path(text)

    try:
        return load_prompt_config(target), ""
    except PromptConfigError as e:
        logger.warning("falling back to built-in LLM prompts: %s", e)
        return default_prompt_config(), str(e)


# ── 쓰기 (편집용 템플릿) ──────────────────────────────────────


def config_to_dict(config: PromptConfig) -> dict:
    """JSON으로 쓸 수 있는 dict. 키 순서는 편집하기 좋은 순서로 고정한다."""
    return {
        "version": CONFIG_VERSION,
        "name": config.name,
        "common_rules": config.common_rules,
        "styles": {
            key: {"system_prompt": style.system_prompt, "output_kind": style.output_kind}
            for key, style in config.styles.items()
        },
        "assistant_instruction": config.assistant_instruction,
        "length_hints": dict(config.length_hints),
        "user_messages": dict(config.user_messages),
    }


#: 파일 맨 위에 넣는 사용법 (JSON에는 주석이 없으므로 `_readme` 키로 적는다).
_README = [
    "NAI-Auto-V5 prompt assistant — system prompts sent to the local LLM (LM Studio).",
    "Edit any value with a text editor; keys you delete fall back to the built-in default.",
    "styles.<natural|danbooru>.system_prompt = role instruction for that output style.",
    "styles.<...>.output_kind = the words used for {kind} in user_messages.",
    "common_rules = appended to every system prompt (JSON-only output contract).",
    "assistant_instruction = added in assistant (image + instruction) mode.",
    "length_hints.<short|medium|long> = how long the answer should be.",
    "user_messages.<...> = the user turn; {kind} and {text} are replaced, other braces are kept.",
]


def write_prompt_config(
    path: str | Path, config: PromptConfig | None = None, *, overwrite: bool = False
) -> Path:
    """편집용 JSON 파일을 만든다 (기본값 그대로 채워서).

    Parameters
    ----------
    path : 만들 파일 경로. 상위 폴더는 없으면 만든다.
    config : 쓸 내용. None이면 내장 기본값.
    overwrite : False면 이미 있는 파일을 덮어쓰지 않고 `PromptConfigError`.

    Raises
    ------
    PromptConfigError
        파일이 이미 있거나(overwrite=False), 쓰지 못했을 때.
    """
    target = Path(path)
    if target.exists() and not overwrite:
        raise PromptConfigError(f"file already exists: {target}")
    payload = {"_readme": _README}
    payload.update(config_to_dict(config or default_prompt_config()))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise PromptConfigError(f"cannot write {target}: {e}") from e
    return target


# ── 내부 ─────────────────────────────────────────────────────


def _opt_str(data: dict, key: str, fallback: str) -> str:
    """문자열 항목 하나. 없으면 기본값, 문자열이 아니면 오류."""
    if key not in data or data[key] is None:
        return fallback
    value = data[key]
    if not isinstance(value, str):
        raise PromptConfigError(f"'{key}' must be a string")
    return value


def _str_map(data: dict, key: str) -> dict[str, str]:
    """`{키: 문자열}` 오브젝트 항목. 없으면 빈 dict."""
    raw = data.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PromptConfigError(f"'{key}' must be a JSON object")
    result: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            raise PromptConfigError(f"'{key}.{name}' must be a string")
        result[str(name)] = value
    return result


def _merge_style(key: str, value: object, fallback: StylePrompt | None) -> StylePrompt:
    """스타일 하나를 합친다. 문자열만 적으면 `system_prompt`로 본다 (짧게 쓰고 싶을 때)."""
    base = fallback or StylePrompt(system_prompt="", output_kind="")
    if isinstance(value, str):
        return StylePrompt(system_prompt=value, output_kind=base.output_kind)
    if not isinstance(value, dict):
        raise PromptConfigError(f"'styles.{key}' must be a JSON object or a string")
    return StylePrompt(
        system_prompt=_opt_str(value, "system_prompt", base.system_prompt),
        output_kind=_opt_str(value, "output_kind", base.output_kind),
    )
