# KOSIS API 기반 RAG 뉴스 수치 사실검증 시스템

> 뉴스 기사 속 수치 주장을 구조화하고, KOSIS 공식 통계와 대조해 근거 중심으로
> 검증하는 AI 시스템

## 프로젝트 개요

### 과제명

**KOSIS API 기반 RAG 뉴스 수치 사실검증 시스템**

### 해결하려는 문제

통계 수치가 포함된 뉴스는 출처·지표·지역·시점·단위를 함께 맞춰야 검증할 수
있습니다. 단순 키워드 검색이나 LLM의 자유 생성만으로는 어떤 공식 통계표의 어느
값을 근거로 삼았는지 보장하기 어렵습니다.

### 핵심 접근

1. 기사 본문에서 수치 주장과 비교 표현을 추출·구조화합니다.
2. Dense/Sparse 검색과 RRF로 관련 KOSIS 통계표 후보를 찾습니다.
3. release-pinned PostgreSQL 메타데이터로 항목·차원·기간·단위를 하나의
   좌표로 결박하고, live stage가 활성화된 경우에만 KOSIS Param API의 공식 셀을
   조회합니다.
4. LLM이 아닌 결정론적 코드로 기사 주장과 공식 수치를 비교하고, 사용한 근거를
   제한해 설명을 만듭니다.
5. 유일한 근거 좌표를 만들 수 없으면 추측으로 판정하지 않고 clarification 또는
   `UNVERIFIABLE`로 종료합니다.

```text
뉴스 기사 → 수치 주장 구조화 → KOSIS 통계표 검색
          → release-pinned 메타데이터 binding → (활성화 시) KOSIS 공식 셀 조회
          → 결정론적 비교 → 근거 기반 결과
```

### 기술 스택

| 영역 | 기술 |
| --- | --- |
| Backend | Python, FastAPI |
| LLM 기반 구조화 | HCX / HCX-DASH |
| 검색·RAG | BGE-M3, Qdrant, BM25, RRF |
| 공식 근거 | release-pinned PostgreSQL metadata, KOSIS Param API cell lookup |
| Frontend | React, Vite |
| 운영 구성 | Docker, Nginx, Redis, OpenSearch |
| 실험 범위 | BGE reranker (현재 release-bound 경로에서는 지원하지 않음) |

### 핵심 성과

- 검색 결과를 곧바로 답으로 사용하지 않고, **공식 통계의 유일한 좌표가 확인된
  경우에만** 수치 비교가 진행되도록 설계했습니다.
- 검색·LLM 역할과 판정 역할을 분리했습니다. LLM은 주장 구조화에 사용하고,
  최종 cell 정렬·계산·판정은 재현 가능한 코드가 담당합니다.
- 근거 부족·후보 충돌·외부 조회 실패를 명시적으로 드러내는 fail-closed 경계를
  두어, 그럴듯하지만 근거 없는 판정을 줄이는 것을 목표로 했습니다.

## 구현 및 공개 범위

이 저장소는 프론트엔드와 FastAPI, 그리고 패키징된 런타임 source를 제공합니다.
KOSIS·HCX 자격증명, DB·검색 인덱스, encoder 모델 서비스와 모델 asset, TLS
인증서와 키는 별도로 준비해야 합니다. 기본 예시 환경에서는 live stage, URL·이미지
입력이 opt-in 상태이며, reranker는 현재 release-bound 경로에서 지원되지 않습니다.
명시적 자연어 `query`와 URL이 아닌 `auto` 입력은 현재 API에서 지원하지 않아
503으로 닫힙니다. URL을 포함한 `auto` 입력은 별도의 URL 경로 gate를 따릅니다.

이 README는 프로젝트 포트폴리오 개요입니다. 성능 수치, live E2E 성공, 운영
승격을 주장하지 않으며, 실제 배포에서는 외부 의존성의 별도 검증이 필요합니다.
