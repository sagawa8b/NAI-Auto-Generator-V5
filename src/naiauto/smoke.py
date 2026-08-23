"""V5 실토큰 스모크 테스트 CLI — Milestone 0 완료 조건 검증용.

로컬 PC에서 실행 (레포 클론 후):

    pip install -e .
    python -m naiauto.smoke                  # 토큰을 안전하게 입력받아 1장 생성
    python -m naiauto.smoke --anlas-only     # 생성 없이 로그인+Anlas 조회만
    python -m naiauto.smoke --subscription-json  # 구독 응답 전문 (비밀정보 제거)
    python -m naiauto.smoke --dry-run        # 네트워크 없이 payload만 출력
    python -m naiauto.smoke --image in.png                    # i2i
    python -m naiauto.smoke --image in.png --mask mask.png    # 인페인팅

i2i/인페인팅은 이미지를 multipart 파트로 보낸다 (2026-08-21 실서버 확인):
parameters.image/mask에는 파트 "이름"이 들어가고 바이트는 별도 파트로
전송된다. 자세한 내용은 payload_v5 모듈 docstring 참조.

토큰: --token 인자, NAI_TOKEN 환경변수, 또는 프롬프트 입력(화면에 안 보임).
NovelAI 웹 → User Settings → Account → "Get Persistent API Token" (pst-...).

검증 항목:
  1. pst- 토큰 로그인 + /user/subscription (Anlas 잔액)
  2. V5 multipart 생성 요청이 recaptcha_token 없이 수락되는지
  3. 저장된 PNG에 NAI tEXt 메타데이터가 보존되는지 (verbatim 저장 검증)
  4. --verbose 시 응답 헤더 전체 로그 (신규 rate-limit 헤더 관찰)
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import logging
import os
import random
import sys
from pathlib import Path

from PIL import Image

from .core.api.client import NAIClient
from .core.api.errors import (
    AuthError,
    InsufficientAnlasError,
    ModelSpecIncompleteError,
    NAIError,
    PayloadRejectedError,
    RateLimitError,
)
from .core.api.model_specs import MODEL_REGISTRY, get_spec
from .core.api.models import GenerationRequest
from .core.api.session import NAISession
from .core.api.subscription import parse_anlas, redact, unknown_keys
from .core.metadata.naiinfo import read_metadata
from .core.metadata.save import save_raw_png

DEFAULT_PROMPT = "1girl, solo, looking at viewer, outdoors, rating:general"


def build_request(args: argparse.Namespace) -> GenerationRequest:
    spec = get_spec(args.model)
    defaults = dict(spec.defaults)
    seed = args.seed if args.seed >= 0 else random.randint(1, 2**32 - 1)

    image = Path(args.image).read_bytes() if args.image else None
    mask = Path(args.mask).read_bytes() if args.mask else None
    if mask is not None:
        action = "infill"
    elif image is not None:
        action = "img2img"
    else:
        action = "generate"

    width, height = args.width, args.height
    if image is not None:
        # i2i/infill은 원본 크기를 그대로 쓰는 것이 안전하다
        with Image.open(io.BytesIO(image)) as img:
            width, height = img.size

    return GenerationRequest(
        action=action,
        prompt=args.prompt + spec.quality_tags,
        negative_prompt=spec.uc_presets.get("heavy", ""),
        model=spec.key,
        width=width,
        height=height,
        seed=seed,
        steps=args.steps if args.steps else defaults.get("steps", 28),
        cfg_scale=defaults.get("cfg_scale", 5.0),
        cfg_rescale=defaults.get("cfg_rescale", 0.0),
        sampler=defaults.get("sampler", "k_euler_ancestral"),
        scheduler=defaults.get("scheduler", "native"),
        image=image,
        mask=mask,
        strength=args.strength,
    )


def resolve_token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get("NAI_TOKEN", "")
    if not token:
        token = getpass.getpass("NovelAI Persistent API Token (pst-..., 입력 숨김): ").strip()
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m naiauto.smoke",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--token", default=None, help="pst- 토큰 (생략 시 NAI_TOKEN env 또는 프롬프트)")
    parser.add_argument("--model", default="naid5f", choices=sorted(MODEL_REGISTRY))
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--width", type=int, default=832)
    parser.add_argument("--height", type=int, default=1216)
    parser.add_argument("--steps", type=int, default=0, help="0 = 모델 기본값")
    parser.add_argument("--seed", type=int, default=-1, help="-1 = 랜덤")
    parser.add_argument("--image", default=None, help="i2i 원본 PNG (지정 시 action=img2img)")
    parser.add_argument(
        "--mask", default=None, help="인페인팅 마스크 PNG (지정 시 action=infill, --image 필수)"
    )
    parser.add_argument("--strength", type=float, default=0.7, help="i2i/인페인팅 디노이즈 강도")
    parser.add_argument("--out", type=Path, default=Path("./smoke_out"), help="저장 폴더")
    parser.add_argument("--anlas-only", action="store_true", help="로그인+Anlas 조회만")
    parser.add_argument(
        "--subscription-json",
        action="store_true",
        help="/user/subscription 응답 전문 출력 (비밀정보 자동 제거). 생성은 하지 않음",
    )
    parser.add_argument("--dry-run", action="store_true", help="네트워크 없이 payload만 출력")
    parser.add_argument("--verbose", action="store_true", help="응답 헤더 등 DEBUG 로그")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    spec = get_spec(args.model)

    if args.dry_run:
        req = build_request(args)
        payload = spec.build_payload(req, spec)
        print(f"[dry-run] endpoint: {spec.endpoint} (format={spec.request_format})")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    token = resolve_token(args)
    session = NAISession()
    try:
        session.login_with_token(token)
    except AuthError as e:
        print(f"토큰 형식 오류: {e}", file=sys.stderr)
        return 1

    client = NAIClient(session, debug_headers=args.verbose, forensics_dir=args.out / "forensics")

    try:
        subscription = client.get_subscription()
        anlas = parse_anlas(subscription)
        # V5부터 Opus도 자동 충전 크레딧을 쓰고 소진되면 Anlas가 나간다 ("무제한" 아님)
        opus = " (Opus)" if anlas["opus"] else ""
        print(f"✔ 로그인 성공 — Anlas 잔액: {anlas['total']}{opus}")
    except NAIError as e:
        print(f"✘ 로그인/구독 조회 실패: {e}", file=sys.stderr)
        return 1

    if args.subscription_json:
        # V5 생성 크레딧 필드를 찾기 위한 원문 덤프. 그대로 붙여넣어 공유해도 안전하다.
        print(json.dumps(redact(subscription), indent=2, ensure_ascii=False, sort_keys=True))
        extra = unknown_keys(subscription)
        print(f"\n# 아직 해석하지 않는 최상위 키: {', '.join(extra) or '(없음)'}", file=sys.stderr)
        return 0

    if args.anlas_only:
        return 0

    req = build_request(args)
    print(f"→ {spec.api_name} 생성 요청 중... (seed={req.seed}, {req.width}x{req.height}, steps={req.steps})")

    try:
        result = client.generate(req)
    except PayloadRejectedError as e:
        print(f"✘ 서버가 payload를 거부 (HTTP {e.status}) — 응답 원문:", file=sys.stderr)
        print(e.body[:2000], file=sys.stderr)
        print(
            "\n힌트: 위 메시지가 V5 스펙의 단서입니다 — 그대로 공유해 주시면 "
            "spec/v5/V5_SPEC.md에 반영해 대응합니다.",
            file=sys.stderr,
        )
        return 2
    except RateLimitError as e:
        print(f"✘ 레이트 리밋 (429), Retry-After={e.retry_after}", file=sys.stderr)
        return 2
    except InsufficientAnlasError:
        print("✘ Anlas 부족 (402) — 해상도/스텝을 무료 범위로 낮춰 보세요.", file=sys.stderr)
        return 2
    except ModelSpecIncompleteError as e:
        print(f"✘ 미구현 기능: {e}", file=sys.stderr)
        return 2
    except NAIError as e:
        print(f"✘ 생성 실패: {e}", file=sys.stderr)
        return 2

    path = save_raw_png(result.raw_bytes, args.out, context={"seed": req.seed, "model": spec.key})
    print(f"✔ 생성 성공 — 저장: {path} ({len(result.raw_bytes):,} bytes)")

    meta = read_metadata(path)
    if meta and meta.get("origin") == "text_chunk":
        print("✔ PNG tEXt 메타데이터 보존 확인 (verbatim 저장 정상)")
        print(f"  seed(회수): {meta.get('seed')} | model: {meta.get('model')}")
        if meta.get("comment"):
            print(f"  Comment 키: {sorted(meta['comment'].keys())[:12]}")
    elif meta:
        print(f"⚠ 메타데이터가 {meta['origin']} 경로로만 판독됨 — tEXt 청크 구조 변화 가능성, raw 확인 필요")
    else:
        print("⚠ 저장된 PNG에서 메타데이터를 찾지 못함 — V5 메타데이터 스키마 변화 가능성")

    print("\n스모크 테스트 완료. 결과(성공/거부 메시지)를 공유해 주시면 스펙 문서에 반영합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
