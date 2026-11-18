"""백업 내보내기 / 가져오기 창 — 어떤 구획을 담을지(풀지) 고른다.

실제 zip 읽기·쓰기는 `core/backup.py`가 한다. 이 창은 구획 체크박스와, 가져올 때의
`이미 있는 파일 덮어쓰기` 선택만 받는다.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..core.backup import SECTION_SETTINGS, SECTIONS
from ..core.i18n.manager import I18nManager


class BackupDialog(QDialog):
    """구획 선택 창.

    `counts`를 주면 **가져오기** 모드다 — 백업에 든 구획만 고를 수 있고, 구획마다 몇 개가
    들었는지 보이며, 덮어쓰기 체크박스가 나온다. 없으면 내보내기 모드다.
    """

    def __init__(
        self,
        i18n: I18nManager,
        *,
        counts: dict[str, int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._importing = counts is not None
        tr = i18n.get_text

        self.setWindowTitle(tr("backup.import_title" if self._importing else "backup.export_title"))
        layout = QVBoxLayout(self)
        intro = QLabel(tr("backup.import_intro" if self._importing else "backup.export_intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.section_checks: dict[str, QCheckBox] = {}
        for section in SECTIONS:
            label = tr(f"backup.section_{section}")
            available = True
            if counts is not None:
                amount = int(counts.get(section, 0) or 0)
                available = amount > 0
                label = f"{label}  ({amount})"
            check = QCheckBox(label)
            check.setChecked(available)
            check.setEnabled(available)
            layout.addWidget(check)
            self.section_checks[section] = check

        self.overwrite_check = QCheckBox(tr("backup.overwrite"))
        self.overwrite_check.setToolTip(tr("backup.overwrite_hint"))
        self.overwrite_check.setVisible(self._importing)
        layout.addWidget(self.overwrite_check)

        note = QLabel(tr("backup.import_note" if self._importing else "backup.export_note"))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            tr("backup.import_button" if self._importing else "backup.export_button")
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("dialogs.cancel"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        for check in self.section_checks.values():
            check.toggled.connect(self._refresh_ok)
        self._refresh_ok()

    def sections(self) -> set[str]:
        """고른 구획."""
        return {
            section
            for section, check in self.section_checks.items()
            if check.isEnabled() and check.isChecked()
        }

    @property
    def overwrite(self) -> bool:
        return self.overwrite_check.isChecked()

    @property
    def restores_settings(self) -> bool:
        return SECTION_SETTINGS in self.sections()

    def _refresh_ok(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.sections()))


__all__ = ["BackupDialog"]
