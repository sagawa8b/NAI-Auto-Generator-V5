"""로그인 다이얼로그 — pst- 영구 API 토큰 방식 (V5 스모크로 검증된 경로).

두 가지 모습을 가진다 (V4 `gui_dialog.LoginDialog`와 같은 구조):

- **로그아웃 상태** — 토큰 입력 + "기억하기". 로그인에 성공하면 `accept()`.
- **로그인 상태** — 현재 로그인되어 있다는 안내와 로그아웃 버튼만. 로그아웃을 확인하면
  `logout_requested`를 세우고 `accept()` 한다. 실제 로그아웃(세션 정리·키링 삭제)은
  호출한 쪽이 한다 — 다이얼로그는 자격 증명을 직접 만지지 않는다.
"""

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
    QPushButton,
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
        logged_in: bool = False,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._validate = validate
        self._logged_in = logged_in
        #: 사용자가 로그아웃을 확인했는가 (accept 후 호출한 쪽이 본다).
        self.logout_requested = False
        tr = i18n.get_text

        self.setWindowTitle(tr("dialogs.login_title"))
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        if logged_in:
            self._build_logged_in(layout)
        else:
            self._build_login_form(layout, initial_token)

    # ── 로그인 상태 ──────────────────────────────────────────────────

    def _build_logged_in(self, layout: QVBoxLayout) -> None:
        tr = self._i18n.get_text

        state_label = QLabel(tr("dialogs.logged_in"))
        state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(state_label)

        method_label = QLabel(tr("dialogs.api_key_login_active"))
        method_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(method_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.logout_button = QPushButton(tr("dialogs.logout_button"))
        self.logout_button.clicked.connect(self._confirm_logout)
        buttons.addButton(self.logout_button, QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _confirm_logout(self) -> None:
        tr = self._i18n.get_text
        answer = QMessageBox.question(
            self,
            tr("dialogs.logout_confirm_title"),
            tr("dialogs.logout_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.logout_requested = True
        self.accept()

    # ── 로그아웃 상태 ────────────────────────────────────────────────

    def _build_login_form(self, layout: QVBoxLayout, initial_token: str) -> None:
        tr = self._i18n.get_text

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
