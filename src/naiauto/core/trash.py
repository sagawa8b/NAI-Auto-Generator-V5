"""파일을 지울 때 **휴지통으로** 보낸다.

`Path.unlink()`은 되돌릴 수 없다. 사용자가 실수로 지운 그림은 앱 안의 `되돌리기`로
기록은 되살려도 파일은 못 살린다 — OS의 휴지통이 마지막 안전망이다.

플랫폼별로 하는 일:

| OS | 방법 |
|---|---|
| Windows | `SHFileOperationW` + `FOF_ALLOWUNDO` — 진짜 휴지통 |
| macOS | `~/.Trash`로 이동 |
| Linux | freedesktop 휴지통 규격 (`$XDG_DATA_HOME/Trash`의 `files/` + `info/`) |

**휴지통에 못 넣으면 그냥 지운다.** 네트워크 드라이브·이동식 매체·권한 문제로 휴지통이
없을 수 있는데, 그때 파일이 남아 버리면 앱은 지웠다고 여기고 사용자는 지워지지 않은
파일을 보게 된다. 어느 쪽으로 지웠는지는 반환값으로 알린다.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: `send_to_trash` 결과.
TRASHED = "trashed"  # 휴지통으로 갔다
DELETED = "deleted"  # 휴지통을 못 써서 바로 지웠다
MISSING = "missing"  # 애초에 없었다


def send_to_trash(path: Path) -> str:
    """파일 하나를 휴지통으로 보낸다.

    Returns:
        `TRASHED` · `DELETED` · `MISSING`.

    Raises:
        OSError: 휴지통도 삭제도 실패했을 때.
    """
    if not path.exists():
        return MISSING
    try:
        if _to_trash(path.resolve()):
            return TRASHED
    except OSError as e:
        # 휴지통 실패는 삭제 실패가 아니다 — 아래에서 그냥 지운다.
        logger.warning("cannot move %s to the trash: %s", path, e)
    path.unlink(missing_ok=True)
    return DELETED


# ── 플랫폼별 ────────────────────────────────────────────────────────────


def _to_trash(path: Path) -> bool:
    """휴지통으로 옮겼으면 True. 이 플랫폼에서 못 하면 False."""
    if sys.platform == "win32":
        return _windows_recycle(path)
    if sys.platform == "darwin":
        return _move_into(path, Path.home() / ".Trash", write_info=False)
    return _freedesktop_trash(path)


def _windows_recycle(path: Path) -> bool:
    """`SHFileOperationW`로 휴지통에 넣는다.

    `FOF_ALLOWUNDO`가 "휴지통으로"에 해당한다. 없으면 그냥 삭제가 된다.
    확인 대화와 진행 표시줄은 끈다 — 앱이 이미 물어보고 지우는 길이다.
    """
    import ctypes
    from ctypes import wintypes

    fo_delete = 0x0003
    fof_silent = 0x0004
    fof_noconfirmation = 0x0010
    fof_allowundo = 0x0040
    fof_noerrorui = 0x0400

    class _ShFileOpStructW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),  # FILEOP_FLAGS = WORD
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    # pFrom은 **널 두 개로** 끝나야 한다 (여러 개를 넘길 수 있는 자리라서).
    # 파이썬 문자열을 그대로 넘기면 첫 널에서 잘리므로 버퍼로 만들어 넘긴다.
    source = ctypes.create_unicode_buffer(f"{path}\0")

    operation = _ShFileOpStructW()
    operation.hwnd = None
    operation.wFunc = fo_delete
    operation.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    operation.pTo = None
    operation.fFlags = fof_allowundo | fof_noconfirmation | fof_noerrorui | fof_silent

    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if code != 0:
        logger.warning("SHFileOperationW failed for %s (code %s)", path, code)
        return False
    return not operation.fAnyOperationsAborted


def _freedesktop_trash(path: Path) -> bool:
    """freedesktop.org 휴지통 규격 — 파일은 `files/`, 원래 경로는 `info/`."""
    home_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return _move_into(path, Path(home_data) / "Trash", write_info=True)


def _move_into(path: Path, trash_dir: Path, *, write_info: bool) -> bool:
    """휴지통 폴더로 옮긴다. 같은 이름이 있으면 뒤에 번호를 붙인다."""
    files_dir = trash_dir / "files" if write_info else trash_dir
    files_dir.mkdir(parents=True, exist_ok=True)
    target = _free_name(files_dir, path.name)

    if write_info:
        # 정보 파일을 **먼저** 쓴다. 순서가 반대면 파일만 들어가고 정보가 없는
        # 항목이 생겨, 파일 관리자가 "복원"을 못 한다.
        info_dir = trash_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        (info_dir / f"{target.name}.trashinfo").write_text(
            "[Trash Info]\n"
            f"Path={urllib.parse.quote(str(path))}\n"
            f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n",
            encoding="utf-8",
        )

    shutil.move(str(path), str(target))
    return True


def _free_name(directory: Path, name: str) -> Path:
    """`directory` 안에서 아직 안 쓰는 이름. 겹치면 `이름.2.png`처럼 번호를 넣는다."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(2, 10000):
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError(f"no free name for {name} in {directory}")


__all__ = ["DELETED", "MISSING", "TRASHED", "send_to_trash"]
