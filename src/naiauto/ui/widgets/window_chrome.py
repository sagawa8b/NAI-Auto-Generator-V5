"""창 테두리 단추와 화면 밖으로 나간 창 되찾기.

`QDialog`은 제목 표시줄에 **닫기 단추만** 둔다. 잠깐 떴다 사라지는 대화 상자라면
그 편이 맞지만, 그림체 아레나·갤러리처럼 **오래 띄워 놓고 크게 보는 창**은 최소화도
최대화도 못 해 다루기 힘들다.

    enable_window_controls(self)                   # 모드리스 창
    enable_window_controls(self, minimize=False)   # `exec()`으로 띄우는 모달

모달에서 최소화를 빼는 이유: 부모 창이 입력을 못 받는 채로 자식만 내려가면
사용자가 "창이 사라졌는데 아무것도 안 눌린다"는 상태에 갇힌다.

`enable_window_controls`는 **`show()` 전에** 불러야 한다 — 플랫폼에 따라
`setWindowFlags()`가 이미 떠 있는 창을 숨기기 때문이다.

## 전체 화면(F11)을 넣지 않는 이유

한때 `F11` 전체 화면 토글이 있었다. Windows에서 부모가 있는 `QDialog`을
`showFullScreen()`하면 **창틀 높이만큼 내용이 위로 밀려**, 탭 줄과 상단 컨트롤이
화면 위로 잘려 나갔다 (v0.8.0 개발 중 제보). 최대화 단추만으로도 "크게 보기"는
되므로 토글을 걷어냈다. 되살리려면 Windows 실물에서 확인한 뒤에 하는 게 맞다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


def enable_window_controls(window: QWidget, *, minimize: bool = True) -> None:
    """제목 표시줄에 최소화·최대화 단추를 붙인다.

    최대화 단추가 붙으면 제목 표시줄 **더블클릭**과 Windows의 스냅
    (`Win`+`←`/`→`, 화면 가장자리로 끌기)도 함께 동작한다 — 최대화 힌트가 없는
    창에서는 OS가 이 동작들을 막는다.
    """
    flags = window.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint
    if minimize:
        flags |= Qt.WindowType.WindowMinimizeButtonHint
    window.setWindowFlags(flags)


def ensure_on_screen(window: QWidget) -> bool:
    """창이 화면 밖으로 벗어났으면 안으로 끌어온다.

    저장해 둔 위치를 되살릴 때 쓴다. 제목 표시줄이 화면 위로 넘어가면 창을 끌어
    옮길 수도, 닫을 수도 없다 — 모니터 구성이 바뀌었거나, 저장된 값이 어긋났거나,
    창이 화면보다 커진 경우에 그렇게 된다.

    최대화·전체 화면 상태는 건드리지 않는다 (그 좌표는 OS가 관리한다).

    Returns:
        실제로 옮기거나 줄였으면 True.
    """
    if window.isMaximized() or window.isFullScreen():
        return False
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is None:  # 화면 정보가 없는 환경 — 손댈 근거가 없다
        return False

    area = screen.availableGeometry()
    frame = window.frameGeometry()
    # 창틀(제목 표시줄·테두리) 두께. 화면에 붙기 전에는 0으로 나온다.
    extra_w = frame.width() - window.width()
    extra_h = frame.height() - window.height()

    width = min(frame.width(), area.width())
    height = min(frame.height(), area.height())
    left = min(max(frame.left(), area.left()), area.right() - width + 1)
    top = min(max(frame.top(), area.top()), area.bottom() - height + 1)
    if (left, top, width, height) == (frame.left(), frame.top(), frame.width(), frame.height()):
        return False

    if (width, height) != (frame.width(), frame.height()):
        window.resize(width - extra_w, height - extra_h)
    # `move()`는 창틀 기준이다 — 프레임 좌표를 그대로 넘긴다.
    window.move(left, top)
    return True


__all__ = ["enable_window_controls", "ensure_on_screen"]
