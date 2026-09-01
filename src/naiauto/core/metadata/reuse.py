"""PNG 메타데이터 → 재사용 가능한 생성 설정 추출 (Qt-free).

naiinfo.read_metadata()의 관대한 dict를 UI가 그대로 적용할 수 있는
타입 값으로 정규화한다. 값이 없거나 형식이 다르면 그 필드만 None —
스키마가 바뀌어도 나머지 필드는 살아남는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..api.models import CharacterCaption, GenerationRequest


@dataclass(frozen=True)
class ReusableSettings:
    """PNG에서 복원한 생성 설정. None = 메타데이터에 없음(UI는 현재 값 유지)."""

    prompt: str | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    steps: int | None = None
    cfg_scale: float | None = None
    cfg_rescale: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    width: int | None = None
    height: int | None = None
    characters: tuple[CharacterCaption, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return (
            all(
                getattr(self, f) is None
                for f in (
                    "prompt",
                    "negative_prompt",
                    "seed",
                    "steps",
                    "cfg_scale",
                    "sampler",
                    "width",
                    "height",
                )
            )
            and not self.characters
        )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _center(entry: Any) -> tuple[float, float]:
    """V5 characterPrompts는 center={x,y}, V4 char_captions는 centers=[{x,y}]."""
    if isinstance(entry, dict):
        center = entry.get("center")
        if not isinstance(center, dict):
            centers = entry.get("centers")
            center = centers[0] if isinstance(centers, list) and centers else None
        if isinstance(center, dict):
            x = _as_float(center.get("x"))
            y = _as_float(center.get("y"))
            if x is not None and y is not None:
                return x, y
    return 0.5, 0.5


def _characters(comment: dict) -> tuple[CharacterCaption, ...]:
    # V5 웹 UI 형식이 가장 정확하다 (prompt/uc/center가 한 항목에 있음)
    entries = comment.get("characterPrompts")
    if isinstance(entries, list) and entries:
        captions = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            prompt = _as_str(entry.get("prompt"))
            if not prompt:
                continue
            if entry.get("enabled") is False:
                continue
            x, y = _center(entry)
            captions.append(
                CharacterCaption(prompt=prompt, uc=_as_str(entry.get("uc")) or "", center_x=x, center_y=y)
            )
        if captions:
            return tuple(captions)

    # 폴백: v4_prompt / v4_negative_prompt의 char_captions 쌍
    positive = _char_captions(comment.get("v4_prompt"))
    negative = _char_captions(comment.get("v4_negative_prompt"))
    captions = []
    for i, entry in enumerate(positive):
        prompt = _as_str(entry.get("char_caption"))
        if not prompt:
            continue
        uc = ""
        if i < len(negative):
            uc = _as_str(negative[i].get("char_caption")) or ""
        x, y = _center(entry)
        captions.append(CharacterCaption(prompt=prompt, uc=uc, center_x=x, center_y=y))
    return tuple(captions)


def _char_captions(node: Any) -> list[dict]:
    if isinstance(node, dict):
        caption = node.get("caption")
        if isinstance(caption, dict):
            entries = caption.get("char_captions")
            if isinstance(entries, list):
                return [e for e in entries if isinstance(e, dict)]
    return []


def extract_reusable(metadata: dict | None) -> ReusableSettings:
    """read_metadata() 결과에서 재사용 가능한 설정을 뽑는다."""
    if not metadata:
        return ReusableSettings()
    comment = metadata.get("comment")
    comment = comment if isinstance(comment, dict) else {}

    prompt = _as_str(comment.get("prompt")) or metadata.get("prompt")
    negative = _as_str(comment.get("uc")) or metadata.get("negative_prompt")

    return ReusableSettings(
        prompt=_as_str(prompt),
        negative_prompt=_as_str(negative),
        seed=_as_int(comment.get("seed")) if comment else _as_int(metadata.get("seed")),
        steps=_as_int(comment.get("steps")),
        cfg_scale=_as_float(comment.get("scale")),
        cfg_rescale=_as_float(comment.get("cfg_rescale")),
        sampler=_as_str(comment.get("sampler")),
        scheduler=_as_str(comment.get("noise_schedule")),
        width=_as_int(comment.get("width")),
        height=_as_int(comment.get("height")),
        characters=_characters(comment),
    )


def apply_to_request(request: GenerationRequest, settings: ReusableSettings) -> GenerationRequest:
    """복원한 설정을 요청에 얹는다 — None인 필드는 요청의 현재 값을 그대로 둔다.

    `apply_reusable()`(메인 윈도우)의 Qt-free 짝. 위젯을 거치지 않고 요청을 만들어야
    하는 곳(폴더 강화처럼 이미지마다 설정이 다른 배치)이 쓴다.

    부정 프롬프트는 **원문 그대로** 덮어쓴다. 메타데이터의 uc에는 UC 프리셋 텍스트가
    이미 합성되어 있어서, 앞에 프리셋을 또 붙이면 같은 문장이 두 번 들어간다.

    같은 이유로 프리셋 **식별자**도 "none"으로 내린다. 프리셋 텍스트가 이미 프롬프트
    안에 있는데 `ucPresetId`/`qualityPresetId`로 또 알리면 서버에 두 번 말하는 셈이고,
    웹 UI도 강화 요청에서 둘 다 "none"으로 보낸다
    (`spec/v5/captures/v5_enhance_*_webui.sanitized.json`). 위젯 경로의
    `apply_reusable()`도 UC 콤보를 none으로, 품질 태그 체크를 해제한다.
    """
    changes: dict[str, object] = {}
    for field_name, value in (
        ("prompt", settings.prompt),
        ("negative_prompt", settings.negative_prompt),
        ("seed", settings.seed),
        ("steps", settings.steps),
        ("cfg_scale", settings.cfg_scale),
        ("cfg_rescale", settings.cfg_rescale),
        ("sampler", settings.sampler),
        ("scheduler", settings.scheduler),
    ):
        if value is not None:
            changes[field_name] = value
    if settings.width and settings.height:
        changes["width"] = settings.width
        changes["height"] = settings.height
    if settings.negative_prompt is not None:
        changes["uc_preset_id"] = "none"
    if settings.prompt is not None:
        changes["quality_preset_id"] = "none"
    if settings.characters:
        changes["characters"] = settings.characters
    return replace(request, **changes)
