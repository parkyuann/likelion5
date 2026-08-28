"""
NCP CLOVA Studio HCX 임베딩 API 클라이언트.

table_index_experiment.py에서 TF-IDF 대신 실제 임베딩으로 표 색인 그레뉴러리티
실험을 재현하기 위해 작성. 다른 스크립트(fewshot_claim_extractor.py 등)와 동일하게
.env의 NCP_CLOVASTUDIO_API_KEY를 그대로 쓴다 — Clova Studio는 채팅완성/임베딩이
같은 키를 공유하므로 별도 키를 새로 받을 필요가 없다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe -c "from src.hcx_embedding_client import embed; print(len(embed('테스트')))"
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ["NCP_CLOVASTUDIO_API_KEY"]

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
