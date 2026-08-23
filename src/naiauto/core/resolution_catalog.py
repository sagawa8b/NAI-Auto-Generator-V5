"""해상도 등급/종횡 분류와 값 정규화 (Qt-free core).

해상도 목록 자체는 Model_Spec_Registry가 소유한다. 이 모듈은 등급 경계 집합과
분류·스냅 규칙만 정의하고, 실제 해상도는 인자로 받는다 (PLANNING.md: 모델 지식은
Model_Spec_Registry에만).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 타입 힌트 전용 (import 순환 방지)
    from collections.abc import Iterable, Sequence

    from naiauto.core.api.model_specs import ModelSpec
    from naiauto.core.settings.schema import AppSettings

__all__ = [
    "Aspect",
    "DIMENSION_STEP",
    "FREE_PIXEL_LIMIT",
    "GROUP_ORDER",
    "LARGE_SIZES",
    "MAX_DIMENSION",
    "MIN_DIMENSION",
    "Resolution",
    "ResolutionCatalog",
    "ResolutionGroup",
    "WALLPAPER_SIZES",
    "classify_aspect",
    "classify_group",
    "exceeds_free_pixels",
    "is_valid_dimension",
    "snap_dimension",
    "snap_size",
]


class Aspect(str, Enum):
    """종횡 분류 (Req 5.2). 값은 UI 버튼 라벨로 그대로 쓰이며 i18n을 타지 않는다."""

    WIDE = "Wide"  # width > height
    SQUARE = "Square"  # width == height
    PORTRAIT = "Portrait"  # width < height


class ResolutionGroup(str, Enum):
    """해상도 등급 (Req 5.1)."""

    NORMAL = "Normal"
    LARGE = "Large"
    WALLPAPER = "Wallpaper"
    CUSTOM = "Custom"


GROUP_ORDER: tuple[ResolutionGroup, ...] = (
    ResolutionGroup.NORMAL,
    ResolutionGroup.LARGE,
    ResolutionGroup.WALLPAPER,
    ResolutionGroup.CUSTOM,
)

WALLPAPER_SIZES: frozenset[tuple[int, int]] = frozenset({(1088, 1920), (1920, 1088)})
LARGE_SIZES: frozenset[tuple[int, int]] = frozenset({(1024, 1536), (1536, 1024), (1472, 1472)})

MIN_DIMENSION = 64
MAX_DIMENSION = 2048
DIMENSION_STEP = 64
FREE_PIXEL_LIMIT = 1_048_576  # 1024×1024 — 초과 시 크레딧 경고 (Req 10.11)


@dataclass(frozen=True)
class Resolution:
    """선택 가능 해상도 1개. 등급은 카탈로그가 부여한다 (커스텀은 CUSTOM)."""

    width: int
    height: int
    group: ResolutionGroup

    @property
    def aspect(self) -> Aspect:
        return classify_aspect(self.width, self.height)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def label(self) -> str:
        """콤보/버튼에 쓰는 표시 문자열 ("832 × 1216")."""
        return f"{self.width} × {self.height}"


# ── 순수 함수 ──────────────────────────────────────────────────────────────


def classify_group(width: int, height: int) -> ResolutionGroup:
    """Wallpaper / Large 집합에 속하면 그 등급, 아니면 Normal (Req 5.1)."""
    size = (width, height)
    if size in WALLPAPER_SIZES:
        return ResolutionGroup.WALLPAPER
    if size in LARGE_SIZES:
        return ResolutionGroup.LARGE
    return ResolutionGroup.NORMAL


def classify_aspect(width: int, height: int) -> Aspect:
    """Req 5.2 / 10.9 — 삼분법."""
    if width > height:
        return Aspect.WIDE
    if width < height:
        return Aspect.PORTRAIT
    return Aspect.SQUARE


def snap_dimension(value: int) -> int:
    """64의 배수로 반올림한 뒤 [64, 2048]로 클램프 (Req 10.12).

    정확히 중간(예: 96)이면 위로 올린다. 결과는 항상 64의 배수이고 멱등이다.
    """
    snapped = ((int(value) + DIMENSION_STEP // 2) // DIMENSION_STEP) * DIMENSION_STEP
    if snapped < MIN_DIMENSION:
        return MIN_DIMENSION
    if snapped > MAX_DIMENSION:
        return MAX_DIMENSION
    return snapped


def snap_size(width: int, height: int) -> tuple[int, int]:
    """너비·높이를 각각 `snap_dimension`으로 정규화한다."""
    return (snap_dimension(width), snap_dimension(height))


def is_valid_dimension(value: int) -> bool:
    """64 ≤ value ≤ 2048 이고 64의 배수 (Req 5.8 검증용)."""
    return MIN_DIMENSION <= value <= MAX_DIMENSION and value % DIMENSION_STEP == 0


def exceeds_free_pixels(width: int, height: int) -> bool:
    """width * height > FREE_PIXEL_LIMIT (Req 10.11)."""
    return width * height > FREE_PIXEL_LIMIT


# ── 카탈로그 ───────────────────────────────────────────────────────────────


class ResolutionCatalog:
    """모델 해상도 + 옵션 → 등급별 선택 가능 해상도.

    구성 규칙 (design.md "구성 규칙"):
    1. `spec_resolutions`의 각 항목을 `classify_group`으로 분류한다.
    2. 비활성 등급(Large / Wallpaper)은 버린다 — `groups()`에도 나오지 않는다 (Req 5.4, 5.5).
    3. `custom_resolutions`는 `snap_size`로 정규화해 `Custom` 등급에 넣는다 (Req 5.7).
    4. 등급 내 중복은 첫 등장만 남기고, 등급 간 중복은 허용한다.
    5. 항목이 없는 등급은 `groups()`에서 빠진다.
    """

    def __init__(
        self,
        spec_resolutions: Sequence[tuple[int, int]],
        *,
        enable_large: bool = False,
        enable_wallpaper: bool = False,
        custom_resolutions: Sequence[tuple[int, int]] = (),
    ) -> None:
        allow_large = bool(enable_large)
        allow_wallpaper = bool(enable_wallpaper)
        buckets: dict[ResolutionGroup, list[Resolution]] = {group: [] for group in GROUP_ORDER}
        seen: dict[ResolutionGroup, set[tuple[int, int]]] = {group: set() for group in GROUP_ORDER}

        def add(width: int, height: int, group: ResolutionGroup) -> None:
            size = (width, height)
            if size in seen[group]:  # 규칙 4: 등급 내 중복은 첫 등장만
                return
            seen[group].add(size)
            buckets[group].append(Resolution(width=width, height=height, group=group))

        for width, height in spec_resolutions:
            group = classify_group(width, height)  # 규칙 1
            if group is ResolutionGroup.LARGE and not allow_large:
                continue  # 규칙 2
            if group is ResolutionGroup.WALLPAPER and not allow_wallpaper:
                continue  # 규칙 2
            add(width, height, group)

        for width, height in custom_resolutions:
            snapped_w, snapped_h = snap_size(width, height)  # 규칙 3
            add(snapped_w, snapped_h, ResolutionGroup.CUSTOM)

        # 규칙 5: 빈 등급은 아예 담지 않는다.
        self._buckets: dict[ResolutionGroup, tuple[Resolution, ...]] = {
            group: tuple(items) for group, items in buckets.items() if items
        }
        self._all: tuple[Resolution, ...] = tuple(
            item for group in GROUP_ORDER for item in self._buckets.get(group, ())
        )

    @classmethod
    def from_settings(cls, spec: ModelSpec, settings: AppSettings) -> ResolutionCatalog:
        """활성화된 커스텀 행만 골라 생성 (ui가 반복 조립하지 않도록)."""
        options = settings.resolution
        customs: Iterable[tuple[int, int]] = (
            (row.width, row.height) for row in options.customs if row.enabled
        )
        return cls(
            tuple(spec.resolutions),
            enable_large=options.enable_large,
            enable_wallpaper=options.enable_wallpaper,
            custom_resolutions=tuple(customs),
        )

    # ── 조회 ────────────────────────────────────────────────────────────

    def groups(self) -> tuple[ResolutionGroup, ...]:
        """비어 있지 않은 등급만, GROUP_ORDER 순서로 (Req 5.4, 5.5)."""
        return tuple(group for group in GROUP_ORDER if group in self._buckets)

    def resolutions(self, group: ResolutionGroup | None = None) -> tuple[Resolution, ...]:
        """group=None이면 모든 등급을 GROUP_ORDER 순으로 이어 붙인다.

        등급 내 순서는 입력 순서(커스텀은 행 순서)를 보존한다.
        """
        if group is None:
            return self._all
        return self._buckets.get(group, ())

    def aspects(self, group: ResolutionGroup) -> frozenset[Aspect]:
        """그 등급에 존재하는 Aspect 집합 (Req 10.6의 버튼 활성화 판단)."""
        return frozenset(item.aspect for item in self.resolutions(group))

    def first_of_aspect(self, group: ResolutionGroup, aspect: Aspect) -> Resolution | None:
        """그 등급에서 해당 Aspect를 가진 첫 해상도 (Req 10.5, 10.7)."""
        for item in self.resolutions(group):
            if item.aspect is aspect:
                return item
        return None

    def first_of_group(self, group: ResolutionGroup) -> Resolution | None:
        """Req 10.8 폴백."""
        items = self.resolutions(group)
        return items[0] if items else None

    def contains(self, width: int, height: int) -> bool:
        """선택 가능 목록에 있는지 (Req 10.10의 '직접 입력' 판단)."""
        return self.group_of(width, height) is not None

    def group_of(self, width: int, height: int) -> ResolutionGroup | None:
        """그 크기를 담고 있는 첫 등급 (GROUP_ORDER 우선). 없으면 None."""
        size = (width, height)
        for group in GROUP_ORDER:
            for item in self._buckets.get(group, ()):
                if item.size == size:
                    return group
        return None

    def default(self) -> Resolution:
        """첫 번째 Normal 해상도. Normal이 비면 전체 첫 항목 (Req 5.10)."""
        normal = self.resolutions(ResolutionGroup.NORMAL)
        if normal:
            return normal[0]
        if self._all:
            return self._all[0]
        raise ValueError("resolution catalog is empty")
