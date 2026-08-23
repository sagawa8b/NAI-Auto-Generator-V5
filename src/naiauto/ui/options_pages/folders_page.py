"""폴더 경로 Options_Page (KEY="folders", Req 2.1–2.4, 2.6).

네 경로(`save_dir`, `wildcards_dir`, `presets_dir`, `artist_combos_dir`)를 같은 모양의 행으로 다룬다: 경로 입력란 +
`찾아보기` + `폴더 열기`. 세 행의 차이는 필드 이름·라벨 키·스키마 기본값뿐이라 `_PATH_FIELDS`
테이블 하나로 기술하고 위젯은 루프로 만든다.

`commit`은 검증하지 않는다 — 공백뿐인 입력을 스키마 기본 경로로 되돌리는 **정규화**만 한다
(Req 2.4). 와일드카드 폴더는 `ui/app.py`에서 주입되므로 바뀌면 다음 실행부터 적용된다는 안내를
`notices()`로 알린다 (Req 2.6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import (
    AppSettings,
    default_artist_combos_dir,
    default_presets_dir,
    default_save_dir,
    default_wildcards_dir,
)
from . import OptionsPage, open_in_file_manager, register_page

#: 저장 후 안내가 필요한 필드 → 안내 문구 i18n 키 (Req 2.6).
WILDCARDS_RESTART_NOTICE = "options.wildcards_restart_note"

__all__ = ["WILDCARDS_RESTART_NOTICE", "FoldersPage"]


@dataclass(frozen=True)
class _PathField:
    """경로 행 하나의 기술 — 위젯은 이 테이블에서 만들어진다."""

    name: str  # AppSettings 필드 이름
    label_key: str  # 라벨 i18n 키
    default: Callable[[], Path]  # 스키마 기본 경로 (Req 2.4)


_PATH_FIELDS: tuple[_PathField, ...] = (
    _PathField("save_dir", "options.folder_save_dir", default_save_dir),
    _PathField("wildcards_dir", "options.folder_wildcards_dir", default_wildcards_dir),
    _PathField("presets_dir", "options.folder_presets_dir", default_presets_dir),
    _PathField("artist_combos_dir", "options.folder_artist_combos_dir", default_artist_combos_dir),
)


@dataclass
class _PathRow:
    """한 행의 위젯 묶음."""

    field: _PathField
    label: QLabel
    edit: QLineEdit
    browse_button: QPushButton
    open_button: QPushButton


@register_page
class FoldersPage(OptionsPage):
    """결과 / 와일드카드 / 프리셋 폴더 경로 (Req 2.1)."""

    KEY = "folders"

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        #: `load` 시점의 값 — `notices()`가 변경 여부를 판단하는 기준 (Req 2.6).
        self._loaded: dict[str, str] = {}
        self._rows: dict[str, _PathRow] = {}

        layout = QVBoxLayout(self)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        for index, field in enumerate(_PATH_FIELDS):
            row = self._build_row(field)
            grid.addWidget(row.label, index, 0)
            grid.addWidget(row.edit, index, 1)
            grid.addWidget(row.browse_button, index, 2)
            grid.addWidget(row.open_button, index, 3)
            self._rows[field.name] = row
        layout.addLayout(grid)
        layout.addStretch(1)

        # 편의 접근자 (design.md 2.1의 위젯 이름)
        self.save_dir_edit = self._rows["save_dir"].edit
        self.wildcards_dir_edit = self._rows["wildcards_dir"].edit
        self.presets_dir_edit = self._rows["presets_dir"].edit
        self.artist_combos_dir_edit = self._rows["artist_combos_dir"].edit

        self.retranslate()

    def _build_row(self, field: _PathField) -> _PathRow:
        row = _PathRow(
            field=field,
            label=QLabel(),
            edit=QLineEdit(),
            browse_button=QPushButton(),
            open_button=QPushButton(),
        )
        row.edit.setPlaceholderText(str(field.default()))
        row.browse_button.clicked.connect(lambda _checked=False, f=field: self._browse(f))
        row.open_button.clicked.connect(lambda _checked=False, f=field: self._open(f))
        return row

    # ── OptionsPage 계약 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self._loaded = {}
        for field in _PATH_FIELDS:
            value = str(getattr(draft, field.name))
            self._loaded[field.name] = self._normalize(field, value)
            self._rows[field.name].edit.setText(value)

    def commit(self, draft: AppSettings) -> None:
        """빈 입력을 스키마 기본 경로로 되돌린다 — 오류가 아니라 정규화다 (Req 2.4)."""
        for field in _PATH_FIELDS:
            setattr(draft, field.name, self._normalize(field, self._rows[field.name].edit.text()))

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        for row in self._rows.values():
            row.label.setText(tr(row.field.label_key))
            row.browse_button.setText(tr("options.browse"))
            row.open_button.setText(tr("options.open_folder"))

    def notices(self) -> tuple[str, ...]:
        """와일드카드 폴더가 바뀌었으면 재시작 안내 키를 돌려준다 (Req 2.6)."""
        field = _PATH_FIELDS[1]
        current = self._normalize(field, self._rows[field.name].edit.text())
        if current != self._loaded.get(field.name, current):
            return (WILDCARDS_RESTART_NOTICE,)
        return ()

    # ── 내부 ────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(field: _PathField, text: str) -> str:
        return text.strip() or str(field.default())

    def _browse(self, field: _PathField) -> None:
        """현재 값을 시작 위치로 하는 디렉터리 선택 다이얼로그 (Req 2.2)."""
        tr = self._i18n.get_text
        row = self._rows[field.name]
        chosen = QFileDialog.getExistingDirectory(
            self,
            tr("options.choose_folder", tr(field.label_key)),
            self._normalize(field, row.edit.text()),
        )
        if chosen:
            row.edit.setText(str(Path(chosen).resolve()))

    def _open(self, field: _PathField) -> None:
        """경로를 만든 뒤 OS 파일 탐색기로 연다 (Req 2.3)."""
        open_in_file_manager(
            self._normalize(field, self._rows[field.name].edit.text()),
            self._i18n.get_text,
            parent=self,
        )
