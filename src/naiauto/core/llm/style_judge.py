"""그림체 유사도 판정 — VLM에게 "이 후보가 참조 그림체와 얼마나 닮았는가"를 묻는다.

아레나는 V4에서 쓰던 그림체(작가 조합)를 V5에서 재현하려고 무작위 조합을 잔뜩 뽑아
사람이 이상형 월드컵으로 고른다. 경우의 수가 너무 많아, 그 1차 선별을 로컬 VLM에게
맡긴다: **V4 참조 이미지 여러 장**을 "목표 그림체"로 제시하고, 후보를 **한 장씩** 보여
0~100 점수를 매기게 한다.

왜 후보를 한 장씩(페어가 아니라)?
    한 요청에 후보를 여러 장 넣고 상대 비교시키면 위치(먼저/나중)에 따라 점수가
    흔들린다(순서 편향). 참조 그룹은 고정하고 후보만 1장씩 넣어 **절대 점수**를 매기면,
    청크를 어떻게 나누든 후보끼리 같은 잣대로 비교할 수 있어 대량 랭킹에 적합하다.

이 모듈은 Qt·SDK와 무관한 순수 로직이다 — 메시지 문안 조립과 출력 파싱만 한다.
실제 서버 호출은 `lmstudio_client.LMStudioPromptGenerator.score_style()`가, 후보 수집과
순차 판정은 `services/style_judge_service.py`가 맡는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parsing import _try_parse_json  # 방어적 JSON 파싱 재사용 (같은 패키지)

__all__ = [
    "DEFAULT_JUDGE_SYSTEM_PROMPT",
    "MAX_SCORE",
    "MIN_SCORE",
    "REASON_MAX_CHARS",
    "StyleJudgeConfig",
    "StyleScore",
    "judge_user_message",
    "parse_style_score",
]

#: 점수 범위. 0 = 전혀 다른 그림체, 100 = 참조와 사실상 같은 그림체.
MIN_SCORE = 0
MAX_SCORE = 100

#: 근거 텍스트를 이 길이에서 자른다 — 표에 한 줄로 보여 줄 것이라, 모델이 장광설을
#: 늘어놓아도 UI가 감당할 수 있게 코어에서 미리 다듬는다.
REASON_MAX_CHARS = 400

#: 판정 시스템 프롬프트. 프롬프트 생성(natural/danbooru)과 목적이 전혀 달라 별도로 둔다.
#: 핵심 지시:
#: - **그림체(스타일)만** 본다 — 인물·구도·배경 같은 내용이 같은지는 무시한다.
#: - 앞선 이미지들이 "참조(목표)", 마지막 한 장이 "후보"임을 못박는다.
#: - 오직 JSON 한 덩어리로만 답한다 (설명·코드펜스 금지) — 파서가 있지만 흔들림을 줄인다.
#: - **점수대별 기준(rubric)과 "엄격하게, 넓게 분포시켜라"는 지시**로 점수 포화를 막는다.
#:   판별력이 약한 모델은 그냥 두면 대부분을 90~100에 몰아 찍어 순위가 무의미해진다
#:   (사용자 제보: LV 모델이 141개 후보를 거의 95점으로 채점). rubric으로 90+는 사실상
#:   같은 작가일 때만 주게 하고, 대부분의 '그럴듯한' 후보는 중간대로 내려 변별을 살린다.
DEFAULT_JUDGE_SYSTEM_PROMPT = (
    "You are a STRICT expert art-style comparator for anime/illustration images. "
    "The first images are REFERENCE images that define a target art style. "
    "The LAST image is a CANDIDATE. Judge ONLY how closely the candidate's "
    "ART STYLE matches the reference style: line work, shading, color palette, "
    "rendering, proportions, and overall aesthetic. IGNORE subject matter, pose, "
    "composition, character identity, and background content. "
    "Use the FULL 0-100 range and spread scores widely; do NOT cluster near the top. "
    "Most plausible candidates should land in the middle; reserve high scores for "
    "genuinely close matches. Scoring rubric: "
    "90-100 = indistinguishable, looks drawn by the exact same artist(s); "
    "70-89 = clearly the same family of style but with visible differences; "
    "50-69 = related or partially similar style; "
    "30-49 = loosely similar, different overall look; "
    "0-29 = clearly a different style. "
    "Be critical: if you hesitate between two bands, choose the LOWER one. "
    "Respond with ONE JSON object only, no prose, no code fences: "
    '{"score": <integer 0-100>, "reason": "<one short sentence naming the specific '
    'style traits that matched or differed>"}.'
)

#: 사용자 메시지 틀 — 이미지는 SDK가 별도로 붙이므로, 여기서는 텍스트 지시만 만든다.
#: `{n}`은 참조 장수. 후보가 항상 마지막 한 장임을 다시 알려 준다.
_USER_MESSAGE_TEMPLATE = (
    "The first {n} image(s) are the reference art style. "
    "The final image is the candidate. "
    "Score how closely the candidate matches the reference art style "
    "(0-100) and give one short reason. Reply with the JSON object only."
)

#: 점수로 인정하는 JSON 키 (모델마다 이름이 조금씩 다르다).
_SCORE_KEYS = ("score", "similarity", "match", "rating")
#: 근거로 인정하는 JSON 키.
_REASON_KEYS = ("reason", "reasoning", "explanation", "comment", "why")

#: JSON이 아예 안 나왔을 때 본문에서 숫자를 건지는 최후의 수단.
_NUMBER_RE = re.compile(r"\b(100|\d{1,2})\b")


@dataclass(frozen=True)
class StyleJudgeConfig:
    """판정 1회에 필요한 설정. UI가 `AppSettings.arena`/`lmstudio`에서 만들어 넘긴다.

    frozen인 이유는 워커 스레드가 이 값을 읽는 동안 UI가 바꾸면 안 되기 때문이다
    (프로젝트의 불변 요청 흐름 원칙과 같다).
    """

    host: str = "localhost:1234"
    model: str = ""  # "" = 로드된 첫 모델 자동 사용
    timeout: float = 120.0  # 0 이하 = 무제한
    #: 참조 이미지를 이 장수까지만 쓴다. 컨텍스트(~20k) 안에서 안전한 상한.
    #: 초과분은 서비스가 잘라내고 UI가 안내한다.
    max_reference_images: int = 5
    #: 시스템 프롬프트 override. 빈 문자열이면 `DEFAULT_JUDGE_SYSTEM_PROMPT`.
    system_prompt: str = ""

    def effective_system_prompt(self) -> str:
        prompt = (self.system_prompt or "").strip()
        return prompt or DEFAULT_JUDGE_SYSTEM_PROMPT


@dataclass(frozen=True)
class StyleScore:
    """후보 한 장에 대한 판정 결과.

    `raw`는 파싱 전 모델 원문 — UI가 파싱 실패 시 폴백 표시에 쓸 수 있다.
    `ok`가 False면 점수를 신뢰할 수 없다는 뜻이다 (숫자를 못 건졌다).
    """

    score: int = MIN_SCORE
    reason: str = ""
    raw: str = ""
    ok: bool = False


def judge_user_message(reference_count: int) -> str:
    """참조 장수를 넣은 사용자 메시지 텍스트."""
    return _USER_MESSAGE_TEMPLATE.format(n=max(1, reference_count))


def _clamp_score(value: object) -> int | None:
    """숫자로 바꿔 0~100으로 자른다. 숫자가 아니면 None."""
    if isinstance(value, bool):  # bool은 int의 하위형이지만 점수로 보지 않는다
        return None
    try:
        number = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(MIN_SCORE, min(MAX_SCORE, number))


def _first_value(data: dict, keys: tuple[str, ...]) -> object | None:
    """dict에서 주어진 키(대소문자 무시) 중 처음 나오는 값."""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def parse_style_score(raw: str) -> StyleScore:
    """모델 출력에서 점수/근거를 뽑는다. 어떤 입력이든 예외를 내지 않는다.

    순서 (parsing.py의 방어적 파싱과 같은 정신):
    1. 코드펜스 안 JSON → 전체 JSON → 문자열 안 첫 `{...}` 순으로 파싱.
    2. dict면 알려진 키(대소문자 무시)로 점수/근거를 찾는다.
    3. JSON이 없으면 본문에서 첫 0~100 정수를 점수로 건지고, 원문 앞부분을 근거로 둔다.
    4. 무엇도 못 건지면 `ok=False`로 돌려준다 (서비스가 실패로 집계).
    """
    text = (raw or "").strip()
    if not text:
        return StyleScore(raw=raw or "")

    data = _try_parse_json(text)
    if isinstance(data, dict):
        score = _clamp_score(_first_value(data, _SCORE_KEYS))
        reason = _first_value(data, _REASON_KEYS)
        reason_text = _clean_reason(reason if isinstance(reason, str) else "")
        if score is not None:
            return StyleScore(score=score, reason=reason_text, raw=raw, ok=True)

    # JSON을 못 읽었거나 점수 키가 없다 — 본문에서 숫자를 건진다.
    match = _NUMBER_RE.search(text)
    if match is not None:
        score = _clamp_score(match.group(1))
        if score is not None:
            return StyleScore(score=score, reason=_clean_reason(text), raw=raw, ok=True)

    return StyleScore(raw=raw)


def _clean_reason(text: str) -> str:
    """근거 텍스트를 한 줄로 다듬고 길이를 제한한다."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) > REASON_MAX_CHARS:
        return collapsed[: REASON_MAX_CHARS - 1].rstrip() + "\u2026"
    return collapsed
