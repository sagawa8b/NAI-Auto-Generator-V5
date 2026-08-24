"""V4 / V4.5 payload 빌더 — 구 naia.py의 검증된 로직을 순수 함수로 이식.

출력은 구 구현과 dict 동등(골든 테스트로 고정)해야 한다.
모델별 차이(api_name, var_plus 시그마 값, 기능 지원 여부)는 전부
ModelSpec에서 오며, 이 모듈에는 모델명 문자열 스니핑이 없다.
"""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from PIL import Image

from .models import CharacterReference, GenerationRequest, VibeTransfer

if TYPE_CHECKING:
    from .model_specs import ModelSpec

_CANVASES = [(2 / 3, 1024, 1536), (3 / 2, 1536, 1024), (1 / 1, 1472, 1472)]


def build_payload_v4(req: GenerationRequest, spec: ModelSpec) -> dict:
    """POST /ai/generate-image 요청 본문 전체를 만든다."""
    params = _build_base_parameters(req, spec)
    model_name = spec.api_name

    if req.action == "img2img":
        if req.image is None:
            raise ValueError("img2img requires 'image'")
        params["image"] = base64.b64encode(req.image).decode()
        params["strength"] = req.strength
        params["noise"] = req.noise
    elif req.action == "infill":
        if req.image is None or req.mask is None:
            raise ValueError("infill requires 'image' and 'mask'")
        params.update(
            {
                "image": base64.b64encode(req.image).decode(),
                "mask": encode_mask(req.mask),
                "add_original_image": req.add_original_image,
                "inpaintImg2ImgStrength": req.strength,
                "noise": 0,
                "deliberate_euler_ancestral_bug": False,
                "controlnet_strength": 1,
                "request_type": "NativeInfillingRequest",
            }
        )
        model_name = spec.api_name + "-inpainting"

    params.update(req.extra_params)

    return {
        "input": req.prompt,
        "model": model_name,
        "action": req.action,
        "parameters": params,
    }


def _build_base_parameters(req: GenerationRequest, spec: ModelSpec) -> dict:
    params = {
        "width": req.width,
        "height": req.height,
        "n_samples": 1,
        "seed": req.seed,
        "extra_noise_seed": req.seed,
        "sampler": req.sampler,
        "steps": req.steps,
        "scale": req.cfg_scale,
        "negative_prompt": req.negative_prompt,
        "cfg_rescale": req.cfg_rescale,
        "noise_schedule": req.scheduler,
        "params_version": spec.params_version,
        "legacy": False,
        "legacy_v3_extend": False,
    }

    if req.var_plus and spec.var_plus_sigma is not None:
        params["skip_cfg_above_sigma"] = spec.var_plus_sigma
    else:
        params["skip_cfg_above_sigma"] = None

    if "v4_prompt" in spec.supports:
        params.update(_build_v4_prompt(req))
    if req.vibes:
        _apply_vibe_transfer(params, list(req.vibes))
    if req.character_refs:
        if "char_ref" not in spec.supports:
            raise ValueError(f"Character Reference is not supported on model '{req.model}'")
        _apply_character_reference(params, list(req.character_refs))

    return params


def _build_v4_prompt(req: GenerationRequest) -> dict:
    char_captions, neg_char_captions = [], []
    for c in req.characters:
        center = {"x": c.center_x, "y": c.center_y}
        char_captions.append({"char_caption": c.prompt, "centers": [center]})
        neg_char_captions.append({"char_caption": c.uc, "centers": [center]})

    return {
        "autoSmea": True,
        "prefer_brownian": True,
        "ucPreset": 0,
        "use_coords": False,
        "legacy_uc": False,
        "add_original_image": True,
        "v4_prompt": {
            "caption": {"base_caption": req.prompt, "char_captions": char_captions},
            "use_coords": False,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {"base_caption": req.negative_prompt, "char_captions": neg_char_captions},
            "legacy_uc": False,
        },
    }


def _apply_vibe_transfer(params: dict, vibes: list[VibeTransfer]) -> None:
    params["reference_image_multiple"] = [v.encoded for v in vibes]
    params["reference_strength_multiple"] = [v.strength for v in vibes]
    params["reference_information_extracted_multiple"] = [v.information_extracted for v in vibes]
    params["normalize_reference_strength_multiple"] = True


def _apply_character_reference(params: dict, refs: list[CharacterReference]) -> None:
    params["director_reference_images"] = [base64.b64encode(letterbox(r.image)).decode() for r in refs]
    params["director_reference_strength_values"] = [r.strength for r in refs]
    params["director_reference_secondary_strength_values"] = [1.0 - r.fidelity for r in refs]
    params["director_reference_descriptions"] = [
        {"caption": {"base_caption": r.type, "char_captions": []}, "legacy_uc": False} for r in refs
    ]
    params["director_reference_information_extracted"] = [1.0] * len(refs)
    params["controlnet_strength"] = 1.0
    params["inpaintImg2ImgStrength"] = 1.0
    params["normalize_reference_strength_multiple"] = True
    params.pop("skip_cfg_above_sigma", None)


def letterbox(image_bytes: bytes) -> bytes:
    """이미지를 NAI 캔버스 크기로 레터박싱 (Character Reference용)."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (0, 0, 0))
        bg.paste(img, mask=img)
        img = bg
    else:
        img = img.convert("RGB")

    w, h = img.size
    ratio = w / h
    _, cw, ch = min(_CANVASES, key=lambda c: abs(ratio - c[0]))

    if w / cw > h / ch:
        nw, nh = cw, int(h * (cw / w))
    else:
        nh, nw = ch, int(w * (ch / h))

    resized = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (cw, ch), (0, 0, 0))
    canvas.paste(resized, ((cw - nw) // 2, (ch - nh) // 2))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def encode_mask(mask_bytes: bytes, scale: int = 8) -> str:
    img = Image.open(io.BytesIO(mask_bytes)).convert("L")
    img = img.point(lambda x: 255 if x > 128 else 0, "1")
    w, h = img.size
    img = img.resize((w * scale, h * scale), Image.NEAREST).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()
