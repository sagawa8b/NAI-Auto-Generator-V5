# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 빌드 정의 (Windows onedir).

CLI 플래그 대신 spec 파일을 쓰는 이유: V4의 distribute.bat은 절대 경로에 묶여 있어
빌드한 사람의 PC에서만 돌았다. 여기서는 spec 위치를 기준으로 상대 경로만 쓴다.

프로즌 빌드에서 조용히 깨지기 쉬운 두 가지를 여기서 막는다:

1. keyring 백엔드 — core/settings/credentials.py가 import 실패를 삼키므로,
   빠지면 "토큰이 저장되지 않는" 증상으로만 드러난다. collect_all로 통째로 넣는다.
2. 번들 리소스 — 언어 파일과 태그 DB는 `Path(__file__).parent.parent/"resources"`
   기준이라, 번들 안에서도 `naiauto/resources/`에 그대로 있어야 한다.

빌드 후 `NAI-Auto-V5.exe --selftest`가 위 둘을 실제로 확인한다 (릴리스 워크플로가 호출).

onefile은 만들지 않는다 — 실행할 때마다 100MB대를 임시 폴더에 풀어 시작이 느리고
백신 오탐도 잦다.
"""

import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_DIR = SPEC_DIR.parent
PACKAGE_DIR = PROJECT_DIR / "src" / "naiauto"
APP_NAME = "NAI-Auto-V5"

# 런타임 의존성이 빌드 환경에 없으면 PyInstaller는 경고만 남기고 그대로 빌드한다.
# 그러면 WD14가 빠진 배포본이 조용히 만들어져, 릴리스 검증(--selftest)에서야 드러난다.
# 실제로 v0.6.7 릴리스에서 공개 저장소의 pyproject.toml에 onnxruntime이 없어 이 일이
# 벌어졌다 — 빌드하는 자리에서 바로 멈추게 한다.
_MISSING = [
    name for name in ("onnxruntime", "numpy", "lmstudio") if importlib.util.find_spec(name) is None
]
if _MISSING:
    raise SystemExit(
        f"빌드 환경에 {', '.join(_MISSING)} 가 없습니다. WD14 자동 태깅 또는 자연어 프롬프트 생성이 "
        "빠진 배포본이 만들어지므로 여기서 멈춥니다 — `pip install .` 로 런타임 의존성을 먼저 설치하세요."
    )

keyring_datas, keyring_binaries, keyring_hiddenimports = collect_all("keyring")
# onnxruntime은 순수 파이썬이 아니다 — capi의 DLL이 함께 들어가야 import가 된다.
# 빠지면 WD14 자동 태깅만 조용히 "모델을 쓸 수 없음"으로 보인다.
onnx_datas, onnx_binaries, onnx_hiddenimports = collect_all("onnxruntime")
if not onnx_binaries:
    raise SystemExit("onnxruntime의 라이브러리를 수집하지 못했습니다 — 번들이 WD14를 쓸 수 없습니다.")

# lmstudio(자연어 프롬프트 생성)는 순수 파이썬이지만 웹소켓 등 하위 의존성을 지연 import한다.
# collect_all로 데이터·하이든임포트를 통째로 넣지 않으면 프로즌 빌드에서 `import lmstudio`가
# 조용히 실패해 이 기능만 "SDK를 쓸 수 없음"으로 보인다. --selftest가 import를 실제로 확인한다.
lms_datas, lms_binaries, lms_hiddenimports = collect_all("lmstudio")

datas = [
    # (원본, 번들 안 위치) — 앱이 naiauto/resources/... 로 찾는다
    (str(PACKAGE_DIR / "resources"), "naiauto/resources"),
    *keyring_datas,
    *onnx_datas,
    *lms_datas,
]

analysis = Analysis(
    [str(SPEC_DIR / "entry.py")],  # app.py를 직접 쓰면 상대 import가 깨진다 (entry.py 주석 참고)
    pathex=[str(PROJECT_DIR / "src")],
    binaries=[*keyring_binaries, *onnx_binaries, *lms_binaries],
    datas=datas,
    hiddenimports=["naiauto", *keyring_hiddenimports, *onnx_hiddenimports, *lms_hiddenimports],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "hypothesis"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,  # UPX 압축은 백신 오탐을 늘린다
    console=False,  # GUI 앱 — 콘솔 창 없이 뜬다
    icon=str(SPEC_DIR / "app_icon.ico"),
)

# 같은 번들을 가리키는 콘솔 모드 실행 파일. --selftest 검증 전용이다.
# GUI 서브시스템(console=False) 실행 파일은 sys.stdout이 없어 print가 조용히
# 사라지고 셸이 종료를 기다리지 못해, 릴리스 워크플로에서 출력도 종료 코드도
# 얻지 못했다. 분석 결과를 그대로 재사용하므로 빌드 시간은 거의 늘지 않는다.
# 워크플로가 검증을 마친 뒤 압축 전에 지운다 — 배포본에는 들어가지 않는다.
exe_selftest = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=f"{APP_NAME}-selftest",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

COLLECT(
    exe,
    exe_selftest,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

# 단일 파일 배포본 — 모든 것을 exe 하나에 담는다 (V4의 distribute.bat도 둘 다 만들었다).
# 대가: 실행할 때마다 약 144MB를 %TEMP%에 풀어 시작이 5~10초 걸리고 백신 오탐도 잦다.
# 그래서 폴더형이 기본이고 이건 "받아서 바로 실행" 편의용이다. 분석 결과를 재사용하므로
# 빌드 시간은 1분 남짓만 늘어난다.
exe_onefile = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name=f"{APP_NAME}-onefile",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(SPEC_DIR / "app_icon.ico"),
)
