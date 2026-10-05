"""자동완성 Options_Page (KEY=`"completion"`, Req 7.1, 7.2, 7.4–7.6).

프롬프트 입력란에 뜨는 두 자동완성을 한 화면에 모은다. 스위치가 둘인 이유는 **읽어 오는 곳이
다르기 때문**이다 — 태그는 여기서 지정하는 DB 파일, 와일드카드는 `폴더 경로` 화면의 와일드카드
폴더다. 한쪽을 꺼도 다른 쪽은 그대로 뜬다.

v0.7.8 이전에는 이 화면 이름이 `태그 자동완성`이었고 WD14 태거 모델 설정까지 들고 있었다.
모델은 결국 "어느 폴더의 어느 파일"이라 `폴더 경로` 화면으로 옮겼다 (`wd14_section.py`).

아래는 태그 자동완성 부분의 원래 설명이다.

경로 입력란 + `찾아보기` + 읽기 전용 상태 문구뿐이다. 입력란이 비면 앱에 동봉된 기본 태그 DB를
쓴다 (`core.tag_completer.resolve_database_path`). 상태 문구는 경로가 바뀔 때마다 **임시**
`TagCompleter`로 시험 로드해서 만든다 — 라이브 완성기는 저장 후 Main_Window가 다시 로드한다
(Req 7.3). 시험 로드가 실패해도 저장은 막지 않는다: 상태 문구만 비활성으로 바뀌고 나머지 항목은
그대로 반영된다 (Req 7.5).

세 번째 묶음 `등록 와일드카드 자동완성`은 고른 와일드카드 파일의 **줄**을 태그 후보에 섞는다
(`core.wildcards.line_source`). 목록 순서가 곧 우선순위이자 색 순서라, 드롭다운과 같은 색으로
이름을 칠해 둔다. 추가할 이름은 와일드카드 폴더(드래프트의 `wildcards_dir`)에서 고른다.

시험 로드 결과는 `_status`에 상태로 저장하고 문구 생성은 `retranslate`가 담당한다. 언어가 바뀔
때마다 태그 DB를 다시 읽지 않기 위해서다 (파일이 수십만 행일 수 있다).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings
from ...core.tag_completer import TagCompleter, resolve_database_path
from ...core.wildcards.catalog import scan_wildcard_names
from ...core.wildcards.line_source import WildcardLineSource
from ..tag_completer_dropdown import source_color
from . import OptionsPage, register_page

logger = logging.getLogger(__name__)

#: 찾아보기 파일 필터 (Req 7.2). 확장자는 `TagCompleter`가 읽는 두 형식이다.
TAG_FILE_FILTER = "Tag database (*.csv *.json);;All files (*)"

#: 상태 문구 i18n 키 — 경로가 비었을 때(= 내장 DB) / 로드 성공 / 로드 실패.
STATUS_BUNDLED_KEY = "options.tags_bundled"
STATUS_EMPTY_KEY = "options.tags_disabled_empty"
STATUS_LOADED_KEY = "options.tags_loaded"
STATUS_ERROR_KEY = "options.tags_disabled_error"

__all__ = [
    "STATUS_BUNDLED_KEY",
    "STATUS_EMPTY_KEY",
    "STATUS_ERROR_KEY",
    "STATUS_LOADED_KEY",
    "TAG_FILE_FILTER",
    "CompletionPage",
]


@register_page
class CompletionPage(OptionsPage):
    """태그 자동완성(DB 파일, Req 7.1) + 와일드카드 이름 자동완성."""

    KEY = "completion"

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        #: 마지막 시험 로드 결과: (상태 키, 태그 개수). 문구는 여기서 만들어진다.
        self._status: tuple[str, int] = (STATUS_EMPTY_KEY, 0)
        #: 시험 로드를 두 번 하지 않기 위한 직전 입력값.
        self._probed_text: str | None = None
        #: 추가할 이름을 고를 와일드카드 폴더 (`load`에서 드래프트 값으로 채운다).
        self._wildcards_dir = ""

        layout = QVBoxLayout(self)

        # ── 태그 자동완성 (DB 파일을 읽는다) ──────────────────────────
        self.tags_title = QLabel(self)
        self.tags_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.tags_title)

        self.enabled_check = QCheckBox(self)
        layout.addWidget(self.enabled_check)

        form = QFormLayout()
        self.path_label = QLabel(self)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self)
        self.browse_button = QPushButton(self)
        self.browse_button.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)
        form.addRow(self.path_label, path_row)
        layout.addLayout(form)

        self.status_label = QLabel(self)  # 읽기 전용 (Req 7.1)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self.status_label)

        # ── 와일드카드 이름 자동완성 (와일드카드 폴더를 읽는다) ────────
        layout.addSpacing(12)
        self.wildcards_title = QLabel(self)
        self.wildcards_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.wildcards_title)

        self.wildcard_autocomplete_check = QCheckBox(self)
        layout.addWidget(self.wildcard_autocomplete_check)
        self.wildcard_autocomplete_hint = QLabel(self)
        self.wildcard_autocomplete_hint.setWordWrap(True)
        self.wildcard_autocomplete_hint.setEnabled(False)  # 읽기 전용 안내
        layout.addWidget(self.wildcard_autocomplete_hint)

        # ── 등록 와일드카드 줄 자동완성 (고른 와일드카드 파일을 읽는다) ──
        layout.addSpacing(12)
        self.wildcard_tags_title = QLabel(self)
        self.wildcard_tags_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.wildcard_tags_title)

        self.wildcard_tags_check = QCheckBox(self)
        layout.addWidget(self.wildcard_tags_check)
        self.wildcard_tags_hint = QLabel(self)
        self.wildcard_tags_hint.setWordWrap(True)
        self.wildcard_tags_hint.setEnabled(False)  # 읽기 전용 안내
        layout.addWidget(self.wildcard_tags_hint)

        sources_row = QHBoxLayout()
        self.wildcard_tags_list = QListWidget(self)
        self.wildcard_tags_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # 끌어서 순서를 바꾼다 — 순서가 후보 우선순위이자 색 순서다
        self.wildcard_tags_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.wildcard_tags_list.setMaximumHeight(120)
        self.wildcard_tags_list.model().rowsMoved.connect(self._refresh_sources)
        sources_row.addWidget(self.wildcard_tags_list, 1)
        sources_buttons = QVBoxLayout()
        self.wildcard_tags_add_button = QPushButton(self)
        self.wildcard_tags_add_button.clicked.connect(self._add_sources)
        self.wildcard_tags_remove_button = QPushButton(self)
        self.wildcard_tags_remove_button.clicked.connect(self._remove_sources)
        sources_buttons.addWidget(self.wildcard_tags_add_button)
        sources_buttons.addWidget(self.wildcard_tags_remove_button)
        sources_buttons.addStretch(1)
        sources_row.addLayout(sources_buttons)
        layout.addLayout(sources_row)

        self.wildcard_tags_status = QLabel(self)
        self.wildcard_tags_status.setWordWrap(True)
        layout.addWidget(self.wildcard_tags_status)

        layout.addStretch(1)

        # 경로가 바뀔 때마다 시험 로드로 상태 문구를 갱신한다 (Req 7.4–7.6).
        self.path_edit.textChanged.connect(self._refresh_status)

        self.retranslate()

    # ── OptionsPage 계약 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self.enabled_check.setChecked(draft.tag_autocomplete_enabled)
        self.path_edit.setText(draft.tag_database_path)
        self.wildcard_autocomplete_check.setChecked(draft.wildcard_autocomplete_enabled)
        self.wildcard_tags_check.setChecked(draft.wildcard_tag_autocomplete_enabled)
        self._wildcards_dir = draft.wildcards_dir
        self._set_sources(draft.wildcard_tag_sources)
        # 값이 그대로면 textChanged가 오지 않으므로 직접 갱신한다.
        self._refresh_status()

    def commit(self, draft: AppSettings) -> None:
        """공백만 남은 태그 DB 경로는 빈 값(= 내장 DB)으로 정규화한다."""
        draft.tag_autocomplete_enabled = self.enabled_check.isChecked()
        draft.tag_database_path = self.path_edit.text().strip()
        draft.wildcard_autocomplete_enabled = self.wildcard_autocomplete_check.isChecked()
        draft.wildcard_tag_autocomplete_enabled = self.wildcard_tags_check.isChecked()
        draft.wildcard_tag_sources = self.sources()

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.tags_title.setText(tr("options.tags_title"))
        self.enabled_check.setText(tr("options.tags_autocomplete_enabled"))
        self.path_label.setText(tr("options.tag_database_path"))
        self.browse_button.setText(tr("options.browse"))
        key, count = self._status
        self.status_label.setText(
            tr(key, count) if key in (STATUS_LOADED_KEY, STATUS_BUNDLED_KEY) else tr(key)
        )
        self.wildcards_title.setText(tr("options.wildcards_title"))
        self.wildcard_autocomplete_check.setText(tr("options.wildcards_autocomplete_enabled"))
        self.wildcard_autocomplete_hint.setText(tr("options.wildcards_autocomplete_hint"))
        self.wildcard_tags_title.setText(tr("options.wildcard_tags_title"))
        self.wildcard_tags_check.setText(tr("options.wildcard_tags_enabled"))
        self.wildcard_tags_hint.setText(tr("options.wildcard_tags_hint"))
        self.wildcard_tags_add_button.setText(tr("options.wildcard_tags_add"))
        self.wildcard_tags_remove_button.setText(tr("options.wildcard_tags_remove"))
        self._refresh_sources_status()

    # ── 등록 와일드카드 ─────────────────────────────────────────────────

    def sources(self) -> list[str]:
        """목록에 보이는 순서 그대로의 등록 이름."""
        return [self.wildcard_tags_list.item(i).text() for i in range(self.wildcard_tags_list.count())]

    def _set_sources(self, names: list[str]) -> None:
        self.wildcard_tags_list.clear()
        for name in names:
            if name not in self.sources():
                self.wildcard_tags_list.addItem(QListWidgetItem(name))
        self._refresh_sources()

    def _add_sources(self) -> None:
        """와일드카드 폴더에서 아직 등록하지 않은 이름을 골라 뒤에 붙인다."""
        taken = set(self.sources())
        available = [name for name in scan_wildcard_names(self._wildcards_dir) if name not in taken]
        if not available:
            QMessageBox.information(
                self,
                self._i18n.get_text("options.wildcard_tags_title"),
                self._i18n.get_text("options.wildcard_tags_no_files"),
            )
            return
        chosen = self._pick_sources(available)
        if chosen:
            self._set_sources(self.sources() + chosen)

    def _remove_sources(self) -> None:
        selected = {item.text() for item in self.wildcard_tags_list.selectedItems()}
        if selected:
            self._set_sources([name for name in self.sources() if name not in selected])

    def _pick_sources(self, available: list[str]) -> list[str]:
        """체크 목록 대화상자로 여러 개를 고른다 (테스트는 이 메서드를 바꿔 끼운다)."""
        dialog = QDialog(self)
        dialog.setWindowTitle(self._i18n.get_text("options.wildcard_tags_pick_title"))
        box = QVBoxLayout(dialog)
        picker = QListWidget(dialog)
        for name in available:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            picker.addItem(item)
        # 이름을 눌러도 체크가 바뀌게 — 작은 체크 상자만 노리지 않아도 된다
        picker.itemClicked.connect(
            lambda item: item.setCheckState(
                Qt.CheckState.Unchecked
                if item.checkState() == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )
        )
        box.addWidget(picker)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return []
        return [
            picker.item(i).text()
            for i in range(picker.count())
            if picker.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _refresh_sources(self, *_args) -> None:
        """이름을 드롭다운과 같은 색으로 칠하고 상태 문구를 다시 만든다."""
        for i in range(self.wildcard_tags_list.count()):
            self.wildcard_tags_list.item(i).setForeground(source_color(i))
        self._refresh_sources_status()

    def _refresh_sources_status(self) -> None:
        """등록 개수·줄 개수, 폴더에 없는 이름을 알린다 (파일은 작아서 매번 읽어도 된다)."""
        tr = self._i18n.get_text
        names = self.sources()
        if not names:
            self.wildcard_tags_status.setText(tr("options.wildcard_tags_none"))
            return
        probe = WildcardLineSource(self._wildcards_dir, names)
        text = tr("options.wildcard_tags_status", len(names), probe.line_count())
        missing = probe.missing()
        if missing:
            text += "\n" + tr("options.wildcard_tags_missing", ", ".join(missing))
        self.wildcard_tags_status.setText(text)

    # ── 내부 ────────────────────────────────────────────────────────────

    def _browse(self) -> None:
        """CSV·JSON을 대상으로 하는 파일 선택 다이얼로그 (Req 7.2)."""
        tr = self._i18n.get_text
        current = self.path_edit.text().strip()
        start_dir = str(Path(current).parent) if current else ""
        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            tr("options.choose_file", tr("options.tag_database_path")),
            start_dir,
            TAG_FILE_FILTER,
        )
        if chosen:
            self.path_edit.setText(str(Path(chosen).resolve()))

    def _refresh_status(self) -> None:
        """임시 `TagCompleter`로 시험 로드해 상태를 갱신한다 (Req 7.4–7.6)."""
        text = self.path_edit.text().strip()
        if self._probed_text == text:
            return
        self._probed_text = text
        self._status = self._probe(resolve_database_path(text), bundled=not text)
        self.retranslate()

    @staticmethod
    def _probe(path: Path, *, bundled: bool = False) -> tuple[str, int]:
        """경로 하나를 시험 로드한다. 어떤 파일이든 다이얼로그를 죽이면 안 된다 (Req 7.5).

        `bundled`는 입력란이 비어 앱에 동봉된 기본 DB를 쓰는 경우다 — 문구만 달라진다.
        """
        probe = TagCompleter(path)
        try:
            ok = probe.load()
        except Exception:  # 이진 파일 등 TagCompleter가 예상하지 못한 입력
            logger.debug("tag database probe failed: %s", path, exc_info=True)
            return (STATUS_ERROR_KEY, 0)
        if not ok:
            return (STATUS_EMPTY_KEY, 0) if bundled else (STATUS_ERROR_KEY, 0)
        return (STATUS_BUNDLED_KEY if bundled else STATUS_LOADED_KEY, probe.tag_count)
