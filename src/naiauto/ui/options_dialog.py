"""Options_Dialog 셸 — 좌측 카테고리 목록 + 우측 `QStackedWidget` + 저장/취소 (Req 1.2–1.4, 1.9, 1.10).

셸은 페이지 내부를 모른다. `ui/options_pages`의 레지스트리에서 `NAV_ORDER` 순서대로 클래스를 찾아
인스턴스를 만들고, 드래프트 사본만 오간다.

드래프트 의미론 (Req 1.4, 1.6):

```
_live   = settings                      # Main_Window가 들고 있는 객체 (동일 참조)
_draft  = settings.model_copy(deep=True)  # 페이지들이 편집하는 사본
_before = settings.model_copy(deep=True)  # 변경 감지·로깅 롤백용 스냅숏
```

취소 / 창 닫기는 드래프트를 그냥 버린다. `_live`는 저장 파이프라인 밖에서 한 번도 만지지 않으므로
메모리 값도 `settings.json`도 그대로다 — **취소는 진짜 no-op**이다.

저장 파이프라인 (`save()`, Req 1.5–1.8):

```
1. 각 페이지.commit(draft)          위젯 → 드래프트 (빈 경로 → 기본값 같은 정규화 포함)
2. validate_options(draft)          core 검증 (Qt 없음)
3. 위반이 있으면 issues[0].page로 전환 + "{항목}: {메시지}" 경고 → return False (열어 둔다)
4. apply_draft(draft, live)         OWNED_FIELDS만 복사, 바뀐 경로 목록을 돌려받는다
5. 바뀐 경로로 부수효과 판단: language → i18n.set_language, debug_logging/log_dir → reconfigure_logging
                              (실패 시 메시지 + log_dir 롤백)
6. save_settings(live, path)        OSError → 오류 메시지, 메모리 값은 유지, 그래도 닫는다
7. applied.emit(); accept()
```
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n.manager import I18nManager
from ..core.logging_setup import LoggingConfigError, reconfigure_logging
from ..core.settings.schema import AppSettings
from ..core.settings.store import save_settings
from ..core.settings.validation import (
    PAGE_FILENAME,
    PAGE_GENERATION,
    PAGE_RESOLUTION,
    OptionIssue,
    validate_options,
)
from .options_pages import OptionsPage, page_class

logger = logging.getLogger(__name__)

#: 좌측 카테고리 순서 (Req 1.2). 세 키는 core 검증 모듈의 상수를 그대로 재사용해
#: `validate_options`가 돌려주는 `OptionIssue.page`와 어긋날 수 없게 한다.
NAV_ORDER: tuple[str, ...] = (
    "folders",
    PAGE_FILENAME,
    PAGE_GENERATION,
    PAGE_RESOLUTION,
    "interface",
    "tags",
    "log",
)

#: 좌측 목록 고정 너비 (px).
NAV_WIDTH = 170

#: 옵션 다이얼로그가 **소유하는** 필드만 드래프트에서 라이브 객체로 되쓴다 (Req 1.5).
#: 화이트리스트를 두는 이유: 다이얼로그가 열려 있는 동안 메인 윈도우 위젯 상태(프롬프트·시드 등)와
#: `AppSettings`가 어긋나 있을 수 있어서, 드래프트를 통째로 덮으면 그 값을 되돌려 버린다.
OWNED_FIELDS: tuple[str, ...] = (
    "language",
    "save_dir",
    "wildcards_dir",
    "presets_dir",
    "artist_combos_dir",
    "gallery_dir",
    "wd14_dir",
    "wd14_model",
    "log_dir",
    "tag_database_path",
    "tag_autocomplete_enabled",
    "prompt_font",
    "filename_template",
    "image_format",
    "prompt_word_limit",
    "character_word_limit",
    "debug_headers",
    "debug_logging",
    "show_image_source",
    "show_enhance",
    "check_updates_on_start",
    "measure_credit",
    "batch.count",
    "batch.delay_seconds",
    "batch.quick_counts",
    "batch.random_settings_order",
    "resolution",
    "ui",
)

__all__ = ["NAV_ORDER", "NAV_WIDTH", "OWNED_FIELDS", "OptionsDialog", "apply_draft"]


def apply_draft(draft: AppSettings, live: AppSettings) -> tuple[str, ...]:
    """`OWNED_FIELDS`만 draft → live로 복사하고, 실제로 바뀐 경로 목록을 돌려준다 (Req 1.5, 1.6).

    돌려받은 목록은 부수효과 판단(언어 전환, 로깅 재구성, `applied` 후속 처리)에 그대로 쓴다.
    중첩 모델(`resolution`, `ui`, `prompt_font`)은 드래프트의 하위 객체를 그대로 넘긴다 —
    드래프트는 저장 직후 폐기되므로 소유권 이전이 안전하다.
    """
    changed: list[str] = []
    for path in OWNED_FIELDS:
        *parents, name = path.split(".")
        draft_holder: object = draft
        live_holder: object = live
        for part in parents:
            draft_holder = getattr(draft_holder, part)
            live_holder = getattr(live_holder, part)
        new = getattr(draft_holder, name)
        if new == getattr(live_holder, name):
            continue
        setattr(live_holder, name, new)
        changed.append(path)
    return tuple(changed)


class OptionsDialog(QDialog):
    """모든 설정을 카테고리별로 모아 편집하는 모달 다이얼로그."""

    #: 저장이 성공한 뒤 1회 — Main_Window가 후속 반영에 쓴다 (task 14.2에서 발신).
    applied = Signal()

    def __init__(
        self,
        i18n: I18nManager,
        settings: AppSettings,
        *,
        supports_i2i: bool = True,
        settings_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._supports_i2i = supports_i2i
        self._settings_path = Path(settings_path) if settings_path is not None else None

        self._live = settings
        self._draft = settings.model_copy(deep=True)
        self._before = settings.model_copy(deep=True)

        self.setModal(True)
        self.resize(820, 560)

        root = QVBoxLayout(self)

        body = QHBoxLayout()

        self._nav = QListWidget(self)
        self._nav.setFixedWidth(NAV_WIDTH)
        self._nav.setIconSize(QSize(0, 0))  # 아이콘 없음 (설계: 텍스트만)
        self._nav.currentRowChanged.connect(self._on_row_changed)
        body.addWidget(self._nav)

        right = QVBoxLayout()
        self._description = QLabel(self)
        self._description.setWordWrap(True)
        self._description.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._description.setStyleSheet("color: palette(mid);")
        right.addWidget(self._description)

        self._stack = QStackedWidget(self)
        right.addWidget(self._stack, 1)
        body.addLayout(right, 1)

        root.addLayout(body, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._save_button = self._buttons.button(QDialogButtonBox.StandardButton.Save)
        self._cancel_button = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self._save_button.clicked.connect(self._on_save_clicked)
        self._cancel_button.clicked.connect(self.reject)
        root.addWidget(self._buttons)

        self._pages: dict[str, OptionsPage] = {}
        self._page_keys: tuple[str, ...] = ()
        self._build_pages()

        # 다이얼로그가 열릴 때 1회: 드래프트 값 → 위젯 (Req 1.4).
        for page in self._pages.values():
            page.load(self._draft)

        self._subscribed = False
        i18n.subscribe(self._on_language_changed)
        self._subscribed = True

        self.retranslate()
        if self._page_keys:
            self._nav.setCurrentRow(0)

    # ── 페이지 구성 ────────────────────────────────────────────────────

    def _build_pages(self) -> None:
        """`NAV_ORDER` 순서대로 등록된 페이지를 만들어 목록과 스택에 넣는다."""
        keys: list[str] = []
        for key in NAV_ORDER:
            try:
                cls = page_class(key)
            except KeyError:
                # 아직 없는 페이지는 그냥 없는 것으로 둔다 (부분 구현 상태에서도 셸이 뜬다).
                logger.warning("options page %r is not registered — skipping", key)
                continue
            page = self._instantiate(cls)
            self._pages[key] = page
            self._stack.addWidget(page)
            self._nav.addItem(QListWidgetItem(key))  # 문구는 retranslate에서 채운다
            keys.append(key)
        self._page_keys = tuple(keys)

    def _instantiate(self, cls: type[OptionsPage]) -> OptionsPage:
        """페이지 생성자가 받는 키워드 인자만 골라 넘긴다.

        페이지마다 시그니처가 조금씩 다르다(`supports_i2i`를 받는 페이지는 인터페이스 페이지
        하나뿐이고, `**_extra`로 남는 인자를 흘려보내는 페이지도 있다). 셸이 특정 페이지를
        특별 취급하지 않도록 시그니처를 보고 결정한다.
        """
        optional = {"supports_i2i": self._supports_i2i, "settings_path": self._settings_path}
        try:
            params = inspect.signature(cls.__init__).parameters
        except (TypeError, ValueError):  # pragma: no cover - 방어적
            params = {}
        accepts_extra = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs = {name: value for name, value in optional.items() if accepts_extra or name in params}
        return cls(self._i18n, parent=self, **kwargs)

    # ── 공개 API ──────────────────────────────────────────────────────

    def select_page(self, key: str) -> None:
        """카테고리를 키로 선택한다. 없는 키는 무시한다."""
        if key not in self._pages:
            logger.warning("unknown options page key %r", key)
            return
        self._nav.setCurrentRow(self._page_keys.index(key))

    def current_page_key(self) -> str:
        """현재 표시 중인 카테고리 키 (페이지가 없으면 빈 문자열)."""
        row = self._nav.currentRow()
        if 0 <= row < len(self._page_keys):
            return self._page_keys[row]
        return ""

    def page(self, key: str) -> OptionsPage:
        """카테고리 키로 페이지 인스턴스를 얻는다 (테스트·셸 내부용)."""
        return self._pages[key]

    def draft(self) -> AppSettings:
        """편집 중인 사본. 라이브 `AppSettings`와는 다른 객체다."""
        return self._draft

    def notices(self) -> tuple[str, ...]:
        """저장 후 보여줄 안내 문구 i18n 키 (페이지들이 낸 것을 순서대로 중복 없이 모은다).

        표시는 Main_Window의 몫이다 (Req 2.6은 안내 주체를 Main_Window로 못박는다). 셸은
        `applied` 이후 조회할 수 있는 창구만 제공한다.
        """
        keys: list[str] = []
        for page in self._pages.values():
            for key in page.notices():
                if key not in keys:
                    keys.append(key)
        return tuple(keys)

    def save(self) -> bool:
        """커밋 → 검증 → 적용 → 부수효과 → 영속화 → `applied` (Req 1.5–1.8).

        `False`는 **검증 실패로 다이얼로그를 열어 둔 경우에만** 돌려준다 (Req 1.7). 파일 기록이
        실패해도 메모리 값은 이미 적용됐으므로 안내만 하고 닫는다 (Req 1.8) — 이때는 `True`다.
        """
        for page in self._pages.values():
            page.commit(self._draft)

        issues = validate_options(self._draft)
        if issues:
            self._report_issues(issues)
            return False

        changed = apply_draft(self._draft, self._live)
        logger.info("options saved — changed fields: %s", ", ".join(changed) or "(none)")
        self._apply_side_effects(changed)

        try:
            save_settings(self._live, self._settings_path)
        except OSError as exc:
            # Req 1.8: 메모리 값은 유지한 채 실패만 알린다. 다시 저장하라고 붙잡아 둬도
            # 같은 디스크 오류가 반복될 뿐이다.
            logger.error("failed to write settings: %s", exc)
            QMessageBox.critical(
                self,
                self._i18n.get_text("errors.title"),
                self._i18n.get_text("options.save_failed", exc),
            )

        self.applied.emit()
        self.accept()
        return True

    # ── 저장 파이프라인 내부 ──────────────────────────────────────────

    def _report_issues(self, issues: tuple[OptionIssue, ...]) -> None:
        """위반 항목이 있는 페이지로 전환하고 "{항목}: {메시지}" 목록을 보여 준다 (Req 1.7)."""
        tr = self._i18n.get_text
        self.select_page(issues[0].page)
        lines = [
            f"{tr(issue.field_key, *issue.field_args)}: {tr(issue.message_key, *issue.args)}"
            for issue in issues
        ]
        QMessageBox.warning(self, tr("validation.title"), "\n".join(lines))

    def _apply_side_effects(self, changed: tuple[str, ...]) -> None:
        """바뀐 경로에서만 부수효과를 낸다 (Req 6.2, 8.3, 8.4)."""
        if "language" in changed:
            self._i18n.set_language(self._live.language)  # retranslate는 구독 콜백이 처리한다
        if "debug_logging" in changed or "log_dir" in changed:
            self._reconfigure_logging()

    def _reconfigure_logging(self) -> None:
        """로그 디렉터리/상세 수준을 다시 구성한다. 실패하면 경로를 이전 값으로 되돌린다 (Req 8.7).

        `reconfigure_logging`은 쓸 수 없는 디렉터리를 먼저 걸러내므로 실패해도 로그는 이전
        디렉터리에 계속 쌓인다. 여기서는 그 사실을 알리고 `log_dir` 값(라이브 + 드래프트)만
        이전 값으로 맞춘다 — 라이브를 되돌려야 곧 기록될 `settings.json`에도 실패한 경로가
        남지 않는다.
        """
        live, before = self._live, self._before
        try:
            reconfigure_logging(
                live.log_dir_path(),
                debug=live.debug_logging,
                previous_dir=before.log_dir_path(),
                previous_debug=before.debug_logging,
            )
            return
        except LoggingConfigError as exc:
            logger.error("failed to reconfigure logging: %s", exc)
            QMessageBox.warning(
                self,
                self._i18n.get_text("errors.title"),
                self._i18n.get_text("options.log_dir_failed", exc),
            )

        live.log_dir = before.log_dir
        self._draft.log_dir = before.log_dir
        if live.debug_logging != before.debug_logging:
            # 폴더는 실패했어도 상세 수준 변경은 이전 폴더에서 적용해 준다 (Req 8.3).
            try:
                reconfigure_logging(
                    before.log_dir_path(),
                    debug=live.debug_logging,
                    previous_dir=before.log_dir_path(),
                    previous_debug=before.debug_logging,
                )
            except LoggingConfigError:
                logger.warning("could not apply debug level in previous log dir", exc_info=True)

    # ── i18n (Req 1.9, 1.10) ─────────────────────────────────────────

    def retranslate(self) -> None:
        """제목·카테고리 이름·설명·모든 페이지·두 버튼을 현재 언어로 다시 채운다."""
        tr = self._i18n.get_text
        self.setWindowTitle(tr("options.title"))
        for row, key in enumerate(self._page_keys):
            self._nav.item(row).setText(tr(f"options_nav.{key}"))
        self._refresh_description()
        for page in self._pages.values():
            page.retranslate()
        self._save_button.setText(tr("options.save"))
        self._cancel_button.setText(tr("options.cancel"))

    def _on_language_changed(self, _code: str) -> None:
        self.retranslate()

    def _unsubscribe(self) -> None:
        """언어 콜백을 떼어 낸다. 다이얼로그가 죽은 뒤 콜백이 남으면 죽은 위젯을 만진다."""
        if self._subscribed:
            self._i18n.unsubscribe(self._on_language_changed)
            self._subscribed = False

    # ── Qt 이벤트 ────────────────────────────────────────────────────

    def done(self, result: int) -> None:
        self._unsubscribe()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 이름
        # 창 닫기 = 취소. 드래프트는 그대로 버려지고 `_live`는 손대지 않는다 (Req 1.6).
        self._unsubscribe()
        super().closeEvent(event)

    # ── 내부 ─────────────────────────────────────────────────────────

    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._page_keys):
            self._stack.setCurrentIndex(row)
        self._refresh_description()

    def _refresh_description(self) -> None:
        key = self.current_page_key()
        self._description.setText(self._i18n.get_text(f"options_nav.{key}_desc") if key else "")

    def _on_save_clicked(self) -> None:
        self.save()
