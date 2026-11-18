"""아티스트 조합 그룹 편집 — 조합 폴더의 JSON을 읽고 고친다 (Qt-free).

`artist_combos.ArtistComboEngine`은 폴더를 **읽기만** 한다. 지금까지 그룹을 만들려면
JSON을 텍스트 편집기로 직접 써야 했다. 이 모듈은 편집 창(`ui/artist_combo_dialog.py`)이
쓰는 읽기·쓰기를 맡는다.

파일 하나에 그룹이 여럿 들어갈 수 있고(`{"그룹": [태그, ...], ...}`), 폴더에 파일이
여럿일 수 있다. 그래서 그룹마다 **어느 파일에서 왔는지**를 기억해 두고, 고칠 때는 그
파일의 그 키만 바꾼다 — 같은 파일의 다른 그룹(엔진이 무시하는 잘못된 항목까지)은
건드리지 않는다. 새 그룹은 `DEFAULT_FILE`에 들어간다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: 엔진(`artist_combos.py`)과 같은 한도. 여기서 넘긴 것은 엔진이 조용히 버리므로,
#: 저장하기 전에 알려 주는 편이 낫다.
MAX_GROUP_NAME_LEN = 64
GROUP_NAME_RE = re.compile(rf"^[a-zA-Z0-9_]{{1,{MAX_GROUP_NAME_LEN}}}$")
MAX_TAGS_PER_GROUP = 500
MAX_TAG_LEN = 200

#: 편집 창에서 새로 만든 그룹이 들어가는 파일.
DEFAULT_FILE = "combos.json"


class ComboStoreError(Exception):
    """그룹을 저장할 수 없다. `code`는 UI가 번역 키(`artist_combo.err_<code>`)로 쓴다."""

    def __init__(self, code: str, *args: object) -> None:
        super().__init__(code, *args)
        self.code = code
        self.args_for_message = args


@dataclass
class ComboGroup:
    """그룹 하나. `file`은 조합 폴더 안의 파일 이름."""

    name: str
    tags: list[str] = field(default_factory=list)
    file: str = DEFAULT_FILE


def is_valid_name(name: str) -> bool:
    return bool(GROUP_NAME_RE.match(name))


def parse_tags(text: str) -> list[str]:
    """편집 창의 글(한 줄에 하나)을 태그 목록으로. 빈 줄과 앞뒤 공백은 버린다."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def check_tags(tags: list[str]) -> None:
    """엔진이 받아 줄 목록인지 본다. 아니면 `ComboStoreError`."""
    if not tags:
        raise ComboStoreError("no_tags")
    if len(tags) > MAX_TAGS_PER_GROUP:
        raise ComboStoreError("too_many_tags", MAX_TAGS_PER_GROUP)
    for tag in tags:
        if len(tag) > MAX_TAG_LEN:
            raise ComboStoreError("tag_too_long", MAX_TAG_LEN, tag[:40])


def _read_file(path: Path) -> dict:
    """파일의 최상위 객체. 없으면 빈 dict, 읽을 수 없으면 `ComboStoreError`."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ComboStoreError("file_unreadable", path.name) from e
    if not isinstance(data, dict):
        raise ComboStoreError("file_unreadable", path.name)
    return data


def _write_file(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise ComboStoreError("file_unwritable", path.name) from e


def load_groups(combos_dir: str | Path) -> list[ComboGroup]:
    """폴더의 모든 그룹 (파일 이름순 → 파일 안 순서). 읽을 수 없는 파일은 건너뛴다.

    문자열 목록이 아닌 항목은 그룹이 아니라고 보고 뺀다 (엔진과 같은 기준). 같은
    이름이 여러 파일에 있으면 엔진이 나중에 읽은 것을 쓰므로, 여기서도 뒤의 것이
    이긴다.
    """
    directory = Path(combos_dir)
    if not directory.is_dir():
        return []
    groups: dict[str, ComboGroup] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = _read_file(path)
        except ComboStoreError:
            continue
        for name, tags in data.items():
            if not isinstance(name, str) or not isinstance(tags, list):
                continue
            groups[name] = ComboGroup(
                name=name,
                tags=[tag for tag in tags if isinstance(tag, str) and tag],
                file=path.name,
            )
    return list(groups.values())


def save_group(
    combos_dir: str | Path,
    group: ComboGroup,
    *,
    original_name: str | None = None,
) -> None:
    """그룹을 자기 파일에 쓴다. `original_name`이 다르면 이름을 바꾼 것이다.

    이름을 바꿀 때는 파일 안의 **자리를 지킨다** — 지우고 끝에 다시 넣으면 편집 창의
    목록 순서가 저장할 때마다 바뀐다.

    Raises:
        ComboStoreError: 이름·태그가 규칙에 안 맞거나, 같은 이름의 그룹이 이미 있거나,
            파일을 읽거나 쓸 수 없을 때.
    """
    if not is_valid_name(group.name):
        raise ComboStoreError("invalid_name", MAX_GROUP_NAME_LEN)
    check_tags(group.tags)

    renamed = original_name is not None and original_name != group.name
    if original_name is None or renamed:
        if any(existing.name == group.name for existing in load_groups(combos_dir)):
            raise ComboStoreError("duplicate_name", group.name)

    path = Path(combos_dir) / (group.file or DEFAULT_FILE)
    data = _read_file(path)
    if renamed and original_name in data:
        data = {(group.name if key == original_name else key): value for key, value in data.items()}
    data[group.name] = list(group.tags)
    _write_file(path, data)


def delete_group(combos_dir: str | Path, group: ComboGroup) -> bool:
    """그룹을 파일에서 뺀다. 뺐으면 True (이미 없었으면 False).

    파일이 비어도 지우지 않는다 — 사용자가 만든 파일이고, 빈 `{}`는 엔진에 해가 없다.
    """
    path = Path(combos_dir) / (group.file or DEFAULT_FILE)
    data = _read_file(path)
    if group.name not in data:
        return False
    del data[group.name]
    _write_file(path, data)
    return True


def placeholder(name: str, *, loop: bool = False) -> str:
    """프롬프트에 넣을 자리표시자 — `{artist:name}` 또는 `{artist_loop:name}`."""
    return f"{{artist_loop:{name}}}" if loop else f"{{artist:{name}}}"


__all__ = [
    "DEFAULT_FILE",
    "MAX_GROUP_NAME_LEN",
    "MAX_TAGS_PER_GROUP",
    "MAX_TAG_LEN",
    "ComboGroup",
    "ComboStoreError",
    "check_tags",
    "delete_group",
    "is_valid_name",
    "load_groups",
    "parse_tags",
    "placeholder",
    "save_group",
]
