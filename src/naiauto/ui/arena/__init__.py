"""그림체 아레나 화면 — 조합을 만들고, 겨루게 하고, 교배시킨다.

    dialog.py      탭 컨테이너 (`ArenaDialog`)
    header.py      창 바닥 상태줄 — 상태 숫자 · 다음 할 일 · 진행 막대
    style.py       버튼 등급 (주 동작 · 위험 동작)
    base.py        탭 공통 계약 (`ArenaTab`)
    roster_tab.py  작가 명단
    build_tab.py   조합 생성 + 이미지 프리페치
    arena_tab.py   이상형 월드컵
    judge_tab.py   LLM 추천 (로컬 VLM 판독)
    result_tab.py  순위표 · 고른 조합 · 교배 · 결산 · 정리 · 기록
    combo_card.py  조합 카드 (월드컵용)
    segment_bar.py 분포 막대 (티어 · 세대)

로직은 `core/arena/`에, 생성 큐는 `services/arena_service.py`에 있다.
"""

from __future__ import annotations

from .base import ArenaTab
from .dialog import ArenaDialog

__all__ = ["ArenaDialog", "ArenaTab"]
