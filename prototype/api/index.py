"""Vercel 서버리스 진입점.

Vercel의 Python 런타임은 이 파일의 `app`(ASGI 애플리케이션)을 찾아 실행한다.
로컬에서는 이 파일이 필요 없고 `uvicorn app.main:app`으로 직접 띄운다.
"""
import sys
from pathlib import Path

# 배포 시 작업 디렉터리가 api/ 가 될 수 있으므로 프로젝트 루트를 경로에 넣는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402,F401
