"""Options_Dialog의 카테고리 페이지 — 공통 계약과 레지스트리.

셸(`ui/options_dialog.py`)은 페이지 내부를 모른다. 페이지는 `OptionsPage`를 상속하고
`@register_page`로 자신을 등록하며, 셸은 `discover_pages()` / `page_class(key)`로 KEY만 보고
클래스를 찾는다. 셸이 7개 페이지 모듈을 하나씩 import하지 않아도 되도록, 발견은 이 패키지의
서브모듈을 훑는 방식(`pkgutil.iter_modules`)으로 지연 수행한다 — 아직 없는 모듈은 그냥 없는 것이다.

드래프트 의미론: `load`는 드래프트 → 위젯, `commit`은 위젯 → 드래프트다. 페이지는 라이브
`AppSettings`를 절대 만지지 않는다 (취소가 진짜 no-op이어야 한다 — Req 1.4, 1.6).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from ...core.settings.schema import AppSettings

#: `I18nManager.get_text` 같은 번역 함수 (키 + 포맷 인자 → 문자열).
TranslateFn = Callable[..., str]


class OptionsPage(QWidget):
    """Options_Dialog의 카테고리 하나.

    하위 클래스는 `KEY`를 `NAV_ORDER`의 값 중 하나로 지정하고 `load` / `commit` /
    `retranslate`를 구현한다. `notices()`는 필요할 때만 재정의한다.
    """

    #: `options_dialog.NAV_ORDER`의 키. 하위 클래스가 반드시 덮어써야 한다.
    KEY: str = ""

    def load(self, draft: AppSettings) -> None:
        """드래프트 값 → 위젯. 다이얼로그가 열릴 때 1회 호출된다."""
        raise NotImplementedError

    def commit(self, draft: AppSettings) -> None:
        """위젯 → 드래프트. 저장 버튼을 눌렀을 때 검증보다 먼저 호출된다.

        빈 경로를 기본값으로 되돌리는 것 같은 **정규화**는 여기서 한다.
        범위 검증은 `core.settings.validation.validate_options`의 몫이다.
        """
        raise NotImplementedError

    def retranslate(self) -> None:
        """모든 표시 문구를 현재 언어로 다시 채운다 (Req 1.9, 1.10)."""
        raise NotImplementedError

    def notices(self) -> tuple[str, ...]:
        """저장 후 사용자에게 보여줄 안내 문구 i18n 키 (기본: 없음)."""
        return ()


# ── 페이지 레지스트리 ─────────────────────────────────────

_REGISTRY: dict[str, type[OptionsPage]] = {}
_discovered = False


def register_page(cls: type[OptionsPage]) -> type[OptionsPage]:
    """`OptionsPage` 하위 클래스를 KEY로 등록하는 데코레이터."""
    key = cls.KEY
    if not key:
        raise ValueError(f"{cls.__name__} must define a non-empty KEY")
    existing = _REGISTRY.get(key)
    if existing is not None and existing is not cls:
        raise ValueError(f"duplicate options page key {key!r}: {existing.__name__} vs {cls.__name__}")
    _REGISTRY[key] = cls
    return cls


def discover_pages() -> Mapping[str, type[OptionsPage]]:
    """이 패키지의 서브모듈을 한 번씩 import해 레지스트리를 채우고 돌려준다."""
    global _discovered
    if not _discovered:
        for info in pkgutil.iter_modules(__path__):
            importlib.import_module(f"{__name__}.{info.name}")
        _discovered = True
    return dict(_REGISTRY)


def registered_pages() -> Mapping[str, type[OptionsPage]]:
    """이미 등록된 페이지만 (import를 유발하지 않는다)."""
    return dict(_REGISTRY)


def page_class(key: str) -> type[OptionsPage]:
    """KEY로 페이지 클래스를 찾는다. 없으면 `KeyError`."""
    pages = discover_pages()
    try:
        return pages[key]
    except KeyError:
        raise KeyError(f"no options page registered for key {key!r}") from None


# ── 공유 헬퍼 ─────────────────────────────────────────────


def open_in_file_manager(
    path: str | Path,
    tr: TranslateFn,
    *,
    parent: QWidget | None = None,
) -> bool:
    """폴더를 OS 파일 탐색기로 연다. 없으면 만들어서 연다 (Req 2.3, 8.6).

    `MainWindow._open_folder()`와 같은 절차를 공유한다. 실패하면 경고 상자를 띄우고
    `False`를 돌려준다.
    """
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        QMessageBox.warning(parent, tr("errors.title"), tr("folders.open_failed", e))
        return False
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
        QMessageBox.warning(parent, tr("errors.title"), tr("folders.open_failed", str(target)))
        return False
    return True


__all__ = [
    "OptionsPage",
    "TranslateFn",
    "discover_pages",
    "open_in_file_manager",
    "page_class",
    "register_page",
    "registered_pages",
]
