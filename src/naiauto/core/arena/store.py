"""아레나 데이터 입출력 — `arena.json`과 `images/` (Qt-free).

폴더 모양:

    <아레나 폴더>/
        arena.json          작가 명단 + 조합 + 점수
        images/<조합id>_<시드>.png

조합을 지우면 그 그림도 치운다. 원본 프로그램은 레코드만 지워서 폴더에 쓰지 않는
이미지가 계속 쌓였다 (사용자 제보). 반대로 어떤 조합도 가리키지 않는 그림
(`orphan_images`)도 찾아서 치울 수 있다.

그림은 **휴지통으로** 보낸다 (`core/trash.py`) — 크레딧을 써서 뽑은 것이라,
실수로 지웠을 때 OS 휴지통에서 되찾을 수 있어야 한다.

저장은 임시 파일에 쓴 뒤 원자적으로 갈아 끼운다. 생성 도중 앱이 죽어도 이전
`arena.json`이 반쯤 덮인 채로 남지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..trash import send_to_trash
from .models import ArenaState, Combo

logger = logging.getLogger(__name__)

DATA_FILENAME = "arena.json"
IMAGES_DIRNAME = "images"

#: 아레나가 쓰는 이미지 확장자. 저장 형식 설정(png/webp)을 따라간다.
_IMAGE_SUFFIXES = (".png", ".webp")


class ArenaStore:
    """한 아레나 폴더에 대한 읽기/쓰기. 상태를 들고 있지 않다 (경로만)."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    @property
    def data_path(self) -> Path:
        return self.base_dir / DATA_FILENAME

    @property
    def images_dir(self) -> Path:
        return self.base_dir / IMAGES_DIRNAME

    def ensure_dirs(self) -> None:
        """폴더를 만들어 둔다. 못 만들어도 앱은 계속 떠야 하므로 로그만 남긴다."""
        for path in (self.base_dir, self.images_dir):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning("cannot create arena folder %s: %s", path, e)

    # ---------------------------------------------------------------- 읽기

    def load(self) -> ArenaState:
        """`arena.json`을 읽는다. 없으면 빈 상태, 깨졌으면 백업하고 빈 상태.

        설정 저장소와 같은 정책이다 — 파손된 파일 때문에 앱이 뜨지 못하면 안 되고,
        원본은 사용자가 살펴볼 수 있게 남긴다.
        """
        path = self.data_path
        if not path.exists():
            return ArenaState()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("arena data broken (%s), starting empty: %s", path, e)
            try:
                path.replace(path.with_suffix(".json.broken"))
            except OSError:
                pass
            return ArenaState()
        return ArenaState.from_dict(data)

    # ---------------------------------------------------------------- 쓰기

    def save(self, state: ArenaState) -> bool:
        """상태를 원자적으로 저장한다. 성공 여부를 돌려준다.

        같은 폴더의 임시 파일에 쓰고 `os.replace`로 갈아 끼운다 (다른 폴더에 쓰면
        파일시스템이 다를 때 원자성이 깨진다).
        """
        self.ensure_dirs()
        target = self.data_path
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, target)
            return True
        except OSError as e:
            logger.error("cannot save arena data to %s: %s", target, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    # ------------------------------------------------------------ 이미지

    def image_name(self, combo: Combo, suffix: str = ".png") -> str:
        """이 조합의 이미지 파일명. 조합 id가 들어가므로 절대 겹치지 않는다."""
        return f"{combo.id}_{combo.seed}{suffix}"

    def image_path(self, combo: Combo) -> Path | None:
        """조합의 그림 경로. 그림이 없거나 파일이 사라졌으면 None.

        `Combo.image_path`는 파일명만 담는다 — 아레나 폴더를 통째로 옮겨도
        그림을 계속 찾을 수 있어야 하기 때문이다.
        """
        if not combo.image_path:
            return None
        path = self.images_dir / combo.image_path
        return path if path.exists() else None

    def delete_images(self, combos: list[Combo]) -> int:
        """조합들의 그림을 **휴지통으로** 보낸다. 처리한 개수를 돌려준다.

        바로 지우지 않는 이유: 앱의 `되돌리기`는 조합 기록만 되살릴 수 있고,
        파일까지 되살리려면 OS의 휴지통이 필요하다.
        """
        removed = 0
        for combo in combos:
            if not combo.image_path:
                continue
            try:
                send_to_trash(self.images_dir / combo.image_path)
                removed += 1
            except OSError as e:
                logger.warning("cannot delete arena image %s: %s", combo.image_path, e)
        return removed

    def orphan_images(self, state: ArenaState) -> list[Path]:
        """어떤 조합도 가리키지 않는 그림 파일들.

        중간에 앱이 죽거나 사용자가 `arena.json`을 손댔을 때 생긴다.
        """
        if not self.images_dir.is_dir():
            return []
        referenced = {combo.image_path for combo in state.combos if combo.image_path}
        try:
            entries = sorted(self.images_dir.iterdir())
        except OSError as e:
            logger.warning("cannot list arena images %s: %s", self.images_dir, e)
            return []
        return [
            path
            for path in entries
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES and path.name not in referenced
        ]

    def cleanup_orphans(self, state: ArenaState) -> int:
        """고아 그림을 휴지통으로 보내고 처리한 개수를 돌려준다."""
        removed = 0
        for path in self.orphan_images(state):
            try:
                send_to_trash(path)
                removed += 1
            except OSError as e:
                logger.warning("cannot delete orphan image %s: %s", path, e)
        return removed

    def prune_missing_images(self, state: ArenaState) -> int:
        """파일이 사라진 조합의 `image_path`를 비운다. 비운 개수를 돌려준다.

        사용자가 탐색기에서 그림만 지웠을 때, 조합이 "그림 있음"인 채로 월드컵에
        올라와 빈 칸이 뜨는 것을 막는다.
        """
        cleared = 0
        for combo in state.combos:
            if combo.image_path and not (self.images_dir / combo.image_path).exists():
                combo.image_path = ""
                cleared += 1
        return cleared
