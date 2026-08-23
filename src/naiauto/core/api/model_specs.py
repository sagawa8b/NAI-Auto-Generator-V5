"""모델별 capability 테이블 — 모델 차이는 전부 여기서만 정의한다.

V4 앱에서 params_version=3, skip_cfg_above_sigma=19, "4-5" in model 같은
모델별 가정이 3개 레이어에 흩어져 있던 문제의 해법. 새 모델(V5)이 나오면
이 레지스트리에 항목 하나를 채우는 것으로 대응한다.

V5 항목은 스텁 상태다: 공식 스펙 미공개(2026-08-20 출시 직후)이므로,
docs/SPEC_CAPTURE.md 절차로 확보한 네트워크 캡처를 근거로
payload_v5.build_payload_v5와 아래 V5 필드들을 채운다.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import ResponseDecodeError
from .models import GenerationRequest, GenerationResult
from .payload_v4 import build_payload_v4
from .payload_v5 import build_binary_parts_v5, build_payload_v5

BASE_URL = "https://image.novelai.net"
GENERATE_ENDPOINT = f"{BASE_URL}/ai/generate-image"

PayloadBuilder = Callable[[GenerationRequest, "ModelSpec"], dict]
ResponseDecoder = Callable[[bytes, str], GenerationResult]
# multipart 전송 시 payload가 이름으로 참조하는 바이너리 파트 {이름: 바이트}
BinaryPartsBuilder = Callable[[GenerationRequest, "ModelSpec"], dict]


def decode_zip_response(body: bytes, content_type: str) -> GenerationResult:
    """V4 시대 표준 응답: zip 안의 첫 PNG. 원본 bytes를 그대로 보존한다."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            image_bytes = zf.read(zf.infolist()[0])
        return GenerationResult(raw_bytes=image_bytes)
    except (zipfile.BadZipFile, IndexError) as e:
        raise ResponseDecodeError(
            f"Unexpected response (content-type={content_type!r}, {len(body)} bytes): {e}"
        ) from e


@dataclass(frozen=True)
class ModelSpec:
    key: str  # 레지스트리 키, 예: "naid4.5f"
    api_name: str  # API model 필드 값
    endpoint: str = GENERATE_ENDPOINT
    params_version: int = 3
    # HTTP 본문 형식: "json" = raw JSON POST (V4 시대),
    # "multipart" = multipart/form-data의 "request" JSON 파트 (V5 웹 UI 방식)
    request_format: str = "json"
    build_payload: PayloadBuilder = build_payload_v4
    build_binary_parts: BinaryPartsBuilder | None = None
    decode_response: ResponseDecoder = decode_zip_response
    samplers: tuple[str, ...] = (
        "k_euler",
        "k_euler_ancestral",
        "k_dpmpp_2m",
        "k_dpmpp_2s_ancestral",
        "k_dpmpp_sde",
        "k_dpmpp_2m_sde",
        "ddim_v3",
    )
    schedulers: tuple[str, ...] = ("karras", "native", "exponential", "polyexponential")
    # (width, height) 프리셋. 첫 항목이 기본값. ~1M px 이하 = 무료 티어(V4 기준)
    resolutions: tuple[tuple[int, int], ...] = (
        (832, 1216),
        (1024, 1024),
        (1216, 832),
        (896, 1152),
        (1152, 896),
        (768, 1344),
        (1344, 768),
        (704, 1472),
        (1472, 704),
        (1024, 1536),
        (1536, 1024),
        (1472, 1472),
        (1088, 1920),
        (1920, 1088),
    )
    defaults: Mapping[str, Any] = field(
        default_factory=lambda: {
            "steps": 28,
            "cfg_scale": 5.0,
            "cfg_rescale": 0.4,
            "sampler": "k_euler_ancestral",
            "scheduler": "native",
        }
    )
    quality_tags: str = ""  # 프롬프트 끝에 추가하는 품질 태그
    uc_presets: Mapping[str, str] = field(default_factory=dict)
    supports: frozenset[str] = (
        frozenset()
    )  # {"v4_prompt","characters","vibe","char_ref","inpaint","var_plus"}
    var_plus_sigma: float | None = None
    incomplete: bool = False  # True면 스펙 캡처 대기 중 (UI에서 경고 표시)
    # False면 메인 윈도우 모델 콤보에 넣지 않는다. V4 계열은 payload 골든 테스트,
    # 옛 PNG 메타데이터 재사용, smoke CLI가 계속 쓰므로 레지스트리에는 남긴다.
    ui_visible: bool = True


_UC_45F = {
    "heavy": "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page",
    "light": "lowres, artistic error, scan artifacts, worst quality, bad quality, jpeg artifacts, multiple views, very displeasing, too many watermarks, negative space, blank page",
    "human_focus": "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page, @_@, mismatched pupils, glowing eyes, bad anatomy",
    "none": "",
}

_UC_45C = {
    "heavy": "blurry, lowres, upscaled, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, halftone, multiple views, logo, too many watermarks, negative space, blank page",
    "light": "blurry, lowres, upscaled, artistic error, scan artifacts, jpeg artifacts, logo, too many watermarks, negative space, blank page",
    "human_focus": "blurry, lowres, upscaled, artistic error, film grain, scan artifacts, bad anatomy, bad hands, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, halftone, multiple views, logo, too many watermarks, @_@, mismatched pupils, glowing eyes, negative space, blank page",
    "none": "",
}

_UC_4F = {
    "heavy": "blurry, lowres, error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, multiple views, logo, too many watermarks",
    "light": "blurry, lowres, error, worst quality, bad quality, jpeg artifacts, very displeasing",
    "none": "",
}

_UC_4C = {
    "heavy": "blurry, lowres, error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, logo, dated, signature, multiple views, gigantic breasts",
    "light": "blurry, lowres, error, worst quality, bad quality, jpeg artifacts, very displeasing, logo, dated, signature",
    "none": "",
}

# V5 UC 프리셋 — heavy는 캡처의 negative_prompt 앞부분에서 확인 (V4.5F heavy와 동일).
# light/human_focus는 미확인이라 V4.5F 값을 잠정 사용 (캡처 확보 시 교체).
_UC_5 = {
    "heavy": _UC_45F["heavy"],
    "light": _UC_45F["light"],
    "human_focus": _UC_45F["human_focus"],
    "none": "",
}

_V4_SUPPORTS = frozenset({"v4_prompt", "characters", "vibe", "inpaint", "var_plus"})
_V45_SUPPORTS = _V4_SUPPORTS | {"char_ref"}


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "naid4.5f": ModelSpec(
        key="naid4.5f",
        api_name="nai-diffusion-4-5-full",
        quality_tags=", location, very aesthetic, masterpiece, no text",
        uc_presets=_UC_45F,
        supports=_V45_SUPPORTS,
        var_plus_sigma=58,
        ui_visible=False,
    ),
    "naid4.5c": ModelSpec(
        key="naid4.5c",
        api_name="nai-diffusion-4-5-curated",
        quality_tags=", location, masterpiece, no text, -0.8::feet::, rating:general",
        uc_presets=_UC_45C,
        supports=_V45_SUPPORTS,
        var_plus_sigma=58,
        ui_visible=False,
    ),
    "naid4f": ModelSpec(
        key="naid4f",
        api_name="nai-diffusion-4-full",
        quality_tags=", no text, best quality, very aesthetic, absurdres",
        uc_presets=_UC_4F,
        supports=_V4_SUPPORTS,
        var_plus_sigma=19,
        ui_visible=False,
    ),
    "naid4c": ModelSpec(
        key="naid4c",
        api_name="nai-diffusion-4-curated-preview",
        quality_tags=", rating:general, amazing quality, very aesthetic, absurdres",
        uc_presets=_UC_4C,
        supports=_V4_SUPPORTS,
        var_plus_sigma=19,
        ui_visible=False,
    ),
    # ── V5 ────────────────────────────────────────────────────
    # 2026-08-21 웹 UI 캡처(spec/v5/captures/v5_t2i_single_character.sanitized.json)로
    # 확정: api_name, endpoint(V4와 동일), params_version=4, multipart 전송,
    # quality tags(", very aesthetic, masterpiece, no text"), UC heavy 프리셋
    # (V4.5F heavy와 동일 텍스트), noise_schedule=karras, autoSmea=false.
    # 미확정: i2i/inpaint/vibe/char_ref payload, Variety+ 시그마(var_plus_sigma=None
    # → 전송 안 함), 샘플러 전체 목록(캡처는 k_euler_ancestral만 — V4 목록 가정),
    # curated의 api_name(전례 기반 추정 → incomplete=True 유지).
    "naid5f": ModelSpec(
        key="naid5f",
        api_name="nai-diffusion-5-full",
        params_version=4,
        request_format="multipart",
        build_payload=build_payload_v5,
        build_binary_parts=build_binary_parts_v5,
        defaults={
            "steps": 28,
            "cfg_scale": 5.0,
            "cfg_rescale": 0.0,
            "sampler": "k_euler_ancestral",
            "scheduler": "karras",
        },
        quality_tags=", very aesthetic, masterpiece, no text",
        uc_presets=_UC_5,
        # inpaint는 v5_infill 캡처(nai-diffusion-5-full-inpainting)로 확인됨.
        # vibe / char_ref는 NovelAI 미출시라 넣지 않는다.
        supports=frozenset({"v4_prompt", "characters", "img2img", "inpaint"}),
        var_plus_sigma=None,
    ),
    "naid5c": ModelSpec(
        key="naid5c",
        api_name="nai-diffusion-5-curated",
        params_version=4,
        request_format="multipart",
        build_payload=build_payload_v5,
        build_binary_parts=build_binary_parts_v5,
        defaults={
            "steps": 28,
            "cfg_scale": 5.0,
            "cfg_rescale": 0.0,
            "sampler": "k_euler_ancestral",
            "scheduler": "karras",
        },
        quality_tags=", very aesthetic, masterpiece, no text",
        uc_presets=_UC_5,
        # Curated Inpainting은 출시 공지의 "Still In Progress" 항목이라 제외
        supports=frozenset({"v4_prompt", "characters", "img2img"}),
        var_plus_sigma=None,
        incomplete=True,
    ),
}


def get_spec(model_key: str) -> ModelSpec:
    """레지스트리 key 또는 API 모델명 어느 쪽으로도 조회 가능."""
    if model_key in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_key]
    for spec in MODEL_REGISTRY.values():
        if spec.api_name == model_key:
            return spec
    raise KeyError(f"Unknown model: {model_key!r} (known: {sorted(MODEL_REGISTRY)})")
