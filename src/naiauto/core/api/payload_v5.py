"""V5 payload 빌더 — 2026-08-21 웹 UI 네트워크 캡처 기반 구현.

근거 캡처 (spec/v5/captures/):
  - v5_t2i_single_character.sanitized.json — t2i
  - v5_infill.sanitized.json               — 인페인팅(action=infill)

V4 대비 확인된 변화:
  - 전송 형식: application/json → **multipart/form-data** ("request"라는 이름의
    JSON 파트 하나, filename="blob") — ModelSpec.request_format="multipart"
  - params_version: 3 → 4
  - 신규 키: ucPresetId/qualityPresetId(프리셋 식별자), characterPrompts
    (enabled 포함, API payload에 직접 등장), straight_alpha, tag_hint_qt,
    tag_hint_uc_preset, image_format
  - autoSmea가 false로 (V4는 true), use_coords가 true로
  - t2i에서는 extra_noise_seed가 빠졌다 (i2i/infill에는 있음)
  - v4_prompt / v4_negative_prompt 구조는 이름 그대로 유지
  - 인페인팅 모델명: "nai-diffusion-5-full-inpainting" (V4와 같은 접미사 규칙)
  - 웹 UI wrapper에는 use_new_shared_trial / recaptcha_token이 있으나
    (무료 티어 웹 세션용) 여기서는 보내지 않는다 — 2026-08-21 실토큰
    스모크로 pst- 경로에서는 불필요함이 확인되었다.

이미지 전송 방식 (2026-08-21 실서버 확인)
  V4는 parameters.image/mask에 base64를 인라인으로 넣었지만, V5는 다르다.
  base64를 넣어보면 서버가 이렇게 답한다:

      400 {"message": "image field references unknown form part \"iVBORw0...\""}

  즉 **parameters.image/mask 값은 multipart 파트의 "이름"**이고, 실제
  바이너리는 같은 multipart 본문의 별도 파트로 보낸다:

      --boundary
      Content-Disposition: form-data; name="request"; filename="blob"
      → {"parameters": {"image": "image", "mask": "mask", ...}}
      --boundary
      Content-Disposition: form-data; name="image"
      → 원본 PNG 바이트
      --boundary
      Content-Disposition: form-data; name="mask"
      → 마스크 PNG 바이트

  파트 이름은 build_binary_parts_v5()가 만들고 transport가 붙인다.
  (웹 UI가 쓰는 image_cache_secret_key 경로는 이미 업로드해 둔 이미지를
   키로 재참조하는 별도 최적화이며, 여기서는 필요하지 않다.)

NovelAI가 아직 V5로 출시하지 않은 기능 (출시 공지 "Still In Progress"):
  Precise Reference, Vibe Transfer → 캡처를 구할 수 없으므로 명시적 거부.
  ※ Curated Inpainting(=V5 Curated 모델의 인페인팅)은 미출시지만,
    Full 모델의 인페인팅은 위 캡처로 확인되어 지원한다.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from PIL import Image

from .errors import ModelSpecIncompleteError
from .models import GenerationRequest

if TYPE_CHECKING:
    from .model_specs import ModelSpec

# ucPresetId → tag_hint_uc_preset. 캡처로 확인된 값은 none=0, heavy=2뿐이며
# light/human_focus는 프리셋 강도 순서를 따른다고 가정한 추정치다.
_UC_PRESET_TAG_HINT = {"none": 0, "light": 1, "heavy": 2, "human_focus": 3}

# parameters.image/mask에 넣는 multipart 파트 이름
IMAGE_PART = "image"
MASK_PART = "mask"


def build_binary_parts_v5(req: GenerationRequest, spec: ModelSpec) -> dict[str, bytes]:
    """payload가 이름으로 참조하는 바이너리 파트들 (transport가 multipart에 붙인다)."""
    parts: dict[str, bytes] = {}
    if req.action in ("img2img", "infill") and req.image is not None:
        parts[IMAGE_PART] = req.image
    if req.action == "infill" and req.mask is not None:
        parts[MASK_PART] = _normalize_mask(req.mask, (req.width, req.height))
    return parts


def _normalize_mask(mask_bytes: bytes, size: tuple[int, int]) -> bytes:
    """마스크를 원본 이미지 해상도의 흑백 PNG로 맞춘다.

    V4는 1/8 크기 마스크를 받아 8배 확대해 보냈지만, V5의 파트 방식에서는
    사용자가 어떤 크기를 주든 대상 해상도에 맞추는 편이 예측 가능하다.
    """
    with Image.open(io.BytesIO(mask_bytes)) as img:
        mask = img.convert("L")
        if mask.size != size:
            mask = mask.resize(size, Image.NEAREST)
        mask = mask.point(lambda v: 255 if v > 128 else 0, "L").convert("RGB")
        buf = io.BytesIO()
        mask.save(buf, format="PNG")
        return buf.getvalue()


def build_payload_v5(req: GenerationRequest, spec: ModelSpec) -> dict:
    _reject_unreleased(req, spec)

    params = _build_base_parameters(req, spec)
    model_name = spec.api_name

    if req.action in ("img2img", "infill"):
        if req.image is None:
            raise ValueError(f"{req.action} requires 'image'")
        # base64가 아니라 multipart 파트 "이름" — 모듈 docstring 참조
        params["image"] = IMAGE_PART
        params["strength"] = req.strength
        params["extra_noise_seed"] = req.seed

    if req.action == "img2img":
        params["noise"] = req.noise
    elif req.action == "infill":
        if req.mask is None:
            raise ValueError("infill requires 'mask'")
        params["mask"] = MASK_PART
        params["noise"] = 0
        # 캡처는 false였지만(웹UI에서 "Overlay Original Image"를 끈 상태), 켜면 마스크 밖이
        # 원본 그대로 남는다 — 사용자가 고를 수 있어야 하므로 요청에서 받는다.
        params["add_original_image"] = req.add_original_image
        model_name = spec.api_name + "-inpainting"

    params.update(req.extra_params)

    return {
        "input": req.prompt,
        "model": model_name,
        "action": req.action,
        "parameters": params,
    }


def _reject_unreleased(req: GenerationRequest, spec: ModelSpec) -> None:
    """V5로 출시되지 않은 기능은 조용히 오동작시키지 않고 이유와 함께 거부한다."""
    if req.vibes:
        raise ModelSpecIncompleteError(
            'Vibe Transfer has not been released for V5 yet (NovelAI launch notes, "Still In Progress").'
        )
    if req.character_refs:
        raise ModelSpecIncompleteError(
            'Precise Reference has not been released for V5 yet (NovelAI launch notes, "Still In Progress").'
        )
    if req.action == "infill" and "inpaint" not in spec.supports:
        raise ModelSpecIncompleteError(
            f"Inpainting has not been released for model '{spec.key}' yet "
            '(NovelAI launch notes, "Still In Progress" — Curated Inpainting).'
        )
    if req.action not in ("generate", "img2img", "infill"):
        raise ModelSpecIncompleteError(f"V5 action '{req.action}' is not supported.")


def _build_base_parameters(req: GenerationRequest, spec: ModelSpec) -> dict:
    char_prompts = [
        {
            "prompt": c.prompt,
            "uc": c.uc,
            "center": {"x": c.center_x, "y": c.center_y},
            "enabled": True,
        }
        for c in req.characters
    ]
    char_captions = [
        {"char_caption": c.prompt, "centers": [{"x": c.center_x, "y": c.center_y}]} for c in req.characters
    ]
    neg_char_captions = [
        {"char_caption": c.uc, "centers": [{"x": c.center_x, "y": c.center_y}]} for c in req.characters
    ]

    params = {
        "params_version": spec.params_version,
        "width": req.width,
        "height": req.height,
        "scale": req.cfg_scale,
        "sampler": req.sampler,
        "steps": req.steps,
        "n_samples": 1,
        "ucPresetId": req.uc_preset_id,
        "qualityPresetId": req.quality_preset_id,
        "autoSmea": False,
        "dynamic_thresholding": False,
        "controlnet_strength": 1,
        "legacy": False,
        "add_original_image": True,
        "cfg_rescale": req.cfg_rescale,
        "legacy_v3_extend": False,
        "use_coords": req.use_coords,
        "legacy_uc": False,
        "normalize_reference_strength_multiple": True,
        "inpaintImg2ImgStrength": 1,
        "seed": req.seed,
        "characterPrompts": char_prompts,
        "straight_alpha": True,
        "tag_hint_qt": 1,
        "tag_hint_uc_preset": _UC_PRESET_TAG_HINT.get(req.uc_preset_id, 2),
        "v4_prompt": {
            "caption": {"base_caption": req.prompt, "char_captions": char_captions},
            "use_coords": req.use_coords,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {"base_caption": req.negative_prompt, "char_captions": neg_char_captions},
            "legacy_uc": False,
        },
        "negative_prompt": req.negative_prompt,
        "deliberate_euler_ancestral_bug": False,
        "prefer_brownian": True,
        "noise_schedule": req.scheduler,
        "image_format": "png",
    }

    if req.var_plus and spec.var_plus_sigma is not None:
        # V5의 Variety+ 시그마 값은 미확인 — spec에 값이 채워지기 전에는 전송되지 않는다
        params["skip_cfg_above_sigma"] = spec.var_plus_sigma

    return params
