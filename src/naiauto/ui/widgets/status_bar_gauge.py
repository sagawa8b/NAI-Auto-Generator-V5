"""상태바 크레딧 게이지 — 퍼센트 + 남은 생성 장수 표시.

CreditEstimator가 현재 해상도·스텝 조합의 장당 소모량을 알고 있으면
"X% (~N images)"로 표시하고, 모르면 "X%"만 표시한다.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

from ...core.credit_estimator import CreditEstimator


class StatusBarGauge(QLabel):
    """V5 크레딧 잔량을 "X% (~N images)" 형태로 보여주는 상태바 위젯."""

    def __init__(self, estimator: CreditEstimator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._estimator = estimator
        self._current_percent: int | None = None

    def update_display(
        self,
        current_percent: int,
        width: int,
        height: int,
        steps: int,
    ) -> None:
        """게이지 텍스트를 갱신한다.

        Parameters
        ----------
        current_percent:
            현재 V5 크레딧 잔량 퍼센트 (0–100).
        width, height:
            현재 선택된 해상도.
        steps:
            현재 설정된 스텝 수.
        """
        self._current_percent = current_percent
        remaining = self._estimator.estimate_remaining(current_percent, (width, height), steps)
        if remaining is not None:
            self.setText(f"{current_percent}% (~{remaining} images)")
        else:
            self.setText(f"{current_percent}%")

    def clear_display(self) -> None:
        """크레딧 정보가 없을 때 위젯을 비운다."""
        self._current_percent = None
        self.setText("")
        self.setVisible(False)
