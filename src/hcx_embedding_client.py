"""
NCP CLOVA Studio HCX 임베딩 API 클라이언트.

table_index_experiment.py에서 TF-IDF 대신 실제 임베딩으로 표 색인 그레뉴러리티
실험을 재현하기 위해 작성. HCX_API_KEY는 .env에 저장(커밋 안 됨, .gitignore).

사용 예 (레포 루트에서):
    venv/Scripts/python.exe -c "from src.hcx_embedding_client import embed; print(len(embed('테스트')))"
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ["HCX_API_KEY"]

EMBEDDING_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"


def embed(text: str) -> list[float]:
    """텍스트 1건을 HCX 임베딩 벡터로 변환한다."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    res = requests.post(EMBEDDING_URL, headers=headers, json={"text": text}, timeout=10)
    data = res.json()
    if res.status_code != 200 or data.get("status", {}).get("code") != "20000":
        raise RuntimeError(f"HCX 임베딩 API 오류 (status={res.status_code}): {data.get('status')}")
    return data["result"]["embedding"]
