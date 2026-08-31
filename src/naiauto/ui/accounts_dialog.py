"""API 계정 관리 다이얼로그 — pst- 토큰을 최대 4개 등록해 두고 골라 쓴다.

계정을 여러 개 쓰는 사용자를 위한 창이다. 각 행은 토큰 입력란 + `저장 및 전환` +
`삭제`이고, `저장 및 전환`을 누르면 그 토큰으로 실제 로그인해 본 뒤(호출한 쪽이 준
`switch` 콜러블) 성공했을 때만 슬롯과 활성 토큰에 반영한다 — 못 쓰는 토큰으로 갈아타
생성이 줄줄이 실패하는 일이 없도록.

토큰 저장은 `core.settings.accounts`(키링)가 맡는다. 다이얼로그는 세션을 직접 만지지
않는다 (로그인 다이얼로그와 같은 규칙).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n.manager import I18nManager
from ..core.settings import accounts, credentials


@dataclass
class _Row:
    """계정 한 칸의 위젯 묶음."""

    index: int
    label: QLabel
    edit: QLineEdit
    status: QLabel
    switch_button: QPushButton
    delete_button: QPushButton


class AccountsDialog(QDialog):
    """계정 슬롯 편집 + 활성 계정 전환.

    Parameters
    ----------
    i18n : I18nManager
        번역기.
    switch : Callable[[str], None]
        토큰으로 실제 로그인해 보는 콜러블. 실패하면 예외를 던지고, 그 전 계정을
        되돌려 놓는 책임도 이쪽에 있다 (`MainWindow._switch_account`).
    current_token : str
        지금 로그인에 쓰고 있는 토큰 (없으면 빈 문자열).
    """

    def __init__(
        self,
        i18n: I18nManager,
        switch: Callable[[str], None],
        current_token: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._switch = switch
        self._active_token = current_token
        #: 계정을 실제로 바꿨는가 — 창을 닫은 뒤 호출한 쪽이 상태바를 맞추는 데 쓴다.
        self.switched = False

        tr = i18n.get_text
        self.setWindowTitle(tr("accounts.title"))
        self.setMinimumWidth(640)

        layout = QVBoxLayout(self)

        self.intro_label = QLabel(tr("accounts.intro", accounts.MAX_ACCOUNTS))
        self.intro_label.setWordWrap(True)
        layout.addWidget(self.intro_label)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self.header_account = QLabel(tr("accounts.column_account"))
        self.header_token = QLabel(tr("accounts.column_token"))
        self.header_status = QLabel(tr("accounts.column_status"))
        grid.addWidget(self.header_account, 0, 0)
        grid.addWidget(self.header_token, 0, 1)
        grid.addWidget(self.header_status, 0, 2)

        # 이미 로그인한 토큰이 슬롯에 없으면 빈 칸에 넣어 둔다 — 창을 처음 열었을 때
        # "지금 쓰는 계정"이 1번으로 보이도록.
        self._tokens = accounts.adopt_active_token(current_token)

        self._rows: list[_Row] = []
        for index in range(accounts.MAX_ACCOUNTS):
            row = self._build_row(index)
            grid.addWidget(row.label, index + 1, 0)
            grid.addWidget(row.edit, index + 1, 1)
            grid.addWidget(row.status, index + 1, 2)
            grid.addWidget(row.switch_button, index + 1, 3)
            grid.addWidget(row.delete_button, index + 1, 4)
            self._rows.append(row)
        layout.addLayout(grid)

        self.show_tokens_check = QCheckBox(tr("accounts.show_tokens"))
        self.show_tokens_check.toggled.connect(self._on_show_tokens)
        layout.addWidget(self.show_tokens_check)

        self.note_label = QLabel(
            tr("accounts.keyring_note") if credentials.is_available() else tr("accounts.keyring_missing")
        )
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet("color: gray;")
        layout.addWidget(self.note_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh()

    # ── 조립 ────────────────────────────────────────────────────────────

    def _build_row(self, index: int) -> _Row:
        tr = self._i18n.get_text
        edit = QLineEdit(self._tokens[index])
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(f"{accounts.TOKEN_PREFIX}...")
        edit.textChanged.connect(self._refresh_buttons)
        row = _Row(
            index=index,
            label=QLabel(tr("accounts.slot", index + 1)),
            edit=edit,
            status=QLabel(),
            switch_button=QPushButton(),
            delete_button=QPushButton(tr("accounts.delete")),
        )
        row.switch_button.clicked.connect(lambda _checked=False, i=index: self._on_switch(i))
        row.delete_button.clicked.connect(lambda _checked=False, i=index: self._on_delete(i))
        return row

    # ── 표시 갱신 ───────────────────────────────────────────────────────

    def _refresh(self) -> None:
        tr = self._i18n.get_text
        active = accounts.active_index(self._tokens, self._active_token)
        for row in self._rows:
            token = self._tokens[row.index]
            if row.index == active:
                row.status.setText(tr("accounts.status_active"))
            elif token:
                row.status.setText(tr("accounts.status_saved"))
            else:
                row.status.setText(tr("accounts.status_empty"))
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        """입력 상태에 따라 버튼을 열고 닫는다 (지금 쓰는 계정은 전환할 것이 없다)."""
        tr = self._i18n.get_text
        active = accounts.active_index(self._tokens, self._active_token)
        for row in self._rows:
            typed = row.edit.text().strip()
            unchanged_active = row.index == active and typed == self._tokens[row.index]
            row.switch_button.setText(
                tr("accounts.in_use") if unchanged_active else tr("accounts.save_and_switch")
            )
            row.switch_button.setEnabled(bool(typed) and not unchanged_active)
            row.delete_button.setEnabled(bool(self._tokens[row.index]))

    def _on_show_tokens(self, checked: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        for row in self._rows:
            row.edit.setEchoMode(mode)

    # ── 동작 ────────────────────────────────────────────────────────────

    def _on_switch(self, index: int) -> None:
        """토큰을 검증하고, 성공하면 슬롯과 활성 토큰에 저장한다."""
        tr = self._i18n.get_text
        token = self._rows[index].edit.text().strip()
        if not accounts.is_valid_token(token):
            QMessageBox.warning(self, tr("errors.title"), tr("accounts.invalid_token"))
            return

        try:
            self._switch(token)
        except Exception as e:
            QMessageBox.warning(self, tr("errors.title"), f"{tr('accounts.switch_failed')}\n\n{e}")
            return

        self._tokens[index] = token
        self._active_token = token
        self.switched = True
        if not accounts.save_token(index, token) or not credentials.save_credential(
            credentials.TOKEN_KEY, token
        ):
            # 세션은 이미 바뀌었다 — 저장만 실패했으므로 그 사실만 알린다.
            QMessageBox.warning(self, tr("errors.title"), tr("accounts.keyring_missing"))
        self._refresh()

    def _on_delete(self, index: int) -> None:
        tr = self._i18n.get_text
        answer = QMessageBox.question(
            self,
            tr("accounts.delete_confirm_title"),
            tr("accounts.delete_confirm", index + 1),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        was_active = self._tokens[index] == self._active_token and bool(self._active_token)
        accounts.delete_token(index)
        self._tokens[index] = ""
        self._rows[index].edit.clear()
        if was_active:
            # 지운 계정으로 다음 실행 때 자동 로그인하면 안 된다. 지금 세션은 그대로 둔다
            # (생성 중일 수 있다) — 로그아웃은 파일 → 로그인에서 한다.
            credentials.delete_credential(credentials.TOKEN_KEY)
        self._refresh()
