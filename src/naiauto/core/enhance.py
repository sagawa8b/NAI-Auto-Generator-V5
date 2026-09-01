"""Enhance(강화 업스케일) 계획 — 원본 크기와 배율에서 요청을 만든다 (Qt-free).

V4.5의 Enhance와 **동작이 다르다.** V5 웹 UI는 배율을 1x / 1.5x / Max 세 가지로
주고, 1.5x와 Max는 서로 다른 방식으로 큰 그림을 만든다. 아래는 같은 그림을
1.5x와 Max로 각각 강화한 결과 PNG의 메타데이터(2026-08-31 확보)에서 읽어낸 것이다.

원본 t2i (832×1216, PromptGenerateRequest) 대비:

| 키 | 1.5x | Max |
|---|---|---|
| `request_type` | Img2ImgRequest | Img2ImgRequest |
| `width` × `height` | **1280 × 1856** (확대됨) | **832 × 1216** (원본 그대로) |
| `upscaled_enhance` | null | **true** |
| `upscaled_width` / `_height` (결과 메타데이터에만) | 없음 | **1467 / 2144** |
| 프롬프트 | 끝에 `, -2::upscaled, blurry::,` | 원본 그대로 |
| `strength` / `noise` | 0.5 / 0 | 0.5 / 0 |
| `legacy` / `color_correct` | false / false | false / false |

두 방식의 의미:

- **1.5x** — 클라이언트가 원본을 강화 해상도로 **먼저 키워서** 보내고, 확산이 그
  큰 해상도에서 돈다. 입력이 흐릿한 확대본이므로 프롬프트 끝에
  `-2::upscaled, blurry::,`를 붙여 "확대 티"를 눌러 준다.
- **Max** — 확산은 **원본 해상도 그대로** 돌고(디테일 보정), 서버가 그 결과를
  픽셀 상한까지 확대한다. 입력이 흐릿하지 않으니 프롬프트도 손대지 않는다.
  확대 크기가 8의 배수가 아니라는 점(1467 = 8×183.375)이 "확산 해상도가 아니라
  최종 업스케일 크기"의 결정적 근거다.

**요청에는 `upscaled_enhance: true` 불리언 하나만 들어간다.** 결과 크기는 서버가
정해 메타데이터에만 남긴다 — 웹 UI 요청 캡처
(`spec/v5/captures/v5_enhance_max_webui.sanitized.json`)에 upscaled_width/height가
없다. 아래 계산은 **UI에 결과 크기를 미리 보여주기 위한 예측**이다.

픽셀 상한 = **3,145,728 (= 1536×2048)**. Max 관찰 3건이 전부 이 값에 맞는다
(가로세로비 유지, 각 변 내림):

| 원본 | 배율 sqrt(상한/픽셀) | 계산 | 관찰 |
|---|---|---|---|
| 832×1216 | 1.763324 | 1467.09 / 2144.20 | **1467×2144** |
| 1024×1024 | 1.732051 | 1773.62 / 1773.62 | **1773×1773** |
| 1536×1024 | 1.414214 | 2172.23 / 1448.15 | **2172×1448** |

1024×1024가 반올림(1774)이 아니라 **내림**(1773)임을 확정해 준다.

이 상한은 1.5x에도 걸린다. Large(1536×1024)에서 웹 UI의 Upscale Amount에
1.5x가 없고 `1x / Max`만 나오는데, 그 1.5x 목표(2304×1536 = 3,538,944)가 상한을
넘는다. 즉 **1.5x는 목표 크기가 상한에 들어갈 때만 제공된다** (`available_amounts`).

1.5x 강화 해상도는 V4.5가 쓰던 표와 같다 (832×1216 → 1280×1856 일치).
표에 없는 크기는 1.5배 후 64의 배수로 맞춘다 — 확산 해상도는 잠재 격자에
걸려야 하고, NAI의 해상도 프리셋도 전부 64의 배수다.

웹 UI의 Magnitude 슬라이더는 Strength/Noise의 프리셋일 뿐이다 (V4와 같다).
`Show Advanced`를 누르면 Strength 0.5 / Noise 0을 그대로 노출한다 — 우리도
Magnitude를 흉내 내지 않고 두 값을 직접 노출한다.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from PIL import Image

from .api.models import GenerationRequest
from .metadata.reuse import ReusableSettings, apply_to_request

logger = logging.getLogger(__name__)

#: 웹 UI의 "Upscale Amount" 세 가지.
UpscaleAmount = Literal["1x", "1.5x", "max"]
UPSCALE_AMOUNTS: tuple[UpscaleAmount, ...] = ("1x", "1.5x", "max")

#: 확산 해상도를 맞출 격자. NAI 해상도 프리셋이 전부 64의 배수다.
RESOLUTION_STEP = 64

#: 강화 결과의 픽셀 상한 (= 1536×2048 = 3,145,728). Max 관찰 3건이 전부 이 값에
#: 맞고, 1.5x가 제공되는지도 이 값으로 갈린다 (모듈 docstring의 표 참고).
ENHANCE_MAX_PIXELS = 1536 * 2048

#: 1.5x에서 프롬프트 끝에 붙는 확대 아티팩트 억제 태그 (캡처 그대로, 앞뒤 공백 없음).
UPSCALE_ARTIFACT_TAG = ", -2::upscaled, blurry::,"

#: 강화 기본값 — 캡처의 "Magnitude 2"가 strength 0.5 / noise 0 이었다.
DEFAULT_STRENGTH = 0.5
DEFAULT_NOISE = 0.0

#: NAI 해상도 프리셋 → 1.5x 강화 해상도. V4.5의 표와 같고, 832×1216 → 1280×1856은
#: 이번 캡처로 V5에서도 같음이 확인됐다. 단순한 ×1.5가 아니라는 점에 주의
#: (832×1.5 = 1248이지만 표는 1280이다).
ENHANCED_RESOLUTIONS: dict[tuple[int, int], tuple[int, int]] = {
    # Normal
    (1024, 1024): (1536, 1536),
    (832, 1216): (1280, 1856),
    (1216, 832): (1856, 1280),
    # Large
    (1472, 1472): (2208, 2208),
    (1024, 1536): (1536, 2304),
    (1536, 1024): (2304, 1536),
    # Wallpaper
    (1088, 1920): (1632, 2880),
    (1920, 1088): (2880, 1632),
    # Small
    (640, 640): (960, 960),
    (512, 768): (768, 1152),
    (768, 512): (1152, 768),
}


@dataclass(frozen=True)
class EnhancePlan:
    """한 장을 강화할 때의 크기 계획. `plan_enhance()`가 만든다."""

    amount: UpscaleAmount
    source_size: tuple[int, int]
    #: 확산이 도는 해상도. 1.5x면 확대된 크기, 1x/Max면 원본(64 격자 보정) 크기.
    diffusion_size: tuple[int, int]
    #: Max — 요청에 `upscaled_enhance: true`를 넣어 서버가 결과를 키우게 한다.
    server_upscale: bool = False
    #: 서버가 키울 크기의 **예측값** (UI 표시용). 요청에는 들어가지 않는다.
    predicted_upscaled_size: tuple[int, int] | None = None
    #: 1.5x에서 프롬프트 끝에 붙는 태그 (다른 배율은 빈 문자열).
    prompt_suffix: str = ""

    @property
    def output_size(self) -> tuple[int, int]:
        """최종 이미지 크기 — 서버 업스케일이 있으면 그 예측 크기다."""
        return self.predicted_upscaled_size or self.diffusion_size

    @property
    def scale(self) -> float:
        """원본 대비 최종 배율 (긴 변 기준)."""
        return max(self.output_size) / max(self.source_size)

    @property
    def is_upscaling(self) -> bool:
        return self.output_size != self.source_size


def _snap(value: int) -> int:
    """확산 해상도를 64 격자에 올린다 (반올림, 최소 한 칸)."""
    return max(RESOLUTION_STEP, int(math.floor(value / RESOLUTION_STEP + 0.5)) * RESOLUTION_STEP)


def _fit_to_pixel_budget(size: tuple[int, int], budget: int) -> tuple[int, int]:
    """가로세로비를 지킨 채 픽셀 수가 budget을 넘지 않는 최대 크기 (내림)."""
    width, height = size
    scale = math.sqrt(budget / (width * height))
    return max(1, int(width * scale)), max(1, int(height * scale))


def enhanced_resolution(size: tuple[int, int]) -> tuple[int, int] | None:
    """1.5x 강화 해상도. **표에 없는 크기는 None** — 짐작하지 않는다.

    표는 NAI가 정해 둔 값이고 단순한 ×1.5가 아니다 (832×1.5 = 1248이지만 표는 1280).
    사용자 정의 해상도에서 NAI가 어떤 크기를 쓰는지는 캡처가 없어 알 수 없으므로,
    ×1.5를 격자에 맞춰 짐작하는 대신 1.5x를 제공하지 않는다
    (`unavailable_reason` → "custom_size"). Max는 순수한 픽셀 상한 계산이라
    어떤 크기에서도 쓸 수 있다.

    픽셀 상한을 넘는지는 보지 않는다 — 그 판단은 `unavailable_reason()`이 한다.
    """
    return ENHANCED_RESOLUTIONS.get(size)


#: 1.5x를 쓸 수 있는 원본 크기 (표에서 상한 안에 드는 것만). UI가 안내 문구에 쓴다.
STANDARD_15X_SIZES: tuple[tuple[int, int], ...] = tuple(
    source for source, target in ENHANCED_RESOLUTIONS.items() if target[0] * target[1] <= ENHANCE_MAX_PIXELS
)

#: 배율을 못 쓰는 이유. UI가 이 값으로 문구를 고른다 (core는 번역을 갖지 않는다).
#:   "custom_size" — 표에 없는 크기라 1.5x 목표를 알 수 없다
#:   "over_cap"    — 1.5x 목표가 픽셀 상한을 넘는다 (Large/Wallpaper)
#:   "already_max" — 원본이 이미 상한이라 더 키울 수 없다
UnavailableReason = Literal["custom_size", "over_cap", "already_max"]


class EnhanceUnavailableError(ValueError):
    """이 원본 크기에서는 그 배율을 쓸 수 없다.

    조용히 다른 크기로 바꾸지 않고 거부한다 — 짐작한 해상도로 Anlas를 쓰는 것보다
    낫다. UI는 `reason`으로 안내 문구를 고른다.
    """

    def __init__(self, amount: UpscaleAmount, size: tuple[int, int], reason: UnavailableReason) -> None:
        self.amount = amount
        self.size = size
        self.reason = reason
        super().__init__(f"{amount} enhance is not available for {size[0]}x{size[1]} ({reason})")


def unavailable_reason(size: tuple[int, int], amount: UpscaleAmount) -> UnavailableReason | None:
    """그 배율을 못 쓰는 이유. None이면 쓸 수 있다."""
    if amount == "1x":
        return None
    native = (_snap(size[0]), _snap(size[1]))
    if amount == "1.5x":
        target = enhanced_resolution(size)
        if target is None:
            return "custom_size"
        if target[0] * target[1] > ENHANCE_MAX_PIXELS:
            return "over_cap"
        if target[0] * target[1] <= native[0] * native[1]:
            return "already_max"
        return None
    return None if max_upscaled_size(native) is not None else "already_max"


def max_upscaled_size(size: tuple[int, int]) -> tuple[int, int] | None:
    """Max로 서버가 만들 결과 크기의 예측값. 더 키울 수 없으면 None.

    요청에 담기는 값이 아니라 UI에 미리 보여 주기 위한 계산이다 (모듈 docstring 참고).
    """
    upscaled = _fit_to_pixel_budget(size, ENHANCE_MAX_PIXELS)
    if upscaled[0] <= size[0] or upscaled[1] <= size[1]:
        return None
    return upscaled


def available_amounts(size: tuple[int, int]) -> tuple[UpscaleAmount, ...]:
    """이 크기에서 고를 수 있는 배율. 1x는 언제나 가능하다.

    웹 UI도 이렇게 감춘다 — Large(1536×1024)에서는 `1x / Max`만 나오는데, 그 1.5x
    목표(2304×1536)가 픽셀 상한을 넘기 때문이다.
    """
    return tuple(a for a in UPSCALE_AMOUNTS if unavailable_reason(size, a) is None)


def plan_enhance(size: tuple[int, int], amount: UpscaleAmount) -> EnhancePlan:
    """원본 크기와 배율에서 확산 크기·서버 업스케일 크기·프롬프트 꼬리를 정한다.

    어떤 배율이든 확산 해상도는 64 격자에 올린다 — 임의 크기 PNG(스캔본, 잘라낸
    이미지)를 그대로 보내면 잠재 격자에 걸리지 않는다.
    """
    if amount not in UPSCALE_AMOUNTS:
        raise ValueError(f"unknown upscale amount: {amount!r} (expected one of {UPSCALE_AMOUNTS})")

    source = (int(size[0]), int(size[1]))
    if source[0] <= 0 or source[1] <= 0:
        raise ValueError(f"invalid source size: {size!r}")

    # NAI 해상도 프리셋은 전부 64의 배수라 이 보정으로 값이 바뀌지 않는다.
    native = (_snap(source[0]), _snap(source[1]))

    reason = unavailable_reason(source, amount)
    if reason == "already_max":
        # 더 키울 수 없다는 것은 **확정된 사실**이라 짐작이 끼어들 여지가 없다.
        # 크기 그대로 디테일만 다시 그린다 (1x와 같은 처리).
        logger.info("%s enhance cannot grow %s — refining at the same size", amount, source)
        return EnhancePlan(amount="1x", source_size=source, diffusion_size=native)
    if reason is not None:
        # 반대로 "얼마로 키워야 하는지 모른다"면 짐작하지 않고 거부한다.
        raise EnhanceUnavailableError(amount, source, reason)

    if amount == "1x":
        return EnhancePlan(amount=amount, source_size=source, diffusion_size=native)

    if amount == "1.5x":
        target = enhanced_resolution(source)
        assert target is not None  # unavailable_reason이 이미 걸렀다
        return EnhancePlan(
            amount=amount,
            source_size=source,
            diffusion_size=target,
            prompt_suffix=UPSCALE_ARTIFACT_TAG,
        )

    # max — 확산은 원본 해상도에서 돌고, 서버가 결과를 픽셀 상한까지 키운다.
    return EnhancePlan(
        amount=amount,
        source_size=source,
        diffusion_size=native,
        server_upscale=True,
        predicted_upscaled_size=max_upscaled_size(native),
    )


def prepare_source_image(image_bytes: bytes, plan: EnhancePlan) -> bytes:
    """확산 해상도에 맞춘 PNG 바이트. 이미 그 크기의 PNG면 원본 바이트 그대로 돌려준다.

    표의 강화 해상도는 원본과 가로세로비가 1% 안쪽으로만 다르므로(832/1216 = 0.6842,
    1280/1856 = 0.6897) 레터박스 없이 그대로 늘린다 — V4.5는 허용 해상도 목록에서
    비율이 크게 어긋나는 값을 고를 수 있어 검은 띠를 넣고 뒤에서 잘라냈지만,
    여기서는 그런 경우가 나오지 않는다.

    크기가 맞아도 PNG가 아니면 변환한다 — API는 PNG를 받는다 (폴더 강화는 JPEG·WebP도
    대기열에 넣는다).
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.size == plan.diffusion_size and img.format == "PNG":
            return image_bytes
        image = img.convert("RGB")
        if image.size != plan.diffusion_size:
            image = image.resize(plan.diffusion_size, Image.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()


def apply_enhance(
    request: GenerationRequest,
    *,
    image: bytes,
    plan: EnhancePlan,
    strength: float = DEFAULT_STRENGTH,
    noise: float = DEFAULT_NOISE,
) -> GenerationRequest:
    """t2i 요청을 강화(i2i) 요청으로 바꾼다. 이미지는 이미 확산 크기로 맞춰져 있어야 한다.

    프롬프트 꼬리는 `prompt`에만 붙인다 — 캡처에서도 `v4_prompt`의 base_caption만
    같이 바뀌었고(빌더가 같은 문자열을 쓴다) 캐릭터 캡션은 원본 그대로였다.
    마스크는 지운다: 강화는 그림 전체를 다시 그리는 i2i다.
    """
    return dataclasses.replace(
        request,
        action="img2img",
        prompt=request.prompt + plan.prompt_suffix,
        width=plan.diffusion_size[0],
        height=plan.diffusion_size[1],
        image=prepare_source_image(image, plan),
        mask=None,
        strength=strength,
        noise=noise,
        upscaled_enhance=plan.server_upscale,
    )


def unusable_sources(sources: Sequence[EnhanceSource], amount: UpscaleAmount) -> tuple[EnhanceSource, ...]:
    """대기열에서 그 배율로 강화할 수 없는 항목 (시작 전 확인용).

    "더 키울 수 없음"(already_max)은 여기 넣지 않는다 — 그건 거부가 아니라 같은 크기로
    디테일만 다시 그리는 정상 처리다 (`plan_enhance` 참고).
    """
    return tuple(s for s in sources if unavailable_reason(s.size, amount) not in (None, "already_max"))


@dataclass(frozen=True)
class EnhanceSource:
    """폴더 강화 대기열의 한 항목 — 파일 경로와 그 PNG에서 읽은 생성 설정."""

    path: str
    size: tuple[int, int]
    settings: ReusableSettings | None = None


def build_enhance_provider(
    base: GenerationRequest,
    sources: Sequence[EnhanceSource],
    *,
    amount: UpscaleAmount,
    strength: float = DEFAULT_STRENGTH,
    noise: float = DEFAULT_NOISE,
    use_metadata: bool = True,
) -> Callable[[int], GenerationRequest]:
    """폴더 강화용 요청 공급자 — index(1부터)를 받아 그 장의 요청을 만든다.

    워커 스레드에서 불리므로 위젯을 만지지 않는다. 이미지 바이트는 **그때 읽는다** —
    폴더에 수백 장이 있어도 한 번에 메모리에 올리지 않기 위해서다 (크기와 메타데이터만
    미리 훑어 둔다).

    그 배율을 쓸 수 없는 크기가 섞여 있으면 `plan_enhance`가 `EnhanceUnavailableError`를
    던져 잡이 중단된다. 배치를 시작하기 전에 `unusable_sources()`로 걸러 두는 것이
    호출자(메인 윈도우)의 몫이다 — Anlas를 쓰다 중간에 멈추지 않도록.
    """
    items = tuple(sources)
    if not items:
        raise ValueError("no enhance sources")

    def provide(index: int) -> GenerationRequest:
        source = items[(index - 1) % len(items)]
        request = base
        if use_metadata and source.settings is not None:
            # 시드는 물려받지 않는다 — 웹 UI도 강화할 때마다 새 시드를 쓴다
            # (같은 그림의 원본/1.5x/Max 세 캡처의 seed가 전부 다르다).
            request = apply_to_request(request, dataclasses.replace(source.settings, seed=None))
        with open(source.path, "rb") as f:
            image_bytes = f.read()
        plan = plan_enhance(source.size, amount)
        return apply_enhance(request, image=image_bytes, plan=plan, strength=strength, noise=noise)

    return provide
