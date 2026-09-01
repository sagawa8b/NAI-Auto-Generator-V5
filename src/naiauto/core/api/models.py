"""생성 요청/결과 스키마 — 전부 불변(frozen) 객체.

UI는 생성 시작 시점에 위젯 상태를 GenerationRequest로 1회 스냅숏하고,
워커/클라이언트는 이 객체만 읽는다. 요청 간 상태 누수가 구조적으로 불가능하다.
(V4 앱의 장수명 가변 parameters dict + del 방식의 재발 방지)
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

from PIL import Image

Action = Literal["generate", "img2img", "infill"]

_EMPTY_MAP: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class CharacterCaption:
    """V4+ 캐릭터 프롬프트. 위치 좌표와 함께 메인 프롬프트에 조합된다."""

    prompt: str
    uc: str = ""
    center_x: float = 0.5
    center_y: float = 0.5


@dataclass(frozen=True)
class VibeTransfer:
    """인코딩된 vibe 데이터로 스타일 전이. encoded는 encode-vibe API 결과."""

    encoded: str
    strength: float = 0.6
    information_extracted: float = 1.0


@dataclass(frozen=True)
class CharacterReference:
    """참조 이미지에서 캐릭터/스타일 추출 (V4.5 이상, ModelSpec.supports 참조)."""

    image: bytes
    type: Literal["character", "style", "character&style"] = "character&style"
    strength: float = 0.6
    fidelity: float = 1.0


@dataclass(frozen=True)
class GenerationRequest:
    """단일 생성 요청.

    - model은 ModelSpec 레지스트리의 key ("naid4.5f", "naid5f" 등)
    - extra_params는 최종 payload parameters에 마지막으로 병합되는 verbatim
      패스스루 — 아직 코드가 모르는 V5 신규 키를 즉시 실험할 수 있는 탈출구
    """

    action: Action = "generate"
    prompt: str = ""
    negative_prompt: str = ""
    width: int = 832
    height: int = 1216
    seed: int = 0
    steps: int = 28
    cfg_scale: float = 5.0
    cfg_rescale: float = 0.4
    sampler: str = "k_euler_ancestral"
    scheduler: str = "native"
    model: str = "naid4.5f"
    var_plus: bool = False

    # V5 전용 (V4 빌더는 무시): 서버에 전달되는 프리셋 식별자와 좌표 사용 플래그.
    # 캡처 기준 기본값 — ucPresetId="heavy", qualityPresetId="standard", use_coords=true
    uc_preset_id: str = "heavy"
    quality_preset_id: str = "standard"
    use_coords: bool = True

    image: bytes | None = None
    strength: float = 0.5
    noise: float = 0.05
    mask: bytes | None = None
    #: V5 Enhance의 "Max" 배율 — 확산은 width×height에서 돌고, 그 결과를 **서버가** 최대
    #: 크기까지 키운다 (payload의 `upscaled_enhance`). 크기는 보내지 않는다: 웹 UI 요청
    #: 캡처에도 이 불리언 하나뿐이고, 결과 크기는 서버가 정해 메타데이터에만 남긴다
    #: (`spec/v5/captures/v5_enhance_max_webui.sanitized.json`).
    #: i2i에서만 의미가 있다. 크기 규칙과 근거는 core/enhance.py 참고.
    upscaled_enhance: bool = False
    #: 인페인팅에서 마스크 **밖**을 원본 픽셀로 덮어쓸지 (NAI 웹UI의 "Overlay Original Image").
    #: 기본 False — 캡처 2건 모두 웹UI가 false로 보냈다 (`spec/v5/captures/`). true로 켜면
    #: 마스크 밖이 완전히 보존되는 대신, 생성된 안쪽과 만나는 경계가 이음선으로 드러난다.
    add_original_image: bool = False

    characters: tuple[CharacterCaption, ...] = ()
    vibes: tuple[VibeTransfer, ...] = ()
    character_refs: tuple[CharacterReference, ...] = ()

    extra_params: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAP)

    def with_seed(self, seed: int) -> GenerationRequest:
        return replace(self, seed=seed)


@dataclass(frozen=True)
class GenerationResult:
    """생성 결과. raw_bytes는 API가 준 PNG 원본 그대로 —
    저장 시 반드시 raw_bytes를 그대로 써서 NAI 메타데이터(tEXt 청크)를 보존할 것."""

    raw_bytes: bytes

    def open_image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.raw_bytes))
