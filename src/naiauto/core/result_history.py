"""결과 패널 히스토리 — 이번 실행에서 만든 최근 이미지 목록과 "지금 보고 있는 장" (Qt-free).

결과 패널은 새 이미지가 나올 때마다 화면을 갈아 끼운다. 연속 생성 중에 두 장 전 그림이
더 마음에 들었다면 갤러리를 열어 찾아야 했다. 여기서는 최근 N장을 기억해 두고, 썸네일
줄(`ui/widgets/history_strip.py`)이 그 사이를 오가게 한다.

규칙 하나만 눈여겨보면 된다 — **따라가기**:

- 맨 끝(가장 최근) 장을 보고 있으면 새 이미지가 나올 때 그 장으로 넘어간다.
- 예전 장을 보고 있으면 넘어가지 않는다. 연속 생성이 몇 초마다 화면을 채 가면
  "이 장을 좀 보자"가 불가능해지기 때문이다.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_LIMIT = 30


@dataclass(frozen=True)
class HistoryEntry:
    """결과 한 장. `request`는 그 장에 실제로 보낸 요청(오버레이용) — 없을 수 있다."""

    path: str
    request: object | None = None


class ResultHistory:
    """최근 결과 목록과 커서."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = max(1, limit)
        self._entries: list[HistoryEntry] = []
        self._cursor = -1

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._entries)

    @property
    def cursor(self) -> int:
        """지금 보고 있는 자리 (비어 있으면 -1)."""
        return self._cursor

    @property
    def current(self) -> HistoryEntry | None:
        if 0 <= self._cursor < len(self._entries):
            return self._entries[self._cursor]
        return None

    @property
    def at_latest(self) -> bool:
        """맨 끝 장을 보고 있는가 (비어 있어도 True — 첫 장은 늘 따라간다)."""
        return self._cursor == len(self._entries) - 1

    def add(self, entry: HistoryEntry) -> bool:
        """새 결과를 끝에 붙인다. 커서가 새 장으로 넘어갔으면 True.

        한도를 넘으면 가장 오래된 것부터 버린다. 보고 있던 장이 버려지면 커서는
        남아 있는 가장 오래된 장에 머문다.
        """
        follow = self.at_latest
        self._entries.append(entry)
        overflow = len(self._entries) - self._limit
        if overflow > 0:
            del self._entries[:overflow]
            self._cursor = max(0, self._cursor - overflow)
        if follow:
            self._cursor = len(self._entries) - 1
        return follow

    def select(self, index: int) -> HistoryEntry | None:
        """`index`번째 장으로 옮긴다. 범위를 벗어나면 그대로 두고 None."""
        if not 0 <= index < len(self._entries):
            return None
        self._cursor = index
        return self._entries[index]

    def step(self, delta: int) -> HistoryEntry | None:
        """앞(-1)·뒤(+1)로 옮긴다. 끝에서 더 가려 하면 None (돌아가지 않는다)."""
        if not self._entries:
            return None
        target = self._cursor + delta
        if not 0 <= target < len(self._entries):
            return None
        return self.select(target)

    def remove(self, path: str) -> bool:
        """지워진 파일을 목록에서 뺀다 (갤러리에서 삭제한 경우). 뺐으면 True."""
        for index, entry in enumerate(self._entries):
            if entry.path == path:
                del self._entries[index]
                if index < self._cursor or self._cursor >= len(self._entries):
                    self._cursor -= 1
                if self._entries and self._cursor < 0:
                    self._cursor = 0
                return True
        return False


__all__ = ["DEFAULT_LIMIT", "HistoryEntry", "ResultHistory"]
