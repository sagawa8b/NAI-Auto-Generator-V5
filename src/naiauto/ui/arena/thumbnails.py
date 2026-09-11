"""조합 그림 로딩 — `ArenaStore` 경로 → `QPixmap`/`QIcon` (mtime 캐시 포함).

네 탭(월드컵·판독·조합 생성·결산)이 저마다 `store.image_path(combo)` →
`QPixmap(str(path))` → `isNull()` → `QIcon(...)` 패턴을 복제하고 있었다. 캐시가
있던 것은 조합 생성 탭 하나뿐이라, 나머지 탭은 새로 그릴 때마다 디스크에서 그림을
다시 디코딩했다 (조합이 수백 개인 순위표에서 눈에 띄게 느렸다).

여기 한 곳으로 모아, 손상 파일 처리·mtime 캐시·HiDPI 대응을 한 번만 고치면 네 탭이
함께 따라오게 한다.

캐시 키에 **수정 시각**을 넣는다 — 같은 이름으로 다시 뽑은 그림이 옛 썸네일로 남지
않게 한다. 캐시는 프로세스 전역이라, 여러 탭이 같은 조합을 보여 줄 때 디코딩을
공유한다. 상한을 넘으면 통째로 비운다 (조합을 수백 개 만들어 두고 오래 띄워 놓는
창이라, 지운 조합의 그림까지 끝없이 붙들고 있지 않게 한다).
"""

from __future__ import annotations

from PySide6.QtGui import QIcon, QPixmap

from ...core.arena.models import Combo
from ...core.arena.store import ArenaStore

#: 캐시가 이보다 커지면 통째로 비운다.
_CACHE_MAX = 400

#: (파일 경로, 수정 시각 ns) → 픽스맵. 수정 시각을 키에 넣어 재생성한 그림을 잡는다.
_pixmap_cache: dict[tuple[str, int], QPixmap] = {}


def combo_pixmap(store: ArenaStore, combo: Combo) -> QPixmap | None:
    """조합의 그림. 그림이 없거나·사라졌거나·못 읽으면 None."""
    path = store.image_path(combo)
    if path is None:
        return None
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:  # 방금 지워졌을 수 있다 — 다음 갱신에 맞춰진다
        return None
    pixmap = _pixmap_cache.get(key)
    if pixmap is None:
        loaded = QPixmap(str(path))
        if loaded.isNull():
            return None
        if len(_pixmap_cache) >= _CACHE_MAX:
            _pixmap_cache.clear()
        _pixmap_cache[key] = loaded
        pixmap = loaded
    return pixmap


def combo_icon(store: ArenaStore, combo: Combo) -> QIcon | None:
    """조합의 그림을 아이콘으로. 목록·표 셀이 쓴다. 그림이 없으면 None."""
    pixmap = combo_pixmap(store, combo)
    return QIcon(pixmap) if pixmap is not None else None


def clear_cache() -> None:
    """썸네일 캐시를 비운다 (테스트·아레나 폴더 전환용)."""
    _pixmap_cache.clear()


__all__ = ["clear_cache", "combo_icon", "combo_pixmap"]
