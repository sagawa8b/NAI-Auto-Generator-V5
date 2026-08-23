"""파라미터 검증 — 구 naia.py의 ParamSpec 테이블과 validate()를 통합 이식.

수치 범위/타입은 여기서, 모델·샘플러·스케줄러 선택지는 ModelSpec에서 검증한다.
반환은 위반 항목 문자열 목록 (빈 리스트 = 통과). 예외로 바꾸는 것은 호출자 몫.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api.models import GenerationRequest


@dataclass(frozen=True)
class ParamSpec:
    type: str  # "int", "float", "str", "bool", "bytes"
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[Any, ...] | None = None
    multiple_of: int | None = None
    description: str = ""


GENERATION_PARAMS: dict[str, ParamSpec] = {
    "action": ParamSpec(type="str", default="generate", choices=("generate", "img2img", "infill")),
    "width": ParamSpec(
        type="int",
        default=832,
        min=64,
        max=8192,
        step=64,
        multiple_of=64,
        description="생성 이미지 너비 (px)",
    ),
    "height": ParamSpec(
        type="int",
        default=1216,
        min=64,
        max=8192,
        step=64,
        multiple_of=64,
        description="생성 이미지 높이 (px)",
    ),
    "seed": ParamSpec(type="int", default=0, min=0, max=4294967295, description="시드. 0=랜덤"),
    "steps": ParamSpec(type="int", default=28, min=1, max=150, description="샘플링 스텝 수"),
    "cfg_scale": ParamSpec(type="float", default=5.0, min=0.0, max=30.0, description="CFG Scale"),
    "cfg_rescale": ParamSpec(type="float", default=0.4, min=0.0, max=1.0, description="CFG Rescale"),
    "strength": ParamSpec(
        type="float", default=0.5, min=0.01, max=0.99, description="디노이즈 강도 (i2i/inpaint)"
    ),
    "noise": ParamSpec(type="float", default=0.05, min=0.0, max=0.99, description="추가 노이즈 (i2i)"),
}

CHARACTER_CAPTION_PARAMS: dict[str, ParamSpec] = {
    "center_x": ParamSpec(
        type="float",
        default=0.5,
        min=0.01,
        max=0.99,
        # step removed — free positioning
        description="Free coordinate X on resolution-ratio canvas (0.01–0.99)",
    ),
    "center_y": ParamSpec(
        type="float",
        default=0.5,
        min=0.01,
        max=0.99,
        # step removed — free positioning
        description="Free coordinate Y on resolution-ratio canvas (0.01–0.99)",
    ),
}

VIBE_TRANSFER_PARAMS: dict[str, ParamSpec] = {
    "strength": ParamSpec(type="float", default=0.6, min=0.01, max=1.0),
    "information_extracted": ParamSpec(type="float", default=1.0, min=0.01, max=1.0),
}

CHARACTER_REFERENCE_PARAMS: dict[str, ParamSpec] = {
    "type": ParamSpec(
        type="str", default="character&style", choices=("character", "style", "character&style")
    ),
    "strength": ParamSpec(type="float", default=0.6, min=0.0, max=1.0),
    "fidelity": ParamSpec(type="float", default=1.0, min=0.0, max=1.0),
}


def validate(params: dict, spec: dict[str, ParamSpec]) -> list[str]:
    """값 딕셔너리를 스펙 테이블에 대해 검사. 위반 목록 반환."""
    errors = []
    for key, value in params.items():
        if key not in spec:
            continue
        s = spec[key]
        if s.choices and value not in s.choices:
            errors.append(f"{key}: {value!r} not in {list(s.choices)}")
        if isinstance(value, bool):
            continue
        if s.min is not None and isinstance(value, (int, float)) and value < s.min:
            errors.append(f"{key}: {value} < min({s.min})")
        if s.max is not None and isinstance(value, (int, float)) and value > s.max:
            errors.append(f"{key}: {value} > max({s.max})")
        if s.multiple_of and isinstance(value, int) and value % s.multiple_of != 0:
            errors.append(f"{key}: {value} is not a multiple of {s.multiple_of}")
    return errors


def validate_request(req: GenerationRequest) -> list[str]:
    """GenerationRequest 전체 검증 (모델/샘플러 선택지 제외 — ModelSpec에서)."""
    errors = validate(
        {
            "action": req.action,
            "width": req.width,
            "height": req.height,
            "seed": req.seed,
            "steps": req.steps,
            "cfg_scale": req.cfg_scale,
            "cfg_rescale": req.cfg_rescale,
            "strength": req.strength,
            "noise": req.noise,
        },
        GENERATION_PARAMS,
    )
    for i, c in enumerate(req.characters):
        errors += [
            f"characters[{i}].{e}"
            for e in validate({"center_x": c.center_x, "center_y": c.center_y}, CHARACTER_CAPTION_PARAMS)
        ]
    for i, v in enumerate(req.vibes):
        errors += [
            f"vibes[{i}].{e}"
            for e in validate(
                {"strength": v.strength, "information_extracted": v.information_extracted},
                VIBE_TRANSFER_PARAMS,
            )
        ]
    for i, r in enumerate(req.character_refs):
        errors += [
            f"character_refs[{i}].{e}"
            for e in validate(
                {"type": r.type, "strength": r.strength, "fidelity": r.fidelity}, CHARACTER_REFERENCE_PARAMS
            )
        ]
    if req.action == "img2img" and req.image is None:
        errors.append("image: img2img requires 'image'")
    if req.action == "infill" and (req.image is None or req.mask is None):
        errors.append("image/mask: infill requires 'image' and 'mask'")
    return errors
