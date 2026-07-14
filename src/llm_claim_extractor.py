"""
LLM(HCX) 기반 뉴스 주장 추출.

`claim_extractor.py`(정규식 기반)는 "숫자 + 비교표현"만 보는 재현율 위주 1차
스크리닝이라, 통계로 검증 불가능한 개별사례 문장도 그대로 통과시킨다
(예: "최고령"의 "최고"가 순위 표현으로 오인되는 식). 이 모듈은 팀원이
eda.ipynb 6장에서 프로토타입으로 짜둔 HCX Chat Completions 기반 추출기를
`src/` 정식 모듈로 옮긴 것 — LLM에게 "집계통계로 검증 가능한 주장만" 뽑게
지시해서 개별사례/전망/수사적 표현을 프롬프트 단계에서 걸러낸다.

원본 프로토타입과의 차이 (실측으로 확인 후 수정한 것):
  - API 키 환경변수명을 프로젝트 관례인 HCX_API_KEY로 통일
    (원본은 NCP_CLOVASTUDIO_API_KEY였으나 같은 CLOVA Studio 키를 쓰므로 값은 동일)
  - SYSTEM_PROMPT/CLAIM_SCHEMA는 원본 그대로 두면 안 됐다 — "제주항공 참사 최고령
    희생자인 배모(78) 씨 일가족 9명이 키우던 반려견이다" 문장으로 원본 프롬프트를
    그대로 테스트해보니, claim_type="non_numeric" 범주 덕에 이 개별사례 문장을
    그대로 claim으로 뽑아버렸다(원본의 "기업 관계자 개인 경험" 배제 문구가 이
    기사 장르엔 안 맞았음). 우리 프로젝트는 애초에 KOSIS 통계표와 비교 가능한
    "집계통계"만 검증 대상이라 non_numeric 범주 자체가 스코프 밖이라고 보고
    제거했고, 스키마 필드도 `claim_extraction_schema.md`의 필드(indicator_raw/
    population/value/unit/time_ref/source_org_raw)에 맞춰 다시 짰다. 판단
    기준으로 "이 숫자가 바뀌면 공식 통계표 셀 값도 바뀌어야 하는가"를 명시하고,
    이 실패 사례 자체를 프롬프트 안에 부정 예시로 박아넣었다.
  - 재검증: 위 개별사례 문장은 0건으로 정상 제외됨. 양성 대조군("비정규직
    근로자는 856만8000명으로 전년 동월 대비 11만 명 증가") 문장은 정상 추출됨
    — 과도하게 깐깐해져서 진짜 통계 주장까지 거르지는 않는 것도 확인.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/llm_claim_extractor.py  # 샘플 1건 테스트
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests

NCP_MODEL = "HCX-007"
NCP_API_URL = f"https://clovastudio.stream.ntruss.com/v3/chat-completions/{NCP_MODEL}"
MAX_CHARS_PER_CHUNK = 40_000
CHUNK_OVERLAP = 500
REQUEST_INTERVAL_SECONDS = 0.5


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env_file(Path(__file__).resolve().parent.parent / ".env")
API_KEY = os.environ.get("HCX_API_KEY", "").strip()

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_text": {"type": "string", "description": "기사에 명시된 완결된 주장(문맥 없이도 이해 가능하게)"},
                    "indicator_raw": {"type": "string", "description": "무엇에 대한 수치인지(예: 실업률, 수출액, 인구)"},
                    "population": {"type": "string", "description": "모집단(예: 15~29세 청년, 전국 가구)"},
                    "value": {"type": "string"},
                    "unit": {"type": "string"},
                    "time_ref": {"type": "string"},
                    "source_org_raw": {"type": ["string", "null"], "description": "이 주장을 발표/인용한 기관명(문장 어디에 있든). 문장에 출처가 전혀 없으면 null"},
                    "evidence_quote": {"type": "string", "description": "본문의 짧은 직접 근거 문구"},
                },
                "required": ["claim_text", "indicator_raw", "value", "unit", "source_org_raw", "evidence_quote"],
            },
        }
    },
    "required": ["claims"],
}

SYSTEM_PROMPT = """
당신은 뉴스 기사에서 KOSIS(국가데이터처) 공식 통계표와 대조해 검증할 수 있는
집계통계 주장만 추출하는 분석가입니다. 목적은 "이 문장이 사실인지 KOSIS
통계표를 찾아 대조해볼 수 있는가"입니다 — 이 기준을 통과 못 하면 claims에
포함하지 않습니다.

추출 대상 (전부 "모집단 전체 또는 부분집합에 대한 집계 수치"여야 함):
- 특정 시점의 집계 수치(예: 실업률, 수출액, 인구, 출생아 수)
- 비율 및 구성비
- 증가율·감소율, 전년/전월 대비 변화
- 두 시점 또는 두 집단 간 비교
- 순위, 최고·최저(전국/집단 단위 통계에서의 순위여야 함)
- 일정 기간의 추세
- 이상·이하·초과·미만 등 범위 주장

절대 추출하면 안 되는 것 (숫자가 있어도 제외):
- 특정 "개인"의 신상 정보·서사·나이·가족관계·거주지·직업
  예시(제외해야 함): "제주항공 참사 최고령 희생자인 배모(78) 씨 일가족 9명이
  키우던 반려견이다" — 78세, 9명이라는 숫자가 있지만 이건 특정 개인 배모 씨
  가족에 대한 서술이지, KOSIS의 "고령자 인구" 같은 집계 통계와 비교할 수 있는
  주장이 아닙니다. 이런 문장은 claims에 넣지 마세요.
- "큰사위는 주말마다 집을 찾았다" 같은 개인의 생활 습관·행동 묘사
- 주관적 평가, 전망과 희망, 수사적 표현
- 단순 날짜·주소·인원 소개(숫자가 사람 수를 세는 것뿐이고 통계지표가 아닌 경우)
- 의미가 불분명한 숫자

판단 기준: "이 숫자가 바뀌면 통계청/한국은행 등 공식 통계표의 한 셀 값도
바뀌어야 하는가?"를 자문하세요. 아니라면(특정 개인·특정 1회성 사건에만 해당하는
숫자라면) 추출하지 마세요.

출처(source_org_raw) 찾는 법 — 반드시 채우려고 시도할 것:
- 문장 앞: "통계청에 따르면", "한국은행이 5일 발표한"
- 문장 뒤: "...라고 통계청은 밝혔다", "...로 조사됐다(주체가 앞 문장에 있으면 그 주체)"
- 인용부호 안/괄호 안에 기관명이 있는 경우도 포함
- 이 주장이 속한 문단 전체를 보고, 가장 가까운 곳에 언급된 발표 주체를 찾으세요.
- 정말 아무 데도 출처가 없으면(예: 기자 자체 서술, 출처 불명) null을 씁니다 — 없는데 지어내지 마세요.

규칙:
1. 한 문장에 여러 Claim이 있으면 각각 분리합니다.
2. 원문에 없는 숫자나 단위를 생성하지 않습니다.
3. '약', '가량', '이상', '넘는' 등의 한정 표현을 보존합니다.
4. claim_text는 앞 문맥 없이도 이해 가능한 문장으로 작성합니다.
5. 위 기준을 통과하는 주장이 하나도 없으면 claims를 빈 배열로 반환합니다.
6. 값을 모르면 null을 쓰고 임의로 채우지 않습니다.
"""


def split_article(text: str, max_chars: int = MAX_CHARS_PER_CHUNK, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(text.rfind(". ", start, end), text.rfind("다. ", start, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def call_clova_claim_api(article_text: str, max_retries: int = 3) -> tuple[dict, dict]:
    if not API_KEY:
        raise RuntimeError("HCX_API_KEY가 .env에 설정되지 않았습니다.")
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"다음 기사에서 주장을 추출하세요.\n\n{article_text}"},
        ],
        "topP": 0.8, "topK": 0, "temperature": 0.1,
        "repetitionPenalty": 1.1, "maxCompletionTokens": 4096,
        "thinking": {"effort": "none"},
        "responseFormat": {"type": "json", "schema": CLAIM_SCHEMA},
    }
    for attempt in range(max_retries):
        response = requests.post(NCP_API_URL, headers=headers, json=body, timeout=180)
        if response.status_code == 429 or response.status_code >= 500:
            if attempt + 1 < max_retries:
                time.sleep(2 ** attempt)
                continue
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", payload)
        content = result.get("message", {}).get("content", "")
        if not content:
            raise RuntimeError(f"CLOVA 응답에 content가 없습니다: {payload}")
        return json.loads(content), result.get("usage", {})
    raise RuntimeError("CLOVA API 재시도 횟수를 초과했습니다.")


def extract_claims_from_article(article_text: str) -> tuple[list[dict], dict]:
    unique: dict[str, dict] = {}
    total_usage = {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0}
    for chunk_number, chunk in enumerate(split_article(article_text), 1):
        parsed, usage = call_clova_claim_api(chunk)
        for claim in parsed.get("claims", []):
            claim["chunk_number"] = chunk_number
            unique.setdefault(claim.get("claim_text", "").strip(), claim)
        for key in total_usage:
            total_usage[key] += int(usage.get(key, 0) or 0)
        time.sleep(REQUEST_INTERVAL_SECONDS)
    unique.pop("", None)
    return list(unique.values()), total_usage


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    PUDDING_ARTICLE = (
        "무안국제공항 제주항공 여객기 참사로 일가족 9명을 잃은 반려견 '푸딩이'가 구조됐다. "
        "동물권보호단체 케어는 지난달 31일 공식 인스타그램을 통해 보호자 없이 마을을 배회하던 "
        "푸딩이를 안전하게 보호 중이라고 밝혔다. 푸딩이는 제주항공 참사 최고령 희생자인 배모(78) "
        "씨 일가족 9명이 키우던 반려견이다. 케어는 푸딩이가 홀로 남았다는 언론 보도와 제보를 접하고 "
        "즉시 전남 영광으로 이동했고, 현장에 도착해 마을회관 밖에서 조용히 앉아있는 푸딩이를 "
        "발견했다. 케어 측은 \"우리를 보자마자 반갑게 달려오는 모습이 영락없이 가족을 기다렸단 "
        "생각이 들었다\"고 했다. 케어는 보호자 없이 마을을 배회하고 있는 푸딩이가 위험하다고 판단해 "
        "구조를 결정했다. 장례식장에 있는 유가족들과 연락해 협의 끝에 푸딩이를 임시로 보호하기로 "
        "했다. 케어는 푸딩이의 건강 상태에 대해서도 우려했다. 서울로 이동하는 과정에서 푸딩이가 "
        "닭뼈와 양파, 김치 등을 토하는 등 그동안 보살핌 없이 적절한 음식을 제공받지 못한 것으로 "
        "보였다. 케어 측은 \"유가족과 협의해 일단 서울에서 보호하고 향후 논의하기로 했다\"며 "
        "\"적절한 보호자가 나타날 때까지 푸딩이를 보호할 것\"이라고 했다. 배 씨는 팔순을 앞두고 "
        "가족과 함께 태국으로 첫 해외여행을 떠났다가 비극적인 사고를 당했다. 배 씨는 영광군 "
        "군남면의 한 마을에서 아내 임모(64) 씨와 큰딸, 외손녀 정모(6) 양과 살았다. 광주에서 일하는 "
        "큰사위는 주말마다 집을 찾았다고 한다. 이들 5명과 작은 딸과 세 자녀까지 모두 9명이 함께 "
        "여행을 갔다가 영영 돌아오지 못했다. 배 씨의 작은 사위는 졸지에 장인·장모, 아내와 세 "
        "자녀를 잃었다. 31일 오후까지 9명 중 신원 확인이 되지 않은 이들은 3명이다. 푸딩이의 "
        "친구였던 정 양과 배씨의 작은딸, 작은딸의 막내아들의 신원이 확인되지 않아 장례를 치르지 "
        "못하고 있다."
    )

    claims, usage = extract_claims_from_article(PUDDING_ARTICLE)
    print(f"추출 주장: {len(claims)}개 / 토큰 사용량: {usage}\n")
    for c in claims:
        print(f"- {c['claim_text']} ({c.get('value')}{c.get('unit', '')})")
        print(f"    근거: {c['evidence_quote']}")
