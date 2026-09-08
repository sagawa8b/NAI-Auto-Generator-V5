"""그림체 아레나 화면 — 조합을 만들고, 겨루게 하고, 교배시킨다.

    dialog.py      탭 컨테이너 (`ArenaDialog`)
    base.py        탭 공통 계약 (`ArenaTab`)
    roster_tab.py  작가 명단
    build_tab.py   조합 생성 + 이미지 프리페치
    arena_tab.py   이상형 월드컵
    evolve_tab.py  교배와 정리
    stats_tab.py   티어 시트 · 작가 순위 · 세대 분포
    combo_card.py  조합 카드 (월드컵·진화 공용)

로직은 `core/arena/`에, 생성 큐는 `services/arena_service.py`에 있다.
"""

from __future__ import annotations

from .base import ArenaTab
from .dialog import ArenaDialog

__all__ = ["ArenaDialog", "ArenaTab"]
