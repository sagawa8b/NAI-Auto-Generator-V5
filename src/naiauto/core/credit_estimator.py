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
    percent: int
    timestamp: float  # monotonic


@dataclass(frozen=True)
class PerImageCost:
    """Measured cost for a specific resolution + step count."""

    resolution: tuple[int, int]  # (width, height)
    steps: int
    cost_per_image: float  # percent consumed per image


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

        Returns None if:
        - image_count <= 0
        - fewer than 2 observations
        - Total percent drop < 1
        - Recharge detected (percent increased between consecutive obs)
        """
        if image_count <= 0:
            return None
        if len(observations) < 2:
            return None

        # Sort by index to ensure proper ordering
        sorted_obs = sorted(observations, key=lambda o: o.index)

        # Check for recharge: percent must never increase between consecutive obs
        for i in range(len(sorted_obs) - 1):
            if sorted_obs[i + 1].percent > sorted_obs[i].percent:
                return None

        first_percent = sorted_obs[0].percent
        last_percent = sorted_obs[-1].percent
        total_drop = first_percent - last_percent

        if total_drop < 1:
            return None

        cost_per_image = total_drop / image_count
        return PerImageCost(resolution=resolution, steps=steps, cost_per_image=cost_per_image)

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
        """Retrieve stored cost. Returns None if missing or invalid (<=0)."""
        for entry in self._entries:
            if entry.resolution == resolution and entry.steps == steps:
                if entry.cost_per_image <= 0:
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
                self._entries.append(
                    PerImageCost(
                        resolution=(width, height),
                        steps=steps,
                        cost_per_image=cost_per_image,
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
            }
            for e in self._entries
        ]

        data = {"version": 1, "entries": entries_data}
        filepath.write_text(json.dumps(data, indent=2), encoding="utf-8")
