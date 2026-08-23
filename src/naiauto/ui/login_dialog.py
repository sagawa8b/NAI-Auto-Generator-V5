"""로그인 다이얼로그 — pst- 영구 API 토큰 방식 (V5 스모크로 검증된 경로)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from ..core.i18n.manager import I18nManager


class LoginDialog(QDialog):
    """validate(token) 콜러블이 성공(예외 없음)하면 accept.
    validate는 보통 NAISession.login_with_token + NAIClient.get_anlas 조합."""

    def __init__(
        self,
        i18n: I18nManager,
        validate: Callable[[str], None],
        parent=None,
        initial_token: str = "",
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._validate = validate
        tr = i18n.get_text

        self.setWindowTitle(tr("dialogs.login_title"))
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("dialogs.login_welcome")))

        layout.addWidget(QLabel(tr("dialogs.token")))
        self.token_edit = QLineEdit(initial_token)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("pst-...")
        layout.addWidget(self.token_edit)

        help_label = QLabel(tr("dialogs.token_help"))
        help_label.setWordWrap(True)
        help_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(help_label)

        self.remember_check = QCheckBox(tr("dialogs.remember"))
        self.remember_check.setChecked(True)
        layout.addWidget(self.remember_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("dialogs.login_button"))
        buttons.accepted.connect(self._try_login)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def token(self) -> str:
        return self.token_edit.text().strip()

    @property
    def remember(self) -> bool:
        return self.remember_check.isChecked()

    def _try_login(self) -> None:
        tr = self._i18n.get_text
        try:
            self._validate(self.token)
        except Exception as e:
            QMessageBox.warning(self, tr("errors.title"), f"{tr('dialogs.login_failed')}\n\n{e}")
            return
        self.accept()
