"""LM Studio 연결·모델 목록·프롬프트 생성 — Qt-free 코어.

로컬에서 도는 LM Studio 서버에 공식 파이썬 SDK(`lmstudio`)로 붙어, 사용자가 넣은
단어/문장/이미지를 NovelAI용 프롬프트로 바꿔 준다.

WD14 태거(`core/wd14_tagger.py`)와 같은 규약을 따른다:

- `lmstudio` import는 **함수 안에서** 한다 — 이 모듈을 읽는 것만으로 무거운 의존성이나
  네트워크 스택을 끌어오지 않는다 (옵션 페이지는 설정만 읽으려고 이 모듈을 볼 수 있다).
- `runtime_error()`는 의존성을 쓸 수 있으면 `""`, 아니면 그 이유를 돌려준다.
- 실패는 전용 예외 계층(`LMStudioError`)으로 전달한다. NovelAI API의 `NAIError`와는
  무관하다 (여기는 LM Studio다).

연결은 SDK의 **동기 스코프 리소스 API**(`with lms.Client(host) as client`)를 쓴다.
편의 API(`lms.llm(...)`)의 "첫 호출 전에 호스트를 지정" 순서 제약과 전역 상태를 피할 수
있고, 웹소켓 연결이 매 생성마다 결정적으로 정리된다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .parsing import PromptResult, parse_prompt_result

logger = logging.getLogger(__name__)

#: 취소 여부를 묻는 콜백. True면 진행 중인 생성을 중단한다.
CancelCheck = Callable[[], bool]

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TIMEOUT",
    "PROMPT_STYLES",
    "STYLE_DANBOORU",
    "STYLE_NATURAL",
    "SYSTEM_PROMPT_DANBOORU",
    "SYSTEM_PROMPT_NATURAL",
    "CancelCheck",
    "LMStudioCancelled",
    "LMStudioConfig",
    "LMStudioConnectionError",
    "LMStudioError",
    "LMStudioNoModelError",
    "LMStudioNotInstalled",
    "LMStudioPromptGenerator",
    "LMStudioResponseError",
    "LMStudioTimeoutError",
    "LMStudioVisionUnsupported",
    "PromptResult",
    "runtime_error",
    "system_prompt_for_style",
]

#: LM Studio 앱의 기본 서버 주소 (host:port).
DEFAULT_HOST = "localhost:1234"

#: 동기 응답 타임아웃 기본값 (초). 큰 모델·긴 출력은 SDK 기본 60초를 넘길 수 있다.
DEFAULT_TIMEOUT = 120.0

#: 이미지로 넘길 수 있는 형식 (LM Studio 서버가 받는 것과 일치).
SUPPORTED_IMAGE_FORMATS = ("png", "jpeg", "jpg", "webp")

#: 출력 스타일. 모델이 자연어 문장과 단부루 태그를 오가는 편차를 없애기 위해,
#: 스타일마다 시스템 프롬프트와 사용자 메시지를 다르게 고정한다.
STYLE_NATURAL = "natural"  # 서술형 자연어 문장 (V5 텍스트 인코더 강점 활용)
STYLE_DANBOORU = "danbooru"  # 쉼표 구분 단부루 태그 (1girl, solo, ...) — WD14 결과와 같은 형태
PROMPT_STYLES = (STYLE_NATURAL, STYLE_DANBOORU)

#: 출력 길이 3단. 모델에 따라 자연어가 지나치게 길게 나오는 것을 조절한다.
#: WD 태거는 태그 개수(임계값)로, LLM은 **시스템 프롬프트의 분량 지시**로 조절한다
#: (max_tokens로 자르면 문장이 중간에 끊기고 추론 모델은 답이 안 나온다).
LENGTH_SHORT = "short"
LENGTH_MEDIUM = "medium"
LENGTH_LONG = "long"
OUTPUT_LENGTHS = (LENGTH_SHORT, LENGTH_MEDIUM, LENGTH_LONG)

#: 안전 상한 토큰 수 — 길이 제어는 프롬프트 지시로 하고, 이 값은 무한 생성만 막는 넉넉한
#: 상한이다. 이보다 작게 잘라 문장이 중간에 끊기지 않도록 크게 잡는다 (특히 추론 모델은
#: 사고에 토큰을 많이 쓰므로 답변까지 나올 여유가 있어야 한다).
_SAFETY_MAX_TOKENS = 2048

#: 길이 → 시스템 프롬프트에 덧붙이는 **분량 지시**. 토큰으로 자르지 않고 모델이 스스로
#: 분량을 맞추게 한다 (자르면 문장이 중간에 끊기고, 추론 모델은 사고 중에 잘려 답이 안 나온다).
#: 대략적인 글자 수를 알려 주되 "완결된 하나"를 강조한다. "중간"도 지시를 준다.
_LENGTH_HINT = {
    LENGTH_SHORT: (
        "Length: keep it short and concise — roughly 200 characters or fewer, only the most "
        "important elements. Still finish as one complete, self-contained prompt."
    ),
    LENGTH_MEDIUM: (
        "Length: keep it moderate — roughly 200 to 500 characters, covering the key elements "
        "without going overboard. Finish as one complete prompt."
    ),
    LENGTH_LONG: (
        "Length: be thorough and detailed — you may use up to roughly 1000 characters, covering "
        "subject, appearance, action, composition, setting, lighting and mood."
    ),
}


def max_tokens_for_length(length: str) -> int:
    """LLM 안전 상한 토큰 수. 길이별로 다르지 않다 — 길이 제어는 프롬프트 지시로 한다.

    `length` 인자는 하위 호환을 위해 받되 무시한다 (예전 시그니처를 쓰는 코드 보호)."""
    return _SAFETY_MAX_TOKENS


#: 두 스타일 공통 출력 규약 (JSON만, 설명·코드펜스 금지).
_COMMON_RULES = (
    'Respond ONLY with a JSON object of the form {"prompt": "...", "negative_prompt": "..."} '
    'and nothing else. Put the main description in "prompt". Use "negative_prompt" only for '
    "things to avoid (leave it an empty string if you have no suggestions). Do not add "
    "explanations, headings, or code fences. "
    "When an image is provided, base the prompt on what you actually see in it; when text is also "
    "provided, treat that text as the user's intent and let it steer the result."
)

#: 자연어 스타일 시스템 프롬프트 — 태그 나열을 금지하고 서술형 문장을 강제한다.
SYSTEM_PROMPT_NATURAL = (
    "You are an assistant that writes image-generation prompts for NovelAI Diffusion V5. "
    "V5 has a strong natural-language text encoder, so write the prompt as vivid, flowing "
    "natural-language sentences that describe the subject, appearance, action, composition, "
    "setting, lighting and mood. "
    "Do NOT output comma-separated Danbooru tags (e.g. do not write things like "
    "'1girl, solo, long hair'); write real descriptive prose instead. " + _COMMON_RULES
)

#: 단부루 스타일 시스템 프롬프트 — 쉼표 구분 태그만 허용하고 문장을 금지한다.
SYSTEM_PROMPT_DANBOORU = (
    "You are an assistant that writes image-generation prompts as Danbooru-style tags. "
    "Output ONLY lowercase, comma-separated Danbooru tags (e.g. '1girl, solo, long hair, "
    "school uniform, classroom, sitting, looking at viewer'). "
    "Use tags for subject count, appearance, clothing, pose, expression, setting and style. "
    "Do NOT write natural-language sentences and do not add articles or punctuation other than "
    "the commas separating tags. " + _COMMON_RULES
)

#: 스타일 → 시스템 프롬프트.
_STYLE_SYSTEM_PROMPTS = {
    STYLE_NATURAL: SYSTEM_PROMPT_NATURAL,
    STYLE_DANBOORU: SYSTEM_PROMPT_DANBOORU,
}

#: 어시스턴트 모드 지시 — 스타일 기본 프롬프트 뒤에 덧붙는다. 스타일(문장/태그) 규약은
#: 그대로 유지하면서, "본 대로"가 아니라 "지시대로 변형해서" 프롬프트를 쓰게 한다.
ASSISTANT_INSTRUCTION = (
    "IMPORTANT — assistant/transform mode: an image is provided together with a text "
    "instruction. Do not simply describe the image as-is. Apply the changes the instruction "
    "asks for (for example replacing an outfit, changing the pose or camera angle, swapping the "
    "background or time of day) and write the prompt for the RESULTING, modified image. Keep "
    "everything the instruction does not mention faithful to the original image. Follow the "
    "output style stated above (natural-language sentences or Danbooru tags)."
)

#: 하위 호환: 예전 코드/설정이 참조하던 이름. 기본은 자연어 스타일이다.
DEFAULT_SYSTEM_PROMPT = SYSTEM_PROMPT_NATURAL


def system_prompt_for_style(style: str) -> str:
    """스타일에 해당하는 기본 시스템 프롬프트. 알 수 없는 값은 자연어로 본다."""
    return _STYLE_SYSTEM_PROMPTS.get(style, SYSTEM_PROMPT_NATURAL)


# ── 예외 계층 ────────────────────────────────────────────────


class LMStudioError(Exception):
    """LM Studio 연동 관련 모든 오류의 베이스."""


class LMStudioNotInstalled(LMStudioError):
    """`lmstudio` 패키지를 불러올 수 없다 (배포본에는 기본 포함, 소스 실행 시 미설치)."""


class LMStudioConnectionError(LMStudioError):
    """서버에 붙지 못했다 — LM Studio 앱 미실행/서버 미시작/host:port 오류."""


class LMStudioNoModelError(LMStudioError):
    """LM Studio에 로드된 LLM이 없다 (모델을 먼저 로드해야 한다)."""


class LMStudioTimeoutError(LMStudioError):
    """응답이 타임아웃됐다 — 더 작은 모델을 쓰거나 타임아웃을 늘려야 한다."""


class LMStudioVisionUnsupported(LMStudioError):
    """비전을 지원하지 않는 모델에 이미지를 넘겼다 (VLM이 아니다)."""


class LMStudioResponseError(LMStudioError):
    """그 외 서버/생성 오류. 원문을 보존한다."""


class LMStudioCancelled(LMStudioError):
    """사용자가 생성을 중단했다 (Stop 버튼)."""


# ── 설정·결과 ────────────────────────────────────────────────


@dataclass(frozen=True)
class LMStudioConfig:
    """생성 1회에 필요한 설정. UI가 `AppSettings.lmstudio`에서 만들어 넘긴다."""

    host: str = DEFAULT_HOST
    model: str = ""  # "" = 로드된 첫 모델을 자동 사용
    timeout: float = DEFAULT_TIMEOUT
    style: str = STYLE_NATURAL  # 출력 스타일 (STYLE_NATURAL | STYLE_DANBOORU)
    length: str = LENGTH_MEDIUM  # 출력 길이 (LENGTH_SHORT | LENGTH_MEDIUM | LENGTH_LONG)
    assistant: bool = False  # True = 어시스턴트(이미지+지시 변형) 모드
    system_prompt: str = ""  # "" = 스타일 기본 프롬프트. 값이 있으면 그 스타일 프롬프트 뒤에 덧붙는다

    def effective_system_prompt(self) -> str:
        """스타일 기본 프롬프트 (+ 어시스턴트 지시 + 길이 지시) + (있으면) 사용자 추가 지시.

        예전 동작(오버라이드 = 전면 교체)과 달리, 스타일 규약이 항상 유지되도록
        기본 프롬프트 **뒤에** 붙인다. 어시스턴트 모드면 변형 지시를, 길이 지시(짧게/중간/
        길게 각각 대략적 글자 수)를 더한다 — 길이 제어의 주된 수단이다."""
        base = system_prompt_for_style(self.style)
        if self.assistant:
            base = f"{base}\n\n{ASSISTANT_INSTRUCTION}"
        length_hint = _LENGTH_HINT.get(self.length, "")
        if length_hint:
            base = f"{base}\n\n{length_hint}"
        extra = self.system_prompt.strip()
        return f"{base}\n\n{extra}" if extra else base

    def max_tokens(self) -> int:
        """생성 안전 상한 토큰 수 (길이 제어가 아니라 무한 생성 방지용)."""
        return max_tokens_for_length(self.length)


# `PromptResult`는 parsing 모듈이 소유한다 — 위에서 import해 여기서 재수출한다.


# ── 가용성 ───────────────────────────────────────────────────


def runtime_error() -> str:
    """`lmstudio`를 쓸 수 있으면 "", 아니면 그 이유.

    WD14의 동명 함수와 같은 계약이다 — 의존성 부재를 안내 문구로 바꾼다.
    ImportError뿐 아니라 하위 의존성 적재 실패도 여기로 온다.
    """
    try:
        import lmstudio  # noqa: F401
    except Exception as e:  # noqa: BLE001 - DLL/하위 의존성 적재 실패도 잡는다
        return str(e)
    return ""


# ── 생성기 ───────────────────────────────────────────────────


class LMStudioPromptGenerator:
    """LM Studio 서버에 붙어 프롬프트를 생성한다.

    상태를 들지 않는다 — 매 호출마다 스코프 클라이언트를 새로 연다. 워커 스레드에서
    호출해야 한다 (`respond`가 블로킹이다).
    """

    @staticmethod
    def check_connection(host: str) -> bool:
        """지정 host:port에 LM Studio API 서버가 떠 있는지 (클라이언트 생성 없이 확인).

        `lmstudio`를 쓸 수 없으면 `LMStudioNotInstalled`.
        """
        lms = _import_lmstudio()
        try:
            return bool(lms.Client.is_valid_api_host(host or DEFAULT_HOST))
        except Exception as e:  # noqa: BLE001 - 잘못된 host 형식 등
            logger.debug("connection check failed for %s: %s", host, e)
            return False

    @staticmethod
    def list_models(host: str) -> list[str]:
        """서버에 **현재 로드된** LLM 식별자 목록 (`lms ps`와 동등). 붙지 못하면 연결 오류.

        모델명으로 `getOrLoad`를 부르지 않는다 — 이 목록은 서버가 이미 로드해 둔 것만
        보여 주고, 우리는 그중에서만 고른다. 그래서 우리 앱이 새 모델을 로드시키는 일이 없다.
        """
        lms = _import_lmstudio()
        client = None
        try:
            client = lms.Client(host or DEFAULT_HOST)
            loaded = client.llm.list_loaded()
            return [name for name in (_model_identifier(m) for m in loaded) if name]
        except Exception as e:  # noqa: BLE001
            raise _map_connection_error(e) from e
        finally:
            _close_client(client)

    def generate(
        self,
        text: str,
        image: bytes | None = None,
        config: LMStudioConfig | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> PromptResult:
        """텍스트/이미지를 프롬프트로 변환한다.

        Parameters
        ----------
        text : str
            사용자가 넣은 단어/문장 (비어 있어도 이미지가 있으면 된다).
        image : bytes | None
            분석할 이미지의 원본 바이트 (PNG/JPEG/WebP). VLM에서만 유효하다.
        config : LMStudioConfig | None
            연결·모델·타임아웃·시스템 프롬프트. None이면 기본값.
        should_cancel : Callable[[], bool] | None
            True를 돌려주면 스트리밍을 멈추고 서버에 취소를 보낸 뒤
            `LMStudioCancelled`를 던진다 (Stop 버튼).

        Returns
        -------
        PromptResult

        Raises
        ------
        LMStudioError
            연결/모델 부재/타임아웃/비전 미지원/취소/생성 실패.
        """
        config = config or LMStudioConfig()
        if not (text or "").strip() and image is None:
            raise LMStudioResponseError("nothing to generate: provide text or an image")

        lms = _import_lmstudio()
        self._apply_timeout(lms, config.timeout)
        cancelled = should_cancel or (lambda: False)

        # 스코프 컨텍스트 매니저(`with`) 대신 명시적으로 열고 finally에서 닫는다.
        # `with`로 결과 직후 빠져나가면 SDK가 관리하던 웹소켓이 close 핸드셰이크 없이
        # 끊겨 서버가 ECONNRESET으로 본다 (사용자 로그의 반복 증상). close()로 정상 종료한다.
        client = None
        try:
            client = lms.Client(config.host or DEFAULT_HOST)
            model = self._resolve_model(client, config.model)
            chat = lms.Chat(config.effective_system_prompt())
            images = self._prepare_images(lms, image)
            message = self._user_message(text, image is not None, config.style, config.assistant)
            chat.add_user_message(message, images=images)
            raw = self._stream_response(lms, model, chat, cancelled, config.max_tokens())
        except LMStudioError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_generation_error(e, has_image=image is not None) from e
        finally:
            _close_client(client)

        return parse_prompt_result(raw)

    # ── 내부 ─────────────────────────────────────────────────

    @staticmethod
    def _stream_response(lms, model, chat, cancelled: CancelCheck, max_tokens: int) -> str:
        """`respond_stream`으로 조각을 받으며 취소를 감시한다. 취소되면 `LMStudioCancelled`.

        스트리밍을 쓰는 이유: 논블로킹으로 조각마다 취소 여부를 볼 수 있어 Stop 버튼이
        즉시 듣는다. `max_tokens`는 **안전 상한**일 뿐이다 — 출력 길이는 시스템 프롬프트의
        분량 지시로 조절한다 (토큰으로 자르면 문장이 중간에 끊기기 때문).

        **추론(reasoning) 모델 대응:** 각 조각의 `reasoning_type`이 `"none"`인 것만 모은다.
        `"reasoning"`/`"reasoningStartTag"`/`"reasoningEndTag"` 조각은 모델의 사고 과정이라
        프롬프트에 들어가면 안 된다. `stream.result().content`는 사고 과정까지 합쳐 주므로
        쓰지 않고, 실제 답변 조각만 직접 이어붙인다 (짧게/중간에서 사고 텍스트가 결과에
        섞여 나오던 문제)."""
        stream = model.respond_stream(chat, config={"maxTokens": max_tokens})
        parts: list[str] = []
        try:
            for fragment in stream:
                if cancelled():
                    stream.cancel()
                    raise LMStudioCancelled("generation cancelled by user")
                if getattr(fragment, "reasoning_type", "none") == "none":
                    content = getattr(fragment, "content", None)
                    if isinstance(content, str):
                        parts.append(content)
        except LMStudioCancelled:
            raise
        # 스트림을 끝까지 받은 뒤 답변(비-추론) 조각만 합친다.
        stream.result()  # 스트림을 마무리한다 (통계/정리). 반환 content는 추론을 포함하므로 안 쓴다.
        return "".join(parts).strip()

    @staticmethod
    def _apply_timeout(lms, timeout: float) -> None:
        """동기 API 타임아웃을 설정한다. 0 이하면 무제한(None)."""
        try:
            lms.set_sync_api_timeout(timeout if timeout and timeout > 0 else None)
        except Exception as e:  # noqa: BLE001 - 타임아웃 설정 실패가 생성을 막으면 안 된다
            logger.debug("could not set sync api timeout: %s", e)

    @staticmethod
    def _resolve_model(client, model: str):
        """**로드된 모델 중에서** 핸들을 고른다. 모델명으로 로드를 요청하지 않는다.

        `model`이 비어 있으면 로드된 첫 모델을, 값이 있으면 로드된 것 중 식별자가
        일치(부분 일치 포함)하는 것을 쓴다. 일치하는 것이 없으면 로드된 첫 모델로
        폴백한다 — 서버에 없는 모델명을 `getOrLoad`로 던져 새로 로드시키지 않기 위해서다
        (사용자 로그의 '복수 모델 로딩' 원인).
        """
        try:
            loaded = client.llm.list_loaded()
        except Exception as e:  # noqa: BLE001
            raise _map_connection_error(e) from e
        if not loaded:
            raise LMStudioNoModelError("no LLM is loaded in LM Studio")

        wanted = (model or "").strip()
        if wanted:
            for handle in loaded:
                ident = _model_identifier(handle)
                if ident == wanted or (ident and wanted in ident) or (ident and ident in wanted):
                    return handle
            logger.info("requested model %r is not loaded; using first loaded model", wanted)
        return loaded[0]

    @staticmethod
    def _prepare_images(lms, image: bytes | None) -> list:
        """이미지 바이트를 SDK 핸들로. 없으면 빈 리스트."""
        if image is None:
            return []
        try:
            return [lms.prepare_image(image)]
        except Exception as e:  # noqa: BLE001
            raise LMStudioResponseError(f"could not prepare image: {e}") from e

    @staticmethod
    def _user_message(text: str, has_image: bool, style: str = STYLE_NATURAL, assistant: bool = False) -> str:
        """스타일을 사용자 메시지에서도 한 번 더 못박는다 (시스템 프롬프트를 소홀히 하는
        모델 대비). 자연어면 '문장으로', 단부루면 '태그로'를 명시한다.

        어시스턴트 모드는 텍스트를 '이미지에 적용할 변형 지시'로 다룬다."""
        text = (text or "").strip()
        kind = "Danbooru tags" if style == STYLE_DANBOORU else "a natural-language description"
        if assistant:
            if has_image and text:
                return f"Apply this change to the image and write {kind} for the modified image: {text}"
            if has_image:
                return f"Write {kind} for a NovelAI prompt based on this image."
            # 이미지 없이 지시만 — 어시스턴트가 변형할 대상이 없으니 지시대로 새로 쓴다.
            return f"Write {kind} for a NovelAI prompt for: {text}"
        if has_image and not text:
            return f"Write {kind} for a NovelAI prompt based on this image."
        if has_image:
            return f"Using this image and the following intent, write {kind} for a NovelAI prompt: {text}"
        return f"Write {kind} for a NovelAI prompt for: {text}"

    @staticmethod
    def _map_generation_error(exc: Exception, *, has_image: bool) -> LMStudioError:
        """SDK 예외를 우리 계층으로 매핑한다.

        SDK가 세분화된 예외 타입을 노출하지 않으므로 메시지 휴리스틱에 기댄다.
        확실히 구분되는 타임아웃/연결만 특정하고, 나머지는 원문을 살린다.
        """
        message = str(exc)
        lowered = message.lower()
        if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
            return LMStudioTimeoutError(message)
        if any(k in lowered for k in ("connection", "connect", "websocket", "refused", "unreachable")):
            return LMStudioConnectionError(message)
        if has_image and any(k in lowered for k in ("image", "vision", "vlm", "multimodal")):
            return LMStudioVisionUnsupported(message)
        return LMStudioResponseError(message)


def _import_lmstudio():
    """`lmstudio`를 import하거나 `LMStudioNotInstalled`를 던진다."""
    try:
        import lmstudio as lms
    except Exception as e:  # noqa: BLE001
        raise LMStudioNotInstalled(str(e)) from e
    return lms


def _close_client(client) -> None:
    """클라이언트 웹소켓을 정상 종료한다 (close 핸드셰이크). 실패해도 조용히 넘어간다.

    `with` 컨텍스트가 결과 직후 빠져나가며 연결이 갑자기 끊기면 서버가 ECONNRESET으로
    보고, 다음 연결에서 상태가 꼬일 수 있다. 명시적 close로 그 반복을 없앤다.
    """
    if client is None:
        return
    try:
        client.close()
    except Exception as e:  # noqa: BLE001 - 종료 실패가 결과 반환을 막으면 안 된다
        logger.debug("error closing LM Studio client: %s", e)


def _map_connection_error(exc: Exception) -> LMStudioError:
    """붙기/모델 조회 단계의 예외를 연결 오류로 매핑한다 (타임아웃은 구분)."""
    lowered = str(exc).lower()
    if "timed out" in lowered or "timeout" in lowered:
        return LMStudioTimeoutError(str(exc))
    return LMStudioConnectionError(str(exc))


def _model_identifier(handle: object) -> str:
    """로드된 모델 핸들에서 `client.llm.model(...)`에 다시 넣을 식별자를 뽑는다.

    LM Studio 1.5의 LLM 핸들은 생성자 인자 `model_identifier`를 인스턴스 속성으로
    들고 있고, 로드된 모델의 상세는 `get_info()`가 준다. SDK 버전차를 흡수하기 위해
    여러 후보를 순서대로 시도하고, 모두 실패하면 빈 문자열(→ UI가 무시)로 둔다.
    """
    for attr in ("identifier", "model_identifier", "_model_identifier", "model_key", "path", "key"):
        value = getattr(handle, attr, None)
        if isinstance(value, str) and value:
            return value
    # get_info()는 서버 왕복이 있을 수 있으나 목록 표시용으로 1회면 충분하다.
    get_info = getattr(handle, "get_info", None)
    if callable(get_info):
        try:
            info = get_info()
        except Exception:  # noqa: BLE001 - 정보 조회 실패는 목록에서 그 항목만 건너뛴다
            info = None
        for attr in ("identifier", "model_key", "path"):
            value = getattr(info, attr, None)
            if isinstance(value, str) and value:
                return value
    return ""
