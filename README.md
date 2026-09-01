# NAI Auto Generator V5

NovelAI Diffusion **V5** 이미지 생성을 자동화하는 데스크톱 앱입니다.
웹 인터페이스에 없는 연속 생성 · 와일드카드 · 프롬프트 관리 기능을 더해, 반복 작업을 맡겨 둘 수 있습니다.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Qt](https://img.shields.io/badge/GUI-PySide6-41cd52.svg)](https://doc.qt.io/qtforpython/)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-orange.svg)](LICENSE)
[![Downloads](https://img.shields.io/github/downloads/sagawa8b/NAI-Auto-Generator-V5/total.svg)](https://github.com/sagawa8b/NAI-Auto-Generator-V5/releases)

[한국어](#한국어) · [English](#english)

> [NAI-Auto-Generator-V4](https://github.com/sagawa8b/NAI-Auto-Generator-V4)(V4.5용)의 후속작으로,
> V5 전용으로 기초부터 새로 만들었습니다. V4.5를 쓰신다면 V4 저장소를 이용해 주세요.

<!-- 스크린샷 자리 — 이미지를 저장소에 올린 뒤 아래 주석을 풀어 주세요
<img width="960" alt="main window" src="docs/screenshot_main.png">
-->

---

## 한국어

### ✨ 주요 기능

- **🎨 V5 전용** — `nai-diffusion-5-full` / `nai-diffusion-5-curated`
- **🔁 연속 생성** — 매수(0 = 무한)와 간격을 정해 두고 자동 생성.
  레이트리밋은 `Retry-After`를 지켜 대기하고, 일시적 오류는 5 → 10 → 20초 백오프로 재시도합니다
- **⚡ 퀵 매수 버튼** — 생성 바의 `5장 / 10장 / 30장 / 200장`을 누르면 그 매수로 바로 시작합니다.
  버튼 값은 옵션 → 생성에서 바꿉니다
- **👥 캐릭터 프롬프트** — 캐릭터마다 프롬프트·네거티브를 따로 쓰고,
  생성 해상도 비율 캔버스에서 마커를 끌어 위치를 지정합니다 (AI 자동 배치도 선택 가능)
- **🏷 태그 자동완성** — Danbooru 태그 DB가 **내장**되어 설정 없이 바로 동작합니다.
  가중치 접두사(`1.5::`)를 보존하고 캐릭터 슬롯 입력창에서도 뜹니다
- **🎲 와일드카드** — `__파일__` 랜덤 · `__=파일__` 공유 랜덤 · `##파일##` 순차 · `##파일*3##` 반복
- **🖌 아티스트 조합** — `{artist:그룹}` 랜덤 / `{artist_loop:그룹}` 순차 치환
- **🖼 i2i · 인페인팅** — 브러시 마스크 편집기 내장, 강도·노이즈 조절
- **🔍 강화 업스케일(Enhance)** — 이미 만든 그림을 다시 그려 크게 만듭니다.
  웹 UI와 같은 `1x / 1.5x / Max` 세 배율이고, Max는 서버가 상한(약 3.1MP)까지 키웁니다
  (1024×1024 → 1773×1773). 원본 PNG에 남은 프롬프트·설정을 그대로 가져오고,
  폴더를 통째로 골라 한 장씩 강화할 수도 있습니다
- **🗂 결과 관리** — 갤러리 뷰, 프리셋, 파일명 템플릿,
  PNG를 창에 끌어다 놓으면 생성 정보를 읽어 설정 복원
- **🤖 WD14 자동 태깅** — 이미지에서 태그를 뽑아 프롬프트에 넣거나 클립보드로 복사합니다.
  모델은 옵션 → 태그에서 고르고 **앱 안에서 바로 내려받습니다** (진행률 표시·취소 가능)
- **📊 상태 표시** — V5 생성 크레딧 게이지 + Anlas 잔액
- **🌏 4개 언어** — 한국어 / English / 日本語 / 中文, 재시작 없이 전환
- **🔔 새 버전 확인** — 릴리스가 올라오면 앱이 알려 줍니다 (자동 확인은 끌 수 있음)

### 📥 다운로드 (Windows)

[**Releases**](https://github.com/sagawa8b/NAI-Auto-Generator-V5/releases/latest)에서 받으세요.
**Python 설치는 필요 없습니다.** 둘 중 편한 쪽을 고르면 됩니다.

| 파일 | 설명 |
|---|---|
| `...-windows.zip` | **권장.** 압축을 풀고 `NAI-Auto-V5.exe` 실행. 시작이 빠릅니다. 같이 나오는 `_internal` 폴더는 프로그램 파일이니 지우지 마세요 (zip 약 58MB, 해제 약 143MB) |
| `...-windows-onefile.exe` | 받아서 바로 실행. 대신 실행할 때마다 내용을 임시 폴더에 풀어 **시작이 5~10초 걸립니다** |

> 서명하지 않은 실행 파일이라 처음 실행할 때 Windows SmartScreen 경고가 뜹니다 —
> `추가 정보` → `실행`을 누르면 됩니다.

앱이 새 버전을 알려 줍니다 (`기타` → `새 버전 확인`, 시작할 때 자동 확인은 옵션에서 끌 수 있습니다).

### 📦 소스에서 실행 (개발자용)

Python 3.10 이상이 필요합니다.

```bash
git clone https://github.com/sagawa8b/NAI-Auto-Generator-V5.git
cd NAI-Auto-Generator-V5
pip install -e .
nai-auto-v5
```

### 🔑 로그인

NovelAI 웹에서 발급한 **`pst-` 영구 API 토큰**을 입력합니다.
`기억하기`를 켜면 OS 키링(Windows 자격 증명 관리자 / macOS 키체인 / Secret Service)에 저장되고,
설정 파일에는 평문으로 남지 않습니다.

### ⌨️ 단축키

| 키 | 기능 | 키 | 기능 |
|---|---|---|---|
| `Ctrl+,` | 옵션 | `F2` | i2i 패널 |
| `Ctrl+P` | 프리셋 | `F3` | 갤러리 |
| `Ctrl+T` | WD14 자동 태깅 | `F4` | 강화 업스케일 패널 |
| `Ctrl+L` | 로그 보기 | `F11` | 결과 패널 접기 |
| `Ctrl+R` | 레이아웃 초기화 | | |

### 📁 파일 위치

폴더는 첫 실행 때 자동으로 만들어지고, 옵션 → 폴더에서 위치를 바꿀 수 있습니다.

**데이터 폴더** — Windows `%LOCALAPPDATA%\NAI-Auto-V5\NAI-Auto-V5` ·
Linux `~/.local/share/NAI-Auto-V5` · macOS `~/Library/Application Support/NAI-Auto-V5`

| 항목 | 위치 |
|---|---|
| 결과 이미지 | 데이터 폴더의 `results/` |
| 와일드카드 | 데이터 폴더의 `wildcards/` (`.txt`, 한 줄에 하나) |
| 프리셋 | 데이터 폴더의 `presets/` |
| 아티스트 조합 | 데이터 폴더의 `artist_combos/` (`{"그룹": ["artist:aaa", ...]}` JSON) |
| WD14 모델 | 데이터 폴더의 `wd14/` (옵션 → 태그에서 폴더를 바꾸거나 모델을 내려받습니다) |
| 로그 | OS 표준 로그 폴더 (도구 → 로그 보기에서 `폴더 열기`) |

### ⚠️ 아직 안 되는 것

- **Precise Reference / Curated Inpainting / Vibe Transfer** — NovelAI가 V5로 아직 출시하지 않았습니다

### 🙏 크레딧

- 원작: [DCP-arca/NAI-Auto-Generator](https://github.com/DCP-arca/NAI-Auto-Generator)
- stealth PNG 메타데이터 리더: [neggles/sd-webui-stealth-pnginfo](https://github.com/neggles/sd-webui-stealth-pnginfo)
- WD14 태거 모델: [SmilingWolf](https://huggingface.co/SmilingWolf) ·
  [pythongosssss/ComfyUI-WD14-Tagger](https://github.com/pythongosssss/ComfyUI-WD14-Tagger)

위 구성요소는 각자의 라이선스를 따릅니다. 이 저장소 자체는 [PolyForm Noncommercial 1.0.0](LICENSE) — **비상업적 용도로만** 사용할 수 있습니다.

이 앱은 NovelAI의 비공식 서드파티 도구입니다. 이용 시 NovelAI 이용약관을 지켜 주세요.

---

## English

A desktop app that automates image generation with **NovelAI Diffusion V5** — batch generation,
wildcards and prompt tooling that the web interface does not provide.

### Features

- **V5 only** — `nai-diffusion-5-full` / `nai-diffusion-5-curated`
- **Batch generation** with count (0 = unlimited) and interval; honours `Retry-After` on rate limits
  and retries transient failures with 5 → 10 → 20 s backoff
- **Quick count buttons** — one click on `5 / 10 / 30 / 200 images` starts a batch of that size;
  the four values are configurable in Options → Generation
- **Character prompts** — per-character prompt/negative, positions dragged on a canvas that matches
  the target aspect ratio (or left to the model)
- **Tag autocomplete** — Danbooru tag database **bundled**, works out of the box, preserves weight
  prefixes (`1.5::`), also attached to character slots
- **Wildcards** — `__file__` random · `__=file__` shared random · `##file##` sequential · `##file*3##` repeat
- **Artist combos** — `{artist:group}` random / `{artist_loop:group}` sequential
- **img2img & inpainting** with a built-in brush mask editor
- **Enhance (upscaling)** — redraw a finished image larger, with the same `1x / 1.5x / Max` amounts
  as the web UI. Max lets the server upscale to the cap (~3.1 MP, e.g. 1024×1024 → 1773×1773).
  Settings are taken from the source PNG, and a whole folder can be enhanced one image at a time
- **Result tools** — gallery view, presets, filename templates, drag a PNG onto the window to
  restore its generation settings
- **WD14 auto-tagging** — pull tags out of an image into the prompt or the clipboard; models are
  chosen and **downloaded from inside the app** (Options → Tags), with progress and cancel
- **Status bar** — V5 generation credit gauge and Anlas balance
- **4 languages** — Korean / English / Japanese / Chinese, switchable at runtime
- **Update check** — the app tells you when a new release is out (can be turned off)

### Download (Windows)

Two builds on [**Releases**](https://github.com/sagawa8b/NAI-Auto-Generator-V5/releases/latest),
no Python installation needed. The `...-windows.zip` (about 58 MB, ~143 MB unpacked) is recommended —
unpack it and run `NAI-Auto-V5.exe`; the `_internal` folder next to it is part of the program. The
`...-windows-onefile.exe` is a single file you can run straight away, at the cost of a 5–10 s startup
while it unpacks itself each time. Both are unsigned, so Windows SmartScreen shows a warning on first
launch (`More info` → `Run anyway`).

### Run from source

Requires Python 3.10+.

```bash
git clone https://github.com/sagawa8b/NAI-Auto-Generator-V5.git
cd NAI-Auto-Generator-V5
pip install -e .
nai-auto-v5
```

Log in with a **`pst-` persistent API token** from NovelAI. With *Remember* enabled it is stored in
the OS keyring — never written to the settings file in plain text.

### Not available yet

Precise Reference / Curated Inpainting / Vibe Transfer (not shipped by NovelAI for V5 yet).

### Credits & license

Based on [DCP-arca/NAI-Auto-Generator](https://github.com/DCP-arca/NAI-Auto-Generator); stealth PNG
reader from [neggles/sd-webui-stealth-pnginfo](https://github.com/neggles/sd-webui-stealth-pnginfo);
WD14 tagger models by [SmilingWolf](https://huggingface.co/SmilingWolf). Those components keep their
own licenses. This repository is licensed under [PolyForm Noncommercial 1.0.0](LICENSE) —
**noncommercial use only**.

Unofficial third-party tool. Please follow NovelAI's Terms of Service when using it.
