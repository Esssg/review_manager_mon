from pathlib import Path
import sys


# Vercel 배포 환경에서도 src 디렉터리의 기존 API 코드를 찾을 수 있게 경로를 추가합니다.
src_path = Path(__file__).resolve().parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from review_manager_mon.api.app import app
