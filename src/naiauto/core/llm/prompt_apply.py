"""생성된 프롬프트를 현재 프롬프트 칸에 반영하는 순수 함수.

WD14의 `append_tags_to_prompt`와 같은 이유로 Qt와 무관하게 core에 둔다 —
core 테스트가 PySide6를 목으로 흉내 내지 않아도 된다.
"""

from __future__ import annotations

__all__ = ["APPLY_MODES", "apply_generated_prompt"]

#: 반영 방식. UI 라디오·설정이 이 값을 쓴다.
APPLY_MODES: tuple[str, ...] = ("append", "replace")


def apply_generated_prompt(current: str, generated: str, mode: str = "append") -> str:
    """생성된 프롬프트를 현재 프롬프트에 반영한다.

    Parameters
    ----------
    current : str
        현재 프롬프트 칸의 내용.
    generated : str
        LLM이 만든 프롬프트.
    mode : str
        ``"replace"``면 통째로 교체, ``"append"``(기본)면 쉼표로 이어 붙인다.
        알 수 없는 값은 ``"append"``로 본다.

    Returns
    -------
    str
        반영된 프롬프트.
    """
    generated = (generated or "").strip()
    if not generated:
        return current
    if mode == "replace":
        return generated
    if current.strip():
        return current.rstrip().rstrip(",") + ", " + generated
    return generated
