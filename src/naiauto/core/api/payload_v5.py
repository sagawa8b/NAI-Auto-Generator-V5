"""V5 payload 빌더 — 2026-08-21 웹 UI 네트워크 캡처 기반 구현.

근거 캡처 (spec/v5/captures/):
  - v5_t2i_single_character.sanitized.json — t2i
  - v5_infill.sanitized.json               — 인페인팅(action=infill)
  - v5_enhance_max_webui.sanitized.json    — 강화 Max (action=img2img, 2026-08-31)

V4 대비 확인된 변화:
  - 전송 형식: application/json → **multipart/form-data** ("request"라는 이름의
    JSON 파트 하나, filename="blob") — ModelSpec.request_format="multipart"
  - params_version: 3 → 4
  - 신규 키: ucPresetId/qualityPresetId(프리셋 식별자), characterPrompts
    (enabled 포함, API payload에 직접 등장), straight_alpha, tag_hint_qt,
    tag_hint_uc_preset, image_format
  - autoSmea가 false로 (V4는 true), use_coords가 true로
  - t2i에서는 extra_noise_seed가 빠졌다. i2i/infill에는 있고 값은 **seed - 1**이다
    (캡처 3건 전부 일치)
  - v4_prompt / v4_negative_prompt 구조는 이름 그대로 유지
  - 인페인팅 모델명: "nai-diffusion-5-full-inpainting" (V4와 같은 접미사 규칙)
  - Enhance(강화)에 서버 업스케일이 생겼다: i2i에 `upscaled_enhance: true` (웹 UI의
    "Max" 배율). **크기는 보내지 않는다** — 서버가 정해 결과 메타데이터에만 남긴다.
    크기 규칙과 근거는 core/enhance.py 참고.
  - img2img에만 있는 키: color_correct(false), inpaintImg2ImgStrength(0.01 —
    인페인팅 캡처 2건은 1이었다).
  - qualityPresetId를 따라 tag_hint_qt가 1(standard) / 0(none)으로 움직인다
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

#: 잠재 공간 축소 배율. 마스크 경계를 이 격자에 맞춘다 (`_normalize_mask`).
LATENT_BLOCK = 8


def build_binary_parts_v5(req: GenerationRequest, spec: ModelSpec) -> dict[str, bytes]:
    """payload가 이름으로 참조하는 바이너리 파트들 (transport가 multipart에 붙인다)."""
    parts: dict[str, bytes] = {}
    if req.action in ("img2img", "infill") and req.image is not None:
        parts[IMAGE_PART] = req.image
    if req.action == "infill" and req.mask is not None:
        parts[MASK_PART] = _normalize_mask(req.mask, (req.width, req.height))
    return parts


def _normalize_mask(mask_bytes: bytes, size: tuple[int, int]) -> bytes:
    """마스크를 대상 해상도의 흑백 PNG로 맞추고, 잠재 블록 경계에 스냅시킨다.

    확산 모델은 1/8 해상도 잠재 공간에서 동작하므로 마스크 경계도 8픽셀 격자에
    맞아야 한다. 격자에 걸치면 절반만 덮인 잠재 셀이 생겨 경계에 아티팩트가 남는다.
    NAI 웹UI의 마스크가 8×8 계단형으로 보이는 것도 같은 이유이고, 우리 V4 경로
    (`payload_v4.encode_mask`)도 1/8 마스크를 ×8로 확대해 같은 결과를 낸다.

    축소는 `BOX`(면적 평균) 후 임계값이라, 블록에 조금이라도 칠해져 있으면 그 블록이
    살아남는다. 칠한 영역이 블록 단위로 조금 넓어지는 편이, 좁아져서 고치려던 부분이
    빠지는 것보다 낫다.
    """
    with Image.open(io.BytesIO(mask_bytes)) as img:
        mask = img.convert("L")
        if mask.size != size:
            mask = mask.resize(size, Image.NEAREST)

        blocks = (max(1, size[0] // LATENT_BLOCK), max(1, size[1] // LATENT_BLOCK))
        mask = mask.resize(blocks, Image.BOX)  # 블록 안 칠해진 비율
        mask = mask.point(lambda v: 255 if v > 0 else 0, "L")  # 조금이라도 칠해졌으면 산다
        mask = mask.resize(size, Image.NEAREST).convert("RGB")

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
        # 캡처 3건(인페인팅 2 + 강화 1)이 모두 seed - 1이다. 규칙이 확정되기 전에는
        # V4처럼 seed를 그대로 보냈지만, 이제 요청 캡처로 확인되어 웹 UI를 따른다.
        params["extra_noise_seed"] = req.seed - 1

    if req.action == "img2img":
        params["noise"] = req.noise
        params["color_correct"] = False
        # 웹 UI의 img2img 요청은 이 값을 0.01로 보낸다 (인페인팅 캡처 2건은 1이었다).
        # 이름대로라면 인페인팅용 값이라 img2img에서는 무시될 것 같지만, 캡처가 있는
        # 액션은 그 캡처를 따른다.
        params["inpaintImg2ImgStrength"] = 0.01
        if req.upscaled_enhance:
            # Enhance "Max" — 확산은 width×height에서 돌고, 서버가 결과를 최대 크기까지
            # 키운다. **크기는 보내지 않는다**: 요청 캡처에 이 불리언 하나뿐이고,
            # upscaled_width/height는 서버가 정해 결과 메타데이터에만 남긴다.
            params["upscaled_enhance"] = True
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
        "tag_hint_qt": 1 if req.quality_preset_id == "standard" else 0,
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
