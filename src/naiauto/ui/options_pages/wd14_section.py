"""WD14 태거 모델 구역 — 모델 폴더 + 모델 고르기 + 내려받기.

`폴더 경로` 화면(`folders_page.py`)이 경로 표 아래에 끼워 쓰는 위젯이다. 페이지가 아니라
**구역**인 이유: 여기서 다루는 것도 결국 "어느 폴더의 어느 파일을 쓰는가"라서, 결과·와일드카드
같은 다른 경로들과 한 화면에 있는 편이 찾기 쉽다 (v0.7.8에서 `태그` 화면에서 옮겨 왔다).

`OptionsPage`와 같은 모양의 `load` / `commit` / `retranslate`를 갖지만 등록되지는 않는다 —
페이지가 자기 것을 호출하면서 이 구역의 것도 함께 부른다.

모델 내려받기는 수백 MB를 받으므로 스레드에서 진행률과 함께 돌린다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings, default_wd14_dir
from ...core.wd14_tagger import (
    KNOWN_MODELS,
    WD14DownloadCancelled,
    WD14Error,
    download_model,
    installed_models,
    runtime_error,
)

logger = logging.getLogger(__name__)

__all__ = ["ModelDownloadWorker", "WD14ModelSection"]


class ModelDownloadWorker(QThread):
    """WD14 모델 한 벌을 내려받는 스레드.

    UI 스레드에서 받으면 수백 MB 동안 창이 얼어붙는다. 취소는 플래그로 알리고
    (`cancel()`), 받던 파일은 `core.wd14_tagger`가 지운다.
    """

    progress = Signal(int, int)  # (받은 바이트, 전체 바이트 — 모르면 0)
    finished_ok = Signal(str)  # 모델 이름
    failed = Signal(str)  # 오류 문구
    cancelled = Signal()

    def __init__(self, name: str, directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._directory = directory
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            download_model(
                self._name,
                self._directory,
                on_progress=lambda received, total: self.progress.emit(received, total),
                should_cancel=lambda: self._cancel,
            )
        except WD14DownloadCancelled:
            self.cancelled.emit()
        except WD14Error as e:
            self.failed.emit(str(e))
        except Exception as e:  # 예상 못 한 오류로 옵션 창이 죽지 않도록
            logger.exception("WD14 model download failed")
            self.failed.emit(str(e))
        else:
            self.finished_ok.emit(self._name)


class WD14ModelSection(QWidget):
    """모델 폴더 · 태거 모델 · 내려받기 한 묶음 (V4의 `단부루 태거 모델`과 같은 구성)."""

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        #: 진행 중인 모델 내려받기 (없으면 None).
        self._download_worker: ModelDownloadWorker | None = None
        self._progress_dialog: QProgressDialog | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.wd14_title = QLabel(self)
        self.wd14_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.wd14_title)

        form = QFormLayout()

        self.wd14_dir_label = QLabel(self)
        dir_row = QHBoxLayout()
        self.wd14_dir_edit = QLineEdit(self)
        self.wd14_dir_edit.setPlaceholderText(str(default_wd14_dir()))
        self.wd14_browse_button = QPushButton(self)
        self.wd14_browse_button.clicked.connect(self._browse_wd14_dir)
        dir_row.addWidget(self.wd14_dir_edit, 1)
        dir_row.addWidget(self.wd14_browse_button)
        form.addRow(self.wd14_dir_label, dir_row)

        self.wd14_model_label = QLabel(self)
        model_row = QHBoxLayout()
        self.wd14_model_combo = QComboBox(self)
        self.wd14_download_button = QPushButton(self)
        self.wd14_download_button.clicked.connect(self._download_model)
        model_row.addWidget(self.wd14_model_combo, 1)
        model_row.addWidget(self.wd14_download_button)
        form.addRow(self.wd14_model_label, model_row)
        layout.addLayout(form)

        self.wd14_status_label = QLabel(self)
        self.wd14_status_label.setWordWrap(True)
        layout.addWidget(self.wd14_status_label)

        # 모델 폴더가 바뀌면 그 폴더에 있는 모델로 목록을 다시 채운다 (V4와 같다).
        self.wd14_dir_edit.textChanged.connect(self._refresh_models)
        self.wd14_model_combo.currentIndexChanged.connect(self._refresh_wd14_status)

        self.retranslate()

    # ── 페이지가 부르는 것 ──────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self.wd14_dir_edit.setText(draft.wd14_dir)
        self._refresh_models(selected=draft.wd14_model)

    def commit(self, draft: AppSettings) -> None:
        """공백만 남은 폴더 입력은 기본 폴더로 정규화한다."""
        draft.wd14_dir = self.wd14_dir_edit.text().strip() or str(default_wd14_dir())
        draft.wd14_model = self.wd14_model_combo.currentData() or ""

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.wd14_title.setText(tr("options.wd14_title"))
        self.wd14_dir_label.setText(tr("options.folder_wd14_dir"))
        self.wd14_model_label.setText(tr("options.wd14_model"))
        self.wd14_browse_button.setText(tr("options.browse"))
        self.wd14_download_button.setText(tr("options.wd14_download"))
        self._relabel_models()
        self._refresh_wd14_status()

    # ── 내부 ────────────────────────────────────────────────────────────

    def _wd14_dir(self) -> Path:
        """입력란이 가리키는 폴더 (비어 있으면 기본 폴더)."""
        text = self.wd14_dir_edit.text().strip()
        return Path(text) if text else default_wd14_dir()

    def _browse_wd14_dir(self) -> None:
        tr = self._i18n.get_text
        chosen = QFileDialog.getExistingDirectory(
            self,
            tr("options.choose_folder", tr("options.folder_wd14_dir")),
            str(self._wd14_dir()),
        )
        if chosen:
            self.wd14_dir_edit.setText(str(Path(chosen).resolve()))

    def _refresh_models(self, selected: str | None = None) -> None:
        """폴더에 있는 모델 + 내려받을 수 있는 모델로 콤보를 다시 채운다.

        항목의 `data`는 모델 이름 그대로이고, 보이는 글자는 설치 여부에 따라
        `retranslate`(정확히는 `_relabel_models`)가 붙인다. 이름이 목록에 없는
        모델을 직접 넣어 둔 폴더도 그대로 고를 수 있다 (V4와 같다).
        """
        if selected is None:
            selected = self.wd14_model_combo.currentData() or ""
        installed = installed_models(self._wd14_dir())
        names = list(KNOWN_MODELS) + [name for name in installed if name not in KNOWN_MODELS]

        blocked = self.wd14_model_combo.blockSignals(True)
        self.wd14_model_combo.clear()
        for name in names:
            self.wd14_model_combo.addItem(name, name)
        self.wd14_model_combo.blockSignals(blocked)

        index = self.wd14_model_combo.findData(selected) if selected else -1
        if index < 0:
            # 고른 모델이 없으면 설치된 것 중 첫 번째를 고른다 — 바로 쓸 수 있는 쪽으로.
            first_installed = next((name for name in names if name in installed), None)
            index = self.wd14_model_combo.findData(first_installed) if first_installed else 0
        self.wd14_model_combo.setCurrentIndex(max(0, index))
        self._relabel_models()
        self._refresh_wd14_status()

    def _relabel_models(self) -> None:
        """설치된 모델에 `(설치됨)` 표시를 붙인다."""
        tr = self._i18n.get_text
        installed = set(installed_models(self._wd14_dir()))
        for index in range(self.wd14_model_combo.count()):
            name = self.wd14_model_combo.itemData(index)
            label = tr("options.wd14_model_installed", name) if name in installed else name
            self.wd14_model_combo.setItemText(index, label)

    def _refresh_wd14_status(self) -> None:
        """고른 모델이 설치돼 있는지 알려 주고, 내려받기 버튼을 열고 닫는다.

        onnxruntime을 쓸 수 없으면 모델이 있어도 태깅은 안 된다 — 모델을 다 받아 놓고
        도구 창에서야 그 사실을 아는 일이 없도록 여기서 먼저 알린다.
        """
        tr = self._i18n.get_text
        name = self.wd14_model_combo.currentData() or ""
        installed = name in set(installed_models(self._wd14_dir()))
        busy = self._download_worker is not None
        failure = runtime_error()
        if failure:
            self.wd14_status_label.setText(tr("options.wd14_runtime_missing", failure))
        elif installed:
            self.wd14_status_label.setText(tr("options.wd14_installed", name))
        else:
            self.wd14_status_label.setText(tr("options.wd14_not_installed"))
        self.wd14_download_button.setEnabled(
            bool(name) and not installed and not busy and name in KNOWN_MODELS
        )

    def _download_model(self) -> None:
        """고른 모델을 내려받는다 (수백 MB — 스레드 + 진행률 창)."""
        tr = self._i18n.get_text
        name = self.wd14_model_combo.currentData() or ""
        if not name or self._download_worker is not None:
            return

        directory = self._wd14_dir()
        self._progress_dialog = QProgressDialog(
            tr("options.wd14_downloading", name), tr("options.cancel"), 0, 0, self
        )
        self._progress_dialog.setWindowTitle(tr("options.wd14_download"))
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setAutoClose(False)
        self._progress_dialog.setAutoReset(False)

        worker = ModelDownloadWorker(name, directory, self)
        self._download_worker = worker
        worker.progress.connect(self._on_download_progress)
        worker.finished_ok.connect(self._on_download_done)
        worker.failed.connect(self._on_download_failed)
        worker.cancelled.connect(self._on_download_cancelled)
        self._progress_dialog.canceled.connect(worker.cancel)
        self._refresh_wd14_status()
        worker.start()
        self._progress_dialog.show()

    def _on_download_progress(self, received: int, total: int) -> None:
        if self._progress_dialog is None:
            return
        if total > 0:
            self._progress_dialog.setMaximum(total // 1024)
            self._progress_dialog.setValue(received // 1024)
        self._progress_dialog.setLabelText(
            self._i18n.get_text(
                "options.wd14_download_progress",
                self.wd14_model_combo.currentData() or "",
                received // (1 << 20),
                total // (1 << 20),
            )
        )

    def _finish_download(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog = None
        self._download_worker = None
        self._refresh_models()

    def _on_download_done(self, name: str) -> None:
        self._finish_download()
        tr = self._i18n.get_text
        index = self.wd14_model_combo.findData(name)
        if index >= 0:
            self.wd14_model_combo.setCurrentIndex(index)
        QMessageBox.information(self, tr("options.wd14_download"), tr("options.wd14_download_done", name))

    def _on_download_failed(self, message: str) -> None:
        self._finish_download()
        tr = self._i18n.get_text
        QMessageBox.warning(self, tr("errors.title"), f"{tr('options.wd14_download_failed')}\n\n{message}")

    def _on_download_cancelled(self) -> None:
        self._finish_download()
