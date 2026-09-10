"""그림체 아레나 — 작가 조합을 만들고, 겨루게 하고, 교배시키는 코어 (Qt-free).

    combos.py     조합 생성 + NovelAI 가중치 프롬프트 변환
    elo.py        점수·티어·대진
    evolution.py  교배·돌연변이·정리
    guidance.py   지금 어디까지 왔는지 + 다음에 할 일
    store.py      arena.json / images 입출력
    models.py     위 모듈들이 주고받는 자료형

UI(`ui/arena_dialog.py`)와 생성 큐(`services/arena_service.py`)가 이 위에 올라간다.
"""

from __future__ import annotations

from .models import (
    ARENA_SCHEMA_VERSION,
    ArenaState,
    ArtistEntry,
    Combo,
    ComboGenParams,
    ComboSlot,
    JudgeRun,
)
from .store import ArenaStore

__all__ = [
    "ARENA_SCHEMA_VERSION",
    "ArenaState",
    "ArenaStore",
    "ArtistEntry",
    "Combo",
    "ComboGenParams",
    "ComboSlot",
    "JudgeRun",
]
