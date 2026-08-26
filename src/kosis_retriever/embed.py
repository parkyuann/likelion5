# -*- coding: utf-8 -*-
"""CLOVA embedding-v2 호출 (HCX_API_KEY, 재시도, 8000자 컷).
   같은 텍스트→같은 벡터(결정론)."""
import os, time, requests
from dotenv import load_dotenv
from pathlib import Path
from .config import EMB_URL, REPO

load_dotenv(REPO / ".env")
_KEY = os.environ.get("HCX_API_KEY")


def embed(text: str):
    """텍스트→1024차원 벡터. 실패 시 재시도, None 반환 가능."""
    for attempt in range(6):
        try:
            r = requests.post(EMB_URL,
                              headers={"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"},
                              json={"text": text[:8000]}, timeout=30)
            v = r.json().get("result", {}).get("embedding")
            if r.status_code == 200 and v:
                return v
        except Exception:
            pass
        time.sleep(min(2 ** attempt, 8))
    return None
