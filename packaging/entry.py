"""PyInstaller 진입 스크립트.

`ui/app.py`를 직접 진입점으로 삼으면 프로즌 실행 파일에서 `__main__`으로 실행되어
패키지 밖에 놓이고, 모듈 안의 상대 import(`from .. import __version__`)가
`ImportError: attempted relative import with no known parent package`로 죽는다.
패키지를 정상 import해서 넘겨주는 얇은 껍데기가 필요한 이유다.
"""

import sys

from naiauto.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
