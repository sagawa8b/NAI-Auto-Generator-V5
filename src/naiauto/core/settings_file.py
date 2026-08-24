"""설정 파일 저장/불러오기 — V4의 `설정 저장(Ctrl+S)` / `설정 불러오기`에 대응한다.

프리셋(`core/presets.py`)과 다른 점은 **저장 위치**뿐이다. 프리셋은 이름을 붙여 관리
폴더에 두고, 이쪽은 사용자가 파일 대화상자로 고른 아무 경로에나 둔다. 그래서 값의 그릇은
`GenerationPreset`을 그대로 쓰고 범위 보정도 프리셋과 같은 로직을 재사용한다.

읽기는 두 형식을 모두 받는다:

- **V5** — 이 모듈이 쓴 형식. `{"format": "nai-auto-v5", "version": 1, ...}`
- **V4** — V4.5가 `.txt`로 저장하던 평평한 JSON. `format` 키가 없는 것으로 알아본다.
  키 이름이 일부 다르고(`scale` → `cfg_scale`) V5에 없는 항목도 있다.

V4 파일의 모델·샘플러가 V5에 없을 수 있는데, 그건 여기서 거르지 않는다 — 값을 그대로
넘기고 UI가 콤보에서 찾지 못하면 건너뛴다 (`MainWindow._on_preset_loaded`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .presets import GenerationPreset, PresetError, _clamp_preset_fields

#: 이 모듈이 쓴 파일임을 알아보는 표식.
FORMAT_KEY = "format"
FORMAT_NAME = "nai-auto-v5"
FORMAT_VERSION = 1

#: V4 키 → V5 `GenerationPreset` 필드. 나머지 V4 키(strength·noise·reference_* 등)는
#: V5의 생성 파라미터에 대응하는 것이 없어 버린다.
_V4_FIELD_MAP = {
    "prompt": "prompt",
    "negative_prompt": "negative_prompt",
    "width": "width",
    "height": "height",
    "steps": "steps",
    "sampler": "sampler",
    "model": "model",
    "scale": "cfg_scale",  # V4는 cfg_scale을 scale로 부른다
    "cfg_rescale": "cfg_rescale",
    "quality_toggle": "quality_tags",
    "variety_plus": "var_plus",
}


#: 기본값이 없어 파일이나 `defaults`가 반드시 채워 줘야 하는 항목.
_REQUIRED_FIELDS = frozenset(name for name, f in GenerationPreset.model_fields.items() if f.is_required())


@dataclass(frozen=True)
class LoadedSettings:
    """불러온 설정. `seed`는 `GenerationPreset`에 없어 따로 돌려준다."""

    preset: GenerationPreset
    seed: int | None
    #: 어느 형식에서 읽었는가 — UI가 "V4 파일을 읽었다"고 알릴 때 쓴다.
    is_v4: bool


def to_dict(preset: GenerationPreset, seed: int | None = None) -> dict[str, Any]:
    """V5 형식 dict. 파일에 그대로 json.dump 하면 된다."""
    data: dict[str, Any] = {
        FORMAT_KEY: FORMAT_NAME,
        "version": FORMAT_VERSION,
        **preset.model_dump(),
    }
    data.pop("name", None)  # 파일 이름이 곧 이름이다 — 안에 또 담지 않는다
    if seed is not None:
        data["seed"] = seed
    return data


def save(path: Path, preset: GenerationPreset, seed: int | None = None) -> None:
    """설정을 파일 하나로 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_dict(preset, seed), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_numbers(data: dict[str, Any]) -> dict[str, Any]:
    """V4는 위젯 문자열을 그대로 저장하기도 한다 (`"28"`, `"5.0"`)."""
    out = dict(data)
    for field_name in ("width", "height", "steps"):
        if field_name in out:
            coerced = _as_int(out[field_name])
            if coerced is not None:
                out[field_name] = coerced
    for field_name in ("cfg_scale", "cfg_rescale"):
        if field_name in out:
            try:
                out[field_name] = float(str(out[field_name]).strip())
            except (TypeError, ValueError):
                out.pop(field_name)
    for field_name in ("quality_tags", "var_plus"):
        if field_name in out and not isinstance(out[field_name], bool):
            out[field_name] = str(out[field_name]).strip().lower() == "true"
    return out


def _from_v4(data: dict[str, Any]) -> dict[str, Any]:
    """V4의 평평한 dict를 `GenerationPreset` 필드 이름으로 옮긴다."""
    return {v5: data[v4] for v4, v5 in _V4_FIELD_MAP.items() if v4 in data}


def load(path: Path, *, defaults: dict[str, Any] | None = None) -> LoadedSettings:
    """설정 파일을 읽는다 (V5·V4 모두). 읽을 수 없으면 `PresetError`.

    `defaults`는 V4 파일처럼 항목이 빠져 있을 때 채울 값이다 (보통 현재 모델의
    `ModelSpec.defaults`). 없으면 `GenerationPreset`의 기본값을 쓴다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PresetError(f"설정 파일을 읽을 수 없습니다: {path}") from e
    if not isinstance(raw, dict):
        raise PresetError(f"설정 파일 형식이 올바르지 않습니다: {path}")

    is_v4 = raw.get(FORMAT_KEY) != FORMAT_NAME
    fields = _from_v4(raw) if is_v4 else {k: v for k, v in raw.items() if k != FORMAT_KEY}
    fields.pop("version", None)

    seed = _as_int(raw.get("seed"))
    fields.pop("seed", None)

    merged = {**(defaults or {}), **_coerce_numbers(fields)}
    merged["name"] = ""  # 파일 기반이라 이름이 없다 (UI가 모델 스킵 판단에 쓰는 값과 무관)

    missing = sorted(_REQUIRED_FIELDS - merged.keys())
    if missing:
        # 오래된 V4 파일에는 `model`처럼 필수 항목이 아예 없다. pydantic 덤프를 그대로
        # 보여 주는 대신 무엇이 없는지 말한다 (호출부가 `defaults`로 채워 주면 여기 안 온다).
        raise PresetError(f"설정 파일에 필요한 항목이 없습니다: {', '.join(missing)}")

    try:
        preset = GenerationPreset(**_clamp_preset_fields(merged))
    except Exception as e:  # pydantic ValidationError 포함
        raise PresetError(f"설정 값이 올바르지 않습니다: {e}") from e

    return LoadedSettings(preset=preset, seed=seed, is_v4=is_v4)
