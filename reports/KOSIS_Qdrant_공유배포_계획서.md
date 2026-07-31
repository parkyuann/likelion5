# KOSIS 벡터DB Qdrant 공유 배포 계획서

작성일: 2026-07-30
대상: KOSIS 통계표 메타데이터 카탈로그(v2, 515건)를 Qdrant 벡터DB로 색인 → Docker 컨테이너화 → 같은 Wi-Fi(LAN) 팀원이 접근

---

## 0. 요약 (TL;DR)

- **무엇을**: `data/kosis_catalog_v2.jsonl`(515개 통계표 메타데이터)를 Qdrant 컬렉션 `kosis_tables_v2`로 색인.
- **어떻게**: Qdrant 공식 이미지를 **서버 모드 Docker 컨테이너**로 띄우고(임베디드 파일 모드 아님), 포트를 열어 LAN 팀원이 `http://<호스트IP>:6333`으로 접속.
- **임베딩 비용**: **API 호출 0건.** 515개 doc_meta 벡터(1024차원)가 `data/kosis_embedding_cache.jsonl`에 100% 캐시되어 있어 오프라인 재사용.
- **선결 조건(사용자 조치 필요)**: **Docker Desktop 미설치** 상태 → 설치 후 실행 가능.
- **포트 정정**: 기존에 방화벽에서 연 **8000은 Qdrant 포트가 아님.** Qdrant는 **6333(REST)/6334(gRPC)**. 6333을 열어야 함.

---

## 1. 현황 분석

### 1.1 데이터
| 항목 | 값 |
|---|---|
| 카탈로그 파일 | `data/kosis_catalog_v2.jsonl` |
| 레코드 수 | 515건 (모두 `table_key` 고유) |
| 핵심 필드 | `table_key`, `org_id/org_name`, `tbl_id/tbl_name`, `category_paths`, `doc_meta_text`, `dimensions`, `items`, `period_types`, `latest_period` |
| 임베딩 대상 | `doc_meta_text` (표명 + 분류경로 + 차원명이 결합된 가장 풍부한 필드) |

### 1.2 임베딩 캐시 (핵심 자산)
- 파일: `data/kosis_embedding_cache.jsonl` (1,107개 벡터)
- 키 형식: `{table_key}|{md5(doc_meta_text)[:12]}`, table_key당 **doc_meta 벡터 1개**
- 차원: **1024** (Clova Studio embedding v2)
- **카탈로그 v2 515건 doc_meta 벡터 커버리지: 515/515 (100%)** → 색인 시 임베딩 API 호출 불필요

### 1.3 기존 코드
- `src/kosis_v2_indexer.py`: **임베디드(파일) 모드** Qdrant(`QdrantClient(path=...)`) 사용 → 단일 프로세스 전용, 네트워크 공유 불가. 또한 sha256 캐시 키를 써서 위 md5 캐시와 호환되지 않음(색인 시 API 재호출 발생). → **공유용으로는 부적합.**
- 본 계획은 **서버 모드 전용 신규 스크립트**를 추가하여 기존 자산은 건드리지 않음.

### 1.4 환경
- OS: Windows 11, 현재 Wi-Fi `likelion_11F`(Public), 호스트 IPv4 `172.31.98.127` / 마스크 `255.255.254.0`(/23).
- **Docker: 미설치** (PowerShell·bash 모두 `docker` 미인식). WSL2 vEthernet 어댑터는 존재하나 Docker Desktop은 없음.

---

## 2. 아키텍처 결정

### 2.1 임베디드 모드 ❌ → 서버 모드 ✅
`QdrantClient(path=...)` 임베디드 모드는 storage.sqlite 파일을 한 프로세스가 잠그는 구조라 여러 기기가 동시에 붙을 수 없음. 팀 공유에는 **Qdrant 서버(REST/gRPC) 컨테이너**가 정답.

### 2.2 벡터 구성: 단일 `doc_meta_vector`
- 캐시에 `doc_meta` 벡터만 존재(tbl_name 벡터 없음). 단일 벡터로 색인하면 **API 0건·완전 오프라인·재현성 100%**.
- `doc_meta_text`가 표명·분류·차원을 모두 포함하므로 카탈로그 검색 단일 벡터로 충분.
- (선택) 향후 `tbl_name_vector`를 추가하려면 515건 tbl_name을 API로 임베딩해야 함 → 본 배포 범위에서는 제외, 확장 항목으로 문서화.

### 2.3 포트 정책
| 포트 | 용도 | 개방 |
|---|---|---|
| 6333 | REST API + 웹 대시보드(`/dashboard`) | **개방(LAN)** |
| 6334 | gRPC | 선택(파이썬 클라이언트 성능용) |
| ~~8000~~ | Qdrant와 무관 | 기존 임시 규칙 **삭제 권장** |

### 2.4 접근·보안
- LAN 공개는 곧 **해당 Wi-Fi의 누구나 접근 가능**을 의미. Qdrant 기본은 인증 없음 → 조회·삭제까지 열림.
- 본 배포는 **Qdrant API Key 인증을 기본 활성화**(`QDRANT__SERVICE__API_KEY`)하여 최소한의 접근 통제 적용.
- 방화벽 규칙은 `-RemoteAddress LocalSubnet`으로 서브넷 한정.
- 데모 종료 후 방화벽 규칙 삭제 및 컨테이너 중지 절차 포함.

---

## 3. 산출물 (이번에 생성)

| 경로 | 내용 |
|---|---|
| `deploy/docker-compose.yml` | Qdrant 서버, 볼륨 영속화, 6333/6334 매핑, `QDRANT__SERVICE__API_KEY` 인증 |
| `deploy/.env.example` | `QDRANT_API_KEY` 샘플(→ `deploy/.env` 로 복사) |
| `deploy/README.md` | 실행·방화벽·팀원 접속·보안·정리 절차 |
| `src/kosis_qdrant_server_indexer.py` | 서버/임베디드 양쪽 지원 색인기(캐시 재사용, API 0건) |
| `src/kosis_qdrant_search.py` | 조회/헬스체크·검색 스모크테스트 |

---

## 4. 실행 절차

### STEP 0 — 선결: Docker Desktop 설치 (사용자 조치)
1. https://www.docker.com/products/docker-desktop 에서 Windows용 설치.
2. WSL2 백엔드 활성화(기본), 재부팅 후 `docker --version`으로 확인.

### STEP 1 — Qdrant 서버 컨테이너 기동
```bash
cd deploy
# .env 생성: QDRANT_API_KEY=<임의의 강한 문자열>
docker compose up -d
docker compose ps
```
- 로컬 확인: `http://localhost:6333/dashboard`

### STEP 2 — 카탈로그 색인 (API 0건, 캐시 재사용)
```bash
# 프로젝트 루트에서
./venv/Scripts/python.exe src/kosis_qdrant_server_indexer.py \
  --server-url http://localhost:6333 --api-key <QDRANT_API_KEY> --recreate
```
- 기대: `points=515`, `dimension=1024`, `api_calls=0`

### STEP 3 — 방화벽 (8000 → 6333 정정)
```powershell
# 기존 임시 규칙 제거
Remove-NetFirewallRule -DisplayName "temp-test-8000"
# Qdrant REST 포트 개방(서브넷 한정)
New-NetFirewallRule -DisplayName "qdrant-6333" -Direction Inbound -Protocol TCP -LocalPort 6333 -Action Allow -Profile Any -RemoteAddress LocalSubnet
```

### STEP 4 — 로컬/자기 IP 접속 순차 확인
```bash
./venv/Scripts/python.exe src/kosis_qdrant_search.py --server-url http://localhost:6333 --api-key <KEY> --health
./venv/Scripts/python.exe src/kosis_qdrant_search.py --server-url http://172.31.98.127:6333 --api-key <KEY> --health
```

### STEP 5 — 팀원 접속
팀원 노트북(같은 Wi-Fi)에서:
```python
from qdrant_client import QdrantClient
client = QdrantClient(url="http://172.31.98.127:6333", api_key="<KEY>")
print(client.count("kosis_tables_v2", exact=True))
```
- 대시보드: `http://172.31.98.127:6333/dashboard`

---

## 5. 검증 계획

| 단계 | 방법 | 통과 기준 |
|---|---|---|
| 데이터 파이프라인 | Docker 없이 로컬 임베디드 모드로 515건 색인 | count=515, dim=1024, api_calls=0 |
| 검색 정합성 | 임의 표의 저장 벡터로 자기 검색 | top-1이 자기 자신 |
| 서버 기동 | `docker compose up` 후 `/healthz`, `/dashboard` | 200/렌더 |
| LAN 접속 | 팀원 기기에서 count 조회 | 515 반환 |

> 본 세션에서는 STEP까지 중 **데이터 파이프라인·검색 정합성(Docker 불필요 부분)**을 로컬 임베디드 모드로 실제 검증. 서버 기동·LAN 접속은 Docker 설치 후 위 명령으로 수행.

---

## 6. 리스크 및 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| Docker 미설치 | 서버·포트·팀접속 불가 | STEP 0 선결(사용자) |
| AP Isolation(클라이언트 격리) | 같은 Wi-Fi여도 상호 접속 차단 | 개인 핫스팟으로 원인 격리 테스트, 안 되면 유선/핫스팟 대안 |
| 인증 없는 공개 | 임의 조회·삭제 | API Key 기본 활성 + LocalSubnet 한정 |
| 8000 임시 규칙 방치 | 불필요한 노출 | STEP 3에서 삭제 |
| 질의 임베딩 API 의존 | 팀원이 신규 질의 검색 시 Clova 키 필요 | 저장 벡터 기반 검색은 오프라인 가능함을 문서화 |
| WSL2 포트 포워딩 | 컨테이너가 VM 내부에 갇힘 | Docker Desktop은 자동 프록시 제공, `netstat`으로 `0.0.0.0:6333` 확인 |

---

## 7. 정리(테스트 종료 후)
```bash
docker compose down            # 컨테이너 중지(볼륨 데이터는 유지)
docker compose down -v         # 볼륨까지 삭제(완전 초기화)
```
```powershell
Remove-NetFirewallRule -DisplayName "qdrant-6333"
```
