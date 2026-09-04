"""생성 결과 요약 문자열 — 결과 이미지 위 오버레이가 쓴다 (Qt-free).

V4의 `prettify_naidict`에 해당한다. 다른 점은 두 가지다. 읽는 대상이 저장된 PNG의
메타데이터 dict가 아니라 **실제로 보낸 요청**(`GenerationRequest`)이라 와일드카드가
전개된 뒤의 값이 그대로 나오고, 라벨 문자열은 UI가 번역해서 넘긴다 — core에
i18n 매니저를 끌어들이지 않기 위해서다 (`compose_ai_summary`와 같은 방식).
"""

from __future__ import annotations

from dataclasses import dataclass

from .api.models import GenerationRequest

#: 값이 여러 개 붙는 줄(모델 · 크기 · 시드)의 구분자.
FIELD_SEPARATOR = " · "


@dataclass(frozen=True)
class ResultLabels:
    """오버레이에 쓰는 번역된 라벨 모음. UI가 `i18n.get_text`로 채워 넘긴다."""

    prompt: str
    negative: str
    character_n: str  # "캐릭터 {}" — `str.format`으로 번호가 들어간다
    character_negative: str
    position_auto: str
    model: str
    size: str
    seed: str
    steps: str
    scale: str
    rescale: str
    sampler: str
    scheduler: str


def _number(value: float) -> str:
    """5.0 → "5", 0.45 → "0.45" (소수점이 지저분하게 남지 않도록)."""
    return f"{value:g}"


def _position(request: GenerationRequest, index: int, labels: ResultLabels) -> str:
    """캐릭터 좌표 표기. 좌표를 안 보내는 요청이면 "(자동)"."""
    if not request.use_coords:
        return f"({labels.position_auto})"
    character = request.characters[index]
    return f"({character.center_x:.2f}, {character.center_y:.2f})"


def compose_result_summary(request: GenerationRequest, labels: ResultLabels, *, model_name: str) -> str:
    """방금 생성한 이미지의 프롬프트와 설정을 여러 줄 문자열로 만든다.

    `model_name`은 UI가 ModelSpec에서 뽑은 표시용 이름이다 — request.model은
    레지스트리 key("naid5f")라 그대로 보여 주면 읽기 어렵다.
    """
    blocks: list[str] = [f"[{labels.prompt}]\n{request.prompt}".rstrip()]
    if request.negative_prompt.strip():
        blocks.append(f"[{labels.negative}]\n{request.negative_prompt}".rstrip())

    for index, character in enumerate(request.characters):
        header = f"[{labels.character_n.format(index + 1)}] {_position(request, index, labels)}"
        lines = [header, character.prompt.strip()]
        if character.uc.strip():
            lines.append(f"{labels.character_negative}: {character.uc.strip()}")
        blocks.append("\n".join(line for line in lines if line))

    blocks.append(
        FIELD_SEPARATOR.join(
            (
                f"{labels.model}: {model_name}",
                f"{labels.size}: {request.width}×{request.height}",
                f"{labels.seed}: {request.seed}",
            )
        )
    )
    blocks.append(
        FIELD_SEPARATOR.join(
            (
                f"{labels.steps}: {request.steps}",
                f"{labels.scale}: {_number(request.cfg_scale)}",
                f"{labels.rescale}: {_number(request.cfg_rescale)}",
            )
        )
    )
    blocks.append(
        FIELD_SEPARATOR.join(
            (
                f"{labels.sampler}: {request.sampler}",
                f"{labels.scheduler}: {request.scheduler}",
            )
        )
    )
    return "\n\n".join(blocks)


__all__ = ["FIELD_SEPARATOR", "ResultLabels", "compose_result_summary"]
