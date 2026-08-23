"""Options_Dialog의 카테고리 페이지 — 공통 계약과 레지스트리.

셸(`ui/options_dialog.py`)은 페이지 내부를 모른다. 페이지는 `OptionsPage`를 상속하고
`@register_page`로 자신을 등록하며, 셸은 `page_class(key)`로 KEY만 보고 클래스를 찾는다.

등록은 이 파일 끝의 **평범한 import 문**으로 일어난다. 예전에는 `pkgutil.iter_modules`로
서브모듈을 훑었는데, 그러면 어느 코드도 페이지 모듈을 직접 import하지 않게 되어 PyInstaller의
정적 분석이 이들을 보지 못하고 번들에서 통째로 빠졌다. 프로즌 빌드에서 레지스트리가 비고
옵션 창이 빈 채로 떴다 (v0.2.0). 발견을 영리하게 만들 이유가 없으므로 import로 되돌렸다.

드래프트 의미론: `load`는 드래프트 → 위젯, `commit`은 위젯 → 드래프트다. 페이지는 라이브
`AppSettings`를 절대 만지지 않는다 (취소가 진짜 no-op이어야 한다 — Req 1.4, 1.6).
"""

from __future__ import annotations

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


def registered_pages() -> Mapping[str, type[OptionsPage]]:
    """등록된 페이지 전부. 이 모듈을 import한 시점에 이미 다 채워져 있다."""
    return dict(_REGISTRY)


def discover_pages() -> Mapping[str, type[OptionsPage]]:
    """`registered_pages()`와 같다 — 발견 단계가 따로 없다."""
    return registered_pages()


def page_class(key: str) -> type[OptionsPage]:
    """KEY로 페이지 클래스를 찾는다. 없으면 `KeyError`."""
    pages = registered_pages()
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


# 페이지 등록 — `register_page` 정의 뒤여야 하고, 정적 import여야 한다 (모듈 docstring 참고).
from . import (  # noqa: E402
    batch_page,  # noqa: F401
    filename_page,  # noqa: F401
    folders_page,  # noqa: F401
    interface_page,  # noqa: F401
    logging_page,  # noqa: F401
    resolution_page,  # noqa: F401
    tags_page,  # noqa: F401
)

__all__ = [
    "OptionsPage",
    "TranslateFn",
    "discover_pages",
    "open_in_file_manager",
    "page_class",
    "register_page",
    "registered_pages",
]
