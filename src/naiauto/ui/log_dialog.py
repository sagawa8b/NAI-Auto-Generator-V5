"""로그 뷰어 — 일반 로그와 크래시 로그를 앱 안에서 바로 확인한다.

로그 파일 위치를 찾아 헤매지 않고, 문제가 난 직후 내용을 복사해 공유할 수 있게
하는 것이 목적이다.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n.manager import I18nManager
from ..core.logging_setup import read_log

LEVELS = ("ALL", "DEBUG", "INFO", "WARNING", "ERROR")


def filter_lines(text: str, level: str) -> str:
    """선택한 레벨 이상만 남긴다. 레벨 표시가 없는 줄(스택 트레이스)은 유지."""
    if level == "ALL":
        return text
    order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    allowed = set(order[order.index(level) :])
    kept: list[str] = []
    keeping = False
    for line in text.splitlines():
        found = next((name for name in order if f" {name} " in f" {line} "), None)
        if found is not None:
            keeping = found in allowed
        if keeping or found is None and kept:
            kept.append(line)
    return "\n".join(kept)


class LogDialog(QDialog):
    """로그 파일 내용을 보여주는 읽기 전용 뷰어."""

    def __init__(
        self,
        i18n: I18nManager,
        log_file: Path,
        crash_file: Path,
        *,
        debug_enabled: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._log_file = Path(log_file)
        self._crash_file = Path(crash_file)
        tr = i18n.get_text

        self.setWindowTitle(tr("logs.title"))
        self.resize(900, 600)
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem(tr("logs.source_app"), userData="app")
        self.source_combo.addItem(tr("logs.source_crash"), userData="crash")
        self.source_combo.currentIndexChanged.connect(self.refresh)
        self.level_label = QLabel(tr("logs.level"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(LEVELS)
        self.level_combo.currentIndexChanged.connect(self.refresh)
        self.debug_check = QCheckBox(tr("logs.debug_mode"))
        self.debug_check.setChecked(debug_enabled)
        self.debug_check.setToolTip(tr("logs.debug_hint"))
        top.addWidget(self.source_combo)
        top.addWidget(self.level_label)
        top.addWidget(self.level_combo)
        top.addWidget(self.debug_check)
        top.addStretch(1)
        layout.addLayout(top)

        self.path_label = QLabel()
        self.path_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.path_label)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setFont(QFont("Monospace"))
        layout.addWidget(self.view, stretch=1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton(tr("logs.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.copy_button = QPushButton(tr("logs.copy"))
        self.copy_button.clicked.connect(self._copy)
        self.open_folder_button = QPushButton(tr("logs.open_folder"))
        self.open_folder_button.clicked.connect(self._open_folder)
        self.close_button = QPushButton(tr("logs.close"))
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.open_folder_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.refresh()

    # ── 내용 ─────────────────────────────────────────────

    @property
    def debug_enabled(self) -> bool:
        return self.debug_check.isChecked()

    def current_path(self) -> Path:
        return self._crash_file if self.source_combo.currentData() == "crash" else self._log_file

    def refresh(self) -> None:
        path = self.current_path()
        self.path_label.setText(str(path))
        text = read_log(path)
        if self.source_combo.currentData() == "app":
            text = filter_lines(text, self.level_combo.currentText())
        self.level_combo.setEnabled(self.source_combo.currentData() == "app")
        self.level_label.setEnabled(self.level_combo.isEnabled())
        self.view.setPlainText(text or self._i18n.get_text("logs.empty"))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.view.toPlainText())
        self.copy_button.setText(self._i18n.get_text("logs.copied"))

    def _open_folder(self) -> None:
        folder = self.current_path().parent
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


__all__ = ["LEVELS", "LogDialog", "filter_lines"]
