"""V5 크레딧 소모량 측정 및 남은 생성 가능 장수 환산.

배치 생성 시 관측된 v5-credit 로그 엔트리로부터 해상도+스텝별
이미지당 소모량을 계산하고 JSON 파일에 저장한다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CreditObservation:
    """Single v5-credit log observation."""

    index: int
    #: V5 크레딧 잔량(%). 응답에 `usage`가 없는 계정(크레딧 대신 Anlas로 내는 경우)은 None.
    percent: int | None
    timestamp: float  # monotonic
    #: 그때의 Anlas 잔액. 크레딧이 떨어진 뒤나 크레딧이 없는 계정은 이쪽이 줄어든다.
    anlas: int | None = None


@dataclass(frozen=True)
class PerImageCost:
    """Measured cost for a specific resolution + step count."""

    resolution: tuple[int, int]  # (width, height)
    steps: int
    cost_per_image: float  # percent consumed per image (0 = 크레딧으로는 측정되지 않음)
    anlas_per_image: float = 0.0  # Anlas consumed per image (0 = Anlas는 나가지 않았음)


def images_between(observations: list[CreditObservation]) -> int:
    """첫 관측과 마지막 관측 **사이에** 생성된 장수.

    관측은 각 장이 끝난 **뒤에** 찍힌다. 그래서 N장 배치의 관측 N개가 보여 주는 잔량 차이는
    2번째~N번째 장, 즉 N-1장이 쓴 양이다 (첫 장이 쓴 양은 첫 관측 이전이라 보이지 않는다).
    이 값을 `compute_batch_cost()`의 `image_count`로 넘겨야 장당 소모량이 맞는다 — 완료 매수
    N을 그대로 넘기면 (N-1)/N배로 낮게 나와 남은 장수가 부풀려진다.
    """
    if len(observations) < 2:
        return 0
    indices = [obs.index for obs in observations]
    return max(indices) - min(indices)


def _drop_per_image(values: list[int | None], image_count: int, *, minimum_drop: int) -> float | None:
    """관측값이 처음부터 끝까지 얼마나 줄었는지를 장수로 나눈다.

    값이 하나라도 비었거나, 중간에 **늘어난** 적이 있거나(충전·구매), 줄어든 양이
    `minimum_drop`에 못 미치면 None — 그 배치로는 이 단위의 소모량을 알 수 없다.
    """
    if any(value is None for value in values):
        return None
    numbers = [value for value in values if value is not None]
    if any(later > earlier for earlier, later in zip(numbers, numbers[1:], strict=False)):
        return None
    drop = numbers[0] - numbers[-1]
    if drop < minimum_drop:
        return None
    return drop / image_count


@dataclass(frozen=True)
class CostEstimate:
    """생성 전에 보여 줄 예상 소모량. `total_*`은 매수가 무한(0)이면 None."""

    percent_per_image: float
    anlas_per_image: float
    total_percent: float | None
    total_anlas: float | None


def _format_amount(percent: float, anlas: float) -> str:
    """`0.8%`, `20 Anlas`, 둘 다 있으면 `0.8% + 20 Anlas`."""
    parts = []
    if percent > 0:
        parts.append(f"{percent:.2f}".rstrip("0").rstrip(".") + "%")
    if anlas > 0:
        parts.append(f"{anlas:.1f}".rstrip("0").rstrip(".") + " Anlas")
    return " + ".join(parts)


def format_estimate(estimate: CostEstimate) -> tuple[str, str]:
    """(장당, 합계) 표시 문자열. 합계를 모르면(무한 매수) 두 번째는 ""."""
    per_image = _format_amount(estimate.percent_per_image, estimate.anlas_per_image)
    if estimate.total_percent is None or estimate.total_anlas is None:
        return per_image, ""
    return per_image, _format_amount(estimate.total_percent, estimate.total_anlas)


_MAX_ENTRIES = 50
_FILENAME = "credit_costs.json"


class CreditEstimator:
    """Computes and persists per-image credit cost from measurement batches."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._entries: list[PerImageCost] = []

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------

    def compute_batch_cost(
        self,
        observations: list[CreditObservation],
        image_count: int,
        resolution: tuple[int, int],
        steps: int,
    ) -> PerImageCost | None:
        """Compute per-image cost from a batch's observations.

        `image_count`는 첫 관측과 마지막 관측 **사이에** 생성된 장수다 (`images_between()`).
        배치의 완료 매수가 아니다 — 관측은 각 장이 끝난 뒤에 찍히기 때문이다.

        Returns None if:
        - image_count <= 0
        - fewer than 2 observations
        - Total percent drop < 1 **and** total Anlas drop < 1 (nothing measurable)
        - Recharge detected (percent increased between consecutive obs) and no Anlas drop

        크레딧(%)과 Anlas를 따로 본다. 크레딧이 남아 있으면 %만 줄고, 크레딧이 없으면
        Anlas만 준다 — 측정된 쪽만 값이 들어가고 나머지는 0이다.
        """
        if image_count <= 0:
            return None
        if len(observations) < 2:
            return None

        # Sort by index to ensure proper ordering
        sorted_obs = sorted(observations, key=lambda o: o.index)

        percent_cost = _drop_per_image([o.percent for o in sorted_obs], image_count, minimum_drop=1)
        anlas_cost = _drop_per_image([o.anlas for o in sorted_obs], image_count, minimum_drop=1)
        if percent_cost is None and anlas_cost is None:
            return None
        return PerImageCost(
            resolution=resolution,
            steps=steps,
            cost_per_image=percent_cost or 0.0,
            anlas_per_image=anlas_cost or 0.0,
        )

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def store_cost(self, cost: PerImageCost) -> None:
        """Persist cost entry. Replaces existing entry for same key. Caps at 50 entries."""
        key = (cost.resolution, cost.steps)

        # Remove existing entry with same key if present
        self._entries = [e for e in self._entries if (e.resolution, e.steps) != key]

        # Evict oldest (first in list) if at cap
        if len(self._entries) >= _MAX_ENTRIES:
            self._entries.pop(0)

        self._entries.append(cost)
        self.save()

    def get_cost(self, resolution: tuple[int, int], steps: int) -> PerImageCost | None:
        """Retrieve stored cost. Returns None if missing or invalid (no positive cost)."""
        for entry in self._entries:
            if entry.resolution == resolution and entry.steps == steps:
                if entry.cost_per_image <= 0 and entry.anlas_per_image <= 0:
                    return None
                return entry
        return None

    # ------------------------------------------------------------------
    # Estimation
    # ------------------------------------------------------------------

    def estimate_remaining(self, current_percent: int, resolution: tuple[int, int], steps: int) -> int | None:
        """Estimated remaining images as floor(current_percent / cost). None if no data."""
        cost = self.get_cost(resolution, steps)
        if cost is None:
            return None
        if cost.cost_per_image <= 0:
            return None
        return math.floor(current_percent / cost.cost_per_image)

    def estimate_cost(self, resolution: tuple[int, int], steps: int, count: int) -> CostEstimate | None:
        """이 해상도·스텝으로 `count`장을 뽑을 때의 예상 소모량. 측정값이 없으면 None.

        `count`가 0(무한)이면 합계는 None이다.
        """
        cost = self.get_cost(resolution, steps)
        if cost is None:
            return None
        percent = max(0.0, cost.cost_per_image)
        anlas = max(0.0, cost.anlas_per_image)
        finite = count > 0
        return CostEstimate(
            percent_per_image=percent,
            anlas_per_image=anlas,
            total_percent=percent * count if finite else None,
            total_anlas=anlas * count if finite else None,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load from JSON. Missing/malformed file → empty state."""
        filepath = self._data_dir / _FILENAME
        self._entries = []

        if not filepath.exists():
            return

        try:
            raw = filepath.read_text(encoding="utf-8")
            data: dict[str, Any] = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            return

        if not isinstance(data, dict):
            return

        entries_raw = data.get("entries")
        if not isinstance(entries_raw, list):
            return

        for item in entries_raw:
            if not isinstance(item, dict):
                continue
            try:
                width = int(item["width"])
                height = int(item["height"])
                steps = int(item["steps"])
                cost_per_image = float(item["cost_per_image"])
                anlas_per_image = float(item.get("anlas_per_image", 0.0))
                self._entries.append(
                    PerImageCost(
                        resolution=(width, height),
                        steps=steps,
                        cost_per_image=cost_per_image,
                        anlas_per_image=anlas_per_image,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

    def save(self) -> None:
        """Persist current entries to JSON."""
        filepath = self._data_dir / _FILENAME
        self._data_dir.mkdir(parents=True, exist_ok=True)

        entries_data = [
            {
                "width": e.resolution[0],
                "height": e.resolution[1],
                "steps": e.steps,
                "cost_per_image": e.cost_per_image,
                "anlas_per_image": e.anlas_per_image,
            }
            for e in self._entries
        ]

        data = {"version": 1, "entries": entries_data}
        filepath.write_text(json.dumps(data, indent=2), encoding="utf-8")
