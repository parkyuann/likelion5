# 주장 추출·구조화 배포 번들 v1

이 폴더는 기사 원문에서 주장 후보를 만들고 구조화하여 검색 질의를 내보내는 앞단을
독립 실행할 수 있게 모은 배포용 코드이다. 상위 src 패키지나 기존 실험 실행기에
의존하지 않는다.

## 포함 범위

- L1: 값·시점·차원 후보와 원문 span 생성
- L2: HCX-007 구조화 레이아웃 호출 및 응답 검증
- L3: 값별 indicator·period·dimension 역할 부여
- L4: 주장 6필드(indicator, period, measurement_type, population, item, dimension) 결정론 정규화
- L5: 라우팅 및 3개 검색 필드(indicator, measurement_type, period) 기반 질의 변형 생성

포함 모듈 중 text_primitives.py는 기존 레거시 추출기에서 L1이 실제로 사용하는
문장 분할·값/단위 규칙만 분리한 것이다. 레거시 문장 단위 claim 행 분리기는 이 앞단의
실행 경로에 사용되지 않으므로 포함하지 않았다.

## 제외 범위

이 번들은 검색·판정 전체 서비스가 아니다. 아래 후단은 별도 배포 대상이다.

- KOSIS catalog·BM25/dense/vector 검색과 후보 union
- R4 axis/cell 정렬, KOSIS API 조회, 값 비교와 최종 verdict
- 평가 gold, 라벨링 UI, 실험·채점 스크립트

## 실행

Python 3.11 이상 환경에서 다음을 실행한다.

    cd src/develop/v1
    python -m pip install -r requirements.txt

HCX 키는 운영 환경 변수로 전달한다. 키를 코드나 입력 파일에 넣지 않는다.

    $env:HCX_API_KEY = "..."
    python run_l2_segmentation.py --articles articles.jsonl --output l2.jsonl --manifest l2_manifest.json
    python run_layer_stack.py --articles articles.jsonl --l2 l2.jsonl --output claims.jsonl --summary summary.json

articles.jsonl의 각 행은 최소 article_idx, article_text를 가져야 하며, L4의 상대 시점
절대화에는 date(기사 발행일)를 함께 제공한다. title은 L2 프롬프트에 선택적으로 전달된다.

.env를 쓰는 환경이면 현재 작업 폴더 또는 이 폴더의 .env에서 HCX_API_KEY
(또는 NCP_CLOVASTUDIO_API_KEY)를 읽는다. 운영에서는 환경 변수 주입을 우선한다.

## 독립성 계약

- 이 폴더 내부의 Python 파일만 import한다(표준 라이브러리와 requirements 제외).
- run_l2_segmentation.py --help, run_layer_stack.py --help는 이 폴더에서 직접 실행된다.
- tests/test_v1_deployment_bundle.py가 현재 L1/L4와의 동작 동등성 및 단독 runner import를 검증한다.
