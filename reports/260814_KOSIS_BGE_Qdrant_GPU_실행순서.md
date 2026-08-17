# KOSIS BGE-M3 + 로컬 Qdrant v5 색인: GPU 컴퓨터 실행 순서

## 이 문서의 목적

이 문서는 GPU가 있는 다른 컴퓨터에서 Claude/Codex가 그대로 따라 실행하기 위한 작업 지시서다.

실행할 파이썬 파일은 하나뿐이다.

```text
src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py
```

기존 `src/bge_encoding/bge_m3_encode.py`나 기존 Qdrant indexer를 다시 실행하지 않는다. 기존 v4 BGE 결과물은 새 indexer가 벡터를 재사용하기 위한 **입력 파일**일 뿐이다.

## 목표와 방식

- 최신 카탈로그: `data/크롤링_v5/kosis_catalog_v5_260814.jsonl` (약 28만 표)
- 기존 재사용 벡터: `src/bge_encoding/encoded/`
- 기존 비교 문서: `src/bge_encoding/documents.jsonl`
- 새 Qdrant collection: `kosis_tables_structured`
- Qdrant 방식: 서버/Docker가 아닌 **embedded 로컬 DB**
- 로컬 DB 저장 위치: `src\bge_encoding\qdrant_structured`
- 다음 업데이트용 완전한 BGE bundle: `src\bge_encoding\bge_bundle_260814`

새 indexer는 `table_key`와 각 필드의 텍스트 SHA-256 해시가 모두 같을 때만 이전 벡터를 재사용한다. 새 표, 내용 변경 표, 기존 벡터 누락 표는 BGE-M3로 새로 임베딩한다. 재사용할 벡터가 없으면 전수 임베딩도 가능하다.

`src/bge_encoding/encoded/`은 **시간 절약용 재사용 캐시**다. 이 폴더를 GPU 컴퓨터에 옮기지 못해도 색인의 정확도에는 문제가 없으며, `--mode full`로 최신 카탈로그 전체를 GPU에서 새로 임베딩하면 된다. 기존 `kosis_qdrant_v4/`, `kosis_qdrant_v5/` 로컬 DB는 어느 경우에도 옮기지 않는다.

벡터 필드는 유안님 BGE 계약을 그대로 유지한다.

- `title`: `tbl_name`
- `meta`: `doc_meta_text`
- `item`: `items[].itm_nm`을 원래 순서대로 중복 제거하여 이어 붙인 텍스트

Qdrant에는 `title`, `meta`, `item` 각각의 dense + sparse 벡터, 총 6개 named vector가 저장된다. `doc_item_index`는 임베딩 입력으로 바꾸지 않고 원문 그대로 payload에 보관한다.

---

## 0. 실행 전 규칙

1. **작업 루트는 `likelion5` 폴더다.** 아래 상대경로는 모두 이 폴더에서 실행한다.
2. 기존 `kosis_qdrant_v4`, `kosis_qdrant_v5`를 수정·삭제하지 않는다.
3. 이번 작업은 `src\bge_encoding\qdrant_structured`에 새 로컬 DB를 만든다.
4. `--recreate`와 `--overwrite-output`은 이번 본 색인 명령에 사용하지 않는다.
5. `--delete-stale`은 전체 최신 카탈로그를 쓸 때만 사용한다. `--limit` 테스트와 결합하지 않는다.
6. embedded 로컬 Qdrant DB는 동시에 두 프로세스가 같은 경로를 열면 안 된다. 색인 실행이 끝날 때까지 검색 프로그램을 실행하지 않는다.

---

## 1. 파일 위치 확인

PowerShell에서 GPU 컴퓨터의 저장소 루트로 이동한다. 이 작업의 실제 경로는 `C:\Users\이현서\Documents\likelion5`다.

```powershell
Set-Location "C:\Users\이현서\Documents\likelion5"

$requiredForReuse = @(
  "src\bge_encoding\kosis_bge_qdrant_updatable_indexer.py",
  "src\bge_encoding\documents.jsonl",
  "src\bge_encoding\encoded",
  "data\크롤링_v5\kosis_catalog_v5_260814.jsonl",
  "requirements.txt"
)

$requiredForReuse | ForEach-Object {
  "{0} : {1}" -f $_, (Test-Path -LiteralPath $_)
}
```

기존 v4 벡터를 재사용하는 경로라면 모두 `True`여야 한다. `encoded` 또는 `documents.jsonl`을 옮길 수 없다면 아래 **전수 색인 경로**를 선택한다. 이 경우에는 indexer, 최신 catalog, `requirements.txt` 세 파일만 있으면 된다.

### GPU 컴퓨터로 옮길 최소 파일 목록

현재 컴퓨터의 `likelion5`에서 다음만 GPU 컴퓨터의 **동일한 상대경로**로 복사하면 된다.

```text
likelion5/
├─ requirements.txt
├─ src/
│  ├─ kosis_bge_qdrant_updatable_indexer.py
│  └─ bge_encoding/
│     ├─ documents.jsonl
│     └─ encoded/                         # 폴더 전체: title/meta/item 모든 shard 파일
└─ data/
   └─ 크롤링_v5/
      └─ kosis_catalog_v5_260814.jsonl
```

함께 복사하면 좋은 문서:

```text
reports/260814_KOSIS_BGE_Qdrant_GPU_실행순서.md
```

이번 새 DB 생성에 필요하지 않아 옮기지 않아도 되는 항목:

- `kosis_qdrant_v4/`, `kosis_qdrant_v5/` (기존 embedded DB)
- `src/크롤링_v5/`의 크롤러·병합·카탈로그 생성 코드
- `src/bge_encoding/bge_m3_encode.py`, `bge_m3_search.py`, `queries.jsonl`
- 기존 v4/v5 보고서와 기타 프로젝트 파일

단, GPU 컴퓨터가 인터넷에 연결되지 않는다면 BGE-M3 모델 파일도 미리 옮겨야 한다. 인터넷이 연결되어 있으면 첫 실행 시 `BAAI/bge-m3` 모델을 자동으로 내려받으므로 따로 복사할 필요가 없다.

### 2GB 전송 제한이 있을 때의 선택

1. **권장: `encoded/` 폴더를 여러 파일로 나누어 전송한다.** 현재 `src/bge_encoding/encoded/`는 총 3.27GB지만 166개 shard 파일로 나뉘어 있고, 가장 큰 파일도 약 78MB다. 따라서 2GB 제한이 있어도 폴더/파일 단위 전송은 가능하다. `C:\Users\minec\Downloads\encoded.zip`은 2.82GB이므로 이 ZIP 자체는 전송하지 않는다.
2. **전송이 불가능하면: 전수 색인한다.** `encoded/`와 `documents.jsonl`을 아예 복사하지 않고 아래의 `--mode full` 명령을 사용한다. 약 28만 표의 `title`, `meta`, `item` 벡터를 모두 새로 굽기 때문에 시간이 더 오래 걸리지만 결과 DB의 내용과 payload schema는 동일하다.

### 현재 컴퓨터 → GPU 컴퓨터: 권장 전송 방법

외장 디스크나 네트워크 공유 폴더가 있다면, 현재 컴퓨터에서 아래 구조로 복사한다. `<TRANSFER_DRIVE>`를 실제 외장 디스크 문자나 공유 폴더로 바꾼다.

```powershell
$source = "C:\Users\minec\Documents\likelion5"
$transfer = "<TRANSFER_DRIVE>\KOSIS_GPU_TRANSFER"

New-Item -ItemType Directory -Force -Path "$transfer\src\bge_encoding" | Out-Null
New-Item -ItemType Directory -Force -Path "$transfer\data\크롤링_v5" | Out-Null
New-Item -ItemType Directory -Force -Path "$transfer\reports" | Out-Null

Copy-Item "$source\requirements.txt" "$transfer\requirements.txt"
Copy-Item "$source\src\bge_encoding\kosis_bge_qdrant_updatable_indexer.py" "$transfer\src\bge_encoding\kosis_bge_qdrant_updatable_indexer.py"
Copy-Item "$source\src\bge_encoding\documents.jsonl" "$transfer\src\bge_encoding\documents.jsonl"
Copy-Item "$source\data\크롤링_v5\kosis_catalog_v5_260814.jsonl" "$transfer\data\크롤링_v5\kosis_catalog_v5_260814.jsonl"
Copy-Item "$source\reports\260814_KOSIS_BGE_Qdrant_GPU_실행순서.md" "$transfer\reports\260814_KOSIS_BGE_Qdrant_GPU_실행순서.md"

robocopy "$source\src\bge_encoding\encoded" "$transfer\src\bge_encoding\encoded" /E /J /Z /R:2 /W:2
```

`robocopy`의 마지막 줄은 166개 shard를 원래 폴더 구조대로 복사한다. 중간에 끊겨도 같은 명령을 다시 실행하면 이어서 복사한다.

GPU 컴퓨터에서는 `KOSIS_GPU_TRANSFER` 폴더의 내용을 `C:\Users\이현서\Documents\likelion5`에 복사한다. 최종 구조는 아래처럼 되어야 한다.

```text
C:\Users\이현서\Documents\likelion5\
├─ requirements.txt
├─ src\bge_encoding\kosis_bge_qdrant_updatable_indexer.py
├─ src\bge_encoding\documents.jsonl
├─ src\bge_encoding\encoded\title\shard_0000.dense.npy
├─ src\bge_encoding\encoded\meta\shard_0000.dense.npy
├─ src\bge_encoding\encoded\item\shard_0000.dense.npy
└─ data\크롤링_v5\kosis_catalog_v5_260814.jsonl
```

외장 디스크/공유 폴더가 없고 파일 전송 서비스만 사용할 수 있다면, `encoded/title`, `encoded/meta`, `encoded/item` 세 폴더를 각각 별도 전송한다. 각 shard 파일은 2GB보다 훨씬 작다. 전송 서비스가 폴더 업로드를 허용하지 않으면 각 폴더를 1GB 이하 분할 압축한 뒤, GPU 컴퓨터에서 원래의 `src/bge_encoding/encoded/` 아래에 풀어 둔다.

### Google Drive에서 `encoded.zip`을 직접 내려받는 방법

Google Drive 링크를 GPU 컴퓨터에서도 열 수 있다면 이 방법이 가장 간단하다. 원본 `encoded.zip`은 약 2.82GB이므로 카카오톡 전송 제한과 무관하다.

1. GPU 컴퓨터에서 Google Drive 링크로 `encoded.zip`을 내려받는다.
2. 저장소 루트 `C:\Users\이현서\Documents\likelion5`에서 다음을 실행한다.

```powershell
Set-Location "C:\Users\이현서\Documents\likelion5"
New-Item -ItemType Directory -Force -Path "src\bge_encoding" | Out-Null
Expand-Archive -LiteralPath "<다운로드한 encoded.zip의 전체 경로>" -DestinationPath "src\bge_encoding"
```

이 ZIP의 내부 최상위 폴더 이름이 이미 `encoded`이므로, destination은 반드시 `src\bge_encoding`이다. destination을 `src\bge_encoding\encoded`로 주면 `encoded\encoded\...`처럼 한 단계가 중복된다.

3. 압축 해제 후 다음 경로가 모두 `True`인지 확인한다.

```powershell
Test-Path "src\bge_encoding\encoded\title\shard_0000.dense.npy"
Test-Path "src\bge_encoding\encoded\meta\shard_0000.dense.npy"
Test-Path "src\bge_encoding\encoded\item\shard_0000.dense.npy"
```

`encoded.zip` 다운로드 파일은 압축 해제가 끝난 뒤에도 삭제하지 말고, 본 색인이 성공할 때까지 보관한다. GPU 컴퓨터에는 ZIP(2.82GB), 압축 해제 폴더(약 3.27GB), 새 output bundle, 로컬 Qdrant DB를 위한 충분한 여유 공간이 필요하다.

### `documents.jsonl`도 함께 준비

벡터 재사용에는 아래 파일도 반드시 필요하다.

```text
src\bge_encoding\documents.jsonl
```

이 파일은 기존 v4 vector와 원래 `title`/`meta`/`item` 텍스트를 안전하게 대조하는 기준이다. 없으면 indexer는 기존 vector를 재사용하지 않고 전수 임베딩으로 전환한다.

`documents.jsonl`은 작은 `encoding_contract.zip` 안에 들어 있으므로, GPU 컴퓨터에 그 ZIP을 함께 옮긴 뒤 다음처럼 푼다.

```powershell
Set-Location "C:\Users\이현서\Documents\likelion5"
Expand-Archive -LiteralPath "<encoding_contract.zip의 전체 경로>" -DestinationPath "src\bge_encoding"
Test-Path "src\bge_encoding\documents.jsonl"
```

마지막 결과가 `True`여야 한다. `encoding_contract.zip`에는 `encoded/`는 없으므로, 위의 큰 `encoded.zip` 압축 해제와 두 작업을 모두 해야 v4 벡터 재사용 경로가 완성된다.

---

## 2. GPU Python 환경 준비

Python 3.11 가상환경을 권장한다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

해당 GPU·NVIDIA 드라이버·CUDA와 맞는 GPU용 PyTorch를 먼저 설치한다. PyTorch 공식 설치 선택기에서 운영체제, Pip, CUDA 환경에 맞는 명령을 사용한다.

```text
https://pytorch.org/get-started/locally/
```

그 다음 프로젝트 의존성과 BGE를 설치한다.

```powershell
python -m pip install -r requirements.txt
python -m pip install -U FlagEmbedding
```

GPU가 실제로 잡히는지 확인한다.

```powershell
python -c "import torch; print('torch=', torch.__version__); print('cuda=', torch.cuda.is_available()); print('device=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NOT FOUND')"
```

`cuda=True`와 GPU 이름이 나와야 한다. 그렇지 않으면 본 색인을 시작하지 말고 PyTorch/CUDA 설치 문제를 해결한다.

---

## 3. 전체 변경량 dry-run

아래 명령은 카탈로그와 기존 BGE 문서의 텍스트 해시만 비교한다. GPU 임베딩, Qdrant DB 생성·변경, output bundle 파일 쓰기를 하지 않는다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --catalog "data/크롤링_v5/kosis_catalog_v5_260814.jsonl" `
  --reuse-bundle "src/bge_encoding/encoded" `
  --reuse-documents "src/bge_encoding/documents.jsonl" `
  --output-bundle "src\bge_encoding\bge_bundle_260814" `
  --dry-run
```

출력의 다음 항목을 기록한다.

- `new=`: v4에 없던 table_key 수
- `title_changed=`, `meta_changed=`, `item_changed=`: 텍스트가 바뀌어 재임베딩될 가능성이 있는 수
- `payload_changed_or_unknown=`: payload를 새로 쓸 수

dry-run이 오류 없이 끝나면 다음 단계로 진행한다.

### 전수 색인 경로의 dry-run

기존 BGE cache를 옮기지 않은 경우에는 아래 명령으로 실행한다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --mode full `
  --catalog "data/크롤링_v5/kosis_catalog_v5_260814.jsonl" `
  --output-bundle "src\bge_encoding\bge_bundle_260814" `
  --dry-run
```

---

## 4. 100건 end-to-end smoke test

처음 GPU 환경에서만 실행한다. 실제 BGE 모델과 로컬 Qdrant API 호환성을 검증하기 위한 작은 별도 DB/벡터 bundle이다. 본 DB와 본 output bundle을 사용하지 않는다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --catalog "data/크롤링_v5/kosis_catalog_v5_260814.jsonl" `
  --reuse-bundle "src/bge_encoding/encoded" `
  --reuse-documents "src/bge_encoding/documents.jsonl" `
  --output-bundle "src\bge_encoding\smoke_bge_bundle_260814" `
  --db-path "src\bge_encoding\qdrant_smoke" `
  --collection "kosis_tables_smoke" `
  --limit 100
```

아래 확인 명령이 `kosis_tables_smoke`의 point 수를 출력해야 한다.

```powershell
python -c "from qdrant_client import QdrantClient; c=QdrantClient(path='src/bge_encoding/qdrant_smoke'); print(c.count('kosis_tables_smoke', exact=True).count); c.close()"
```

전수 색인 경로(`encoded/`를 옮기지 않은 경우)에서는 smoke test 명령에서 `--reuse-bundle`, `--reuse-documents` 두 줄을 빼고 `--mode full`을 추가한다.

오류가 있으면 본 색인을 시작하지 말고 오류 전문, Python 버전, `torch.cuda.is_available()` 결과를 보고한다. smoke DB와 bundle은 검증 후에도 자동 삭제하지 않는다.

---

## 5. 실제 v5 색인 실행

smoke test가 성공했을 때만 실행한다. 이 명령 하나가 벡터 재사용, 새 임베딩, 로컬 Qdrant upsert, payload index 생성, 다음 업데이트용 bundle 생성을 모두 수행한다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --catalog "data/크롤링_v5/kosis_catalog_v5_260814.jsonl" `
  --reuse-bundle "src/bge_encoding/encoded" `
  --reuse-documents "src/bge_encoding/documents.jsonl" `
  --output-bundle "src\bge_encoding\bge_bundle_260814" `
  --db-path "src\bge_encoding\qdrant_structured" `
  --collection "kosis_tables_structured" `
  --delete-stale
```

실행 중에는 다음 로그가 순서대로 나와야 한다.

1. `[LOAD] catalog`, `[LOAD] reuse documents`, `[DIFF]`
2. `[FIELD] meta`, `[FIELD] title`, `[FIELD] item`
3. `[PAYLOAD] updated`
4. `[INDEX] ensured keyword indexes`
5. 마지막 JSON의 `"status": "complete"`

모델 첫 로딩 시 BGE-M3 모델 파일을 내려받을 수 있으므로 인터넷 연결과 수 GB 이상의 디스크 여유 공간이 필요하다. output bundle과 Qdrant DB 모두 수 GB 이상을 사용할 수 있다.

### 기존 v4 BGE cache를 옮기지 못한 경우: 전수 색인 명령

`src/bge_encoding/encoded/`와 `src/bge_encoding/documents.jsonl`을 GPU 컴퓨터에 복사하지 않았다면, 위의 재사용 명령 대신 아래 명령을 **한 번만** 실행한다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --mode full `
  --catalog "data/크롤링_v5/kosis_catalog_v5_260814.jsonl" `
  --output-bundle "src\bge_encoding\bge_bundle_260814" `
  --db-path "src\bge_encoding\qdrant_structured" `
  --collection "kosis_tables_structured" `
  --delete-stale
```

이 경로에서는 `--reuse-bundle`, `--reuse-documents` 옵션을 넣지 않는다.

---

## 6. 완료 검증

색인 프로세스가 끝난 뒤, 다음 두 명령을 실행한다.

```powershell
Get-Content "src\bge_encoding\bge_bundle_260814\manifest.json" -Encoding UTF8
```

```powershell
python -c "from qdrant_client import QdrantClient; c=QdrantClient(path='src/bge_encoding/qdrant_structured'); print(c.get_collection('kosis_tables_structured')); print('points=', c.count('kosis_tables_structured', exact=True).count); c.close()"
```

확인 기준:

- `manifest.json`의 `status`가 `complete`
- `catalog_records`가 최신 카탈로그의 표 수와 일치
- `points`가 `catalog_records`와 일치
- `field_counts`에 `title`, `meta`, `item`이 모두 존재
- `payload_updates`가 출력됨

오류가 없이 위 기준을 충족하면 색인 완료다.

---

## 7. 다음 카탈로그 업데이트

다음 날짜 또는 다음 catalog 버전의 최신 카탈로그가 생기면, 이번에 만들어진 bundle을 재사용한다. 기존 v4 `src/bge_encoding/documents.jsonl`은 더 이상 필요 없다.

### 7-1. 먼저 변경량만 확인

`<NEW_CATALOG>`만 실제 새 카탈로그 경로로 바꾼다. 예: `data/크롤링_v6/kosis_catalog_v6_YYYYMMDD.jsonl`.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --catalog "<NEW_CATALOG>" `
  --reuse-bundle "src\bge_encoding\bge_bundle_260814" `
  --output-bundle "src\bge_encoding\bge_bundle_YYYYMMDD" `
  --dry-run
```

이 명령은 새 표·바뀐 텍스트·바뀐 payload 규모만 보여 주며, GPU와 Qdrant DB는 수정하지 않는다.

### 7-2. 실제 증분 업데이트

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --catalog "<NEW_CATALOG>" `
  --reuse-bundle "src\bge_encoding\bge_bundle_260814" `
  --output-bundle "src\bge_encoding\bge_bundle_YYYYMMDD" `
  --db-path "src\bge_encoding\qdrant_structured" `
  --collection "kosis_tables_structured" `
  --delete-stale
```

실행 결과는 다음처럼 해석한다.

- 이전과 동일한 `table_key`와 텍스트: BGE 재사용, Qdrant vector 변경 없음
- 새 표·텍스트 변경 표·과거 벡터 누락 표: 해당 필드만 GPU로 재임베딩 후 upsert
- 항목·차원·주기·단위·분류 등 payload만 변경: 임베딩 없이 payload만 갱신
- 새 카탈로그에 사라진 표: `--delete-stale`로 Qdrant point 삭제

### 7-3. 다음 업데이트 기준 교체

성공 기준은 `src\bge_encoding\bge_bundle_YYYYMMDD\manifest.json`의 `status`가 `complete`인 것이다. 성공한 경우에만 다음 실행에서 아래처럼 새 bundle을 `--reuse-bundle`로 사용한다.

```text
이전: src\bge_encoding\bge_bundle_260814
다음 기준: src\bge_encoding\bge_bundle_YYYYMMDD
```

새 output bundle은 항상 새 날짜 폴더로 지정한다. 이전 bundle을 덮어쓰거나 자동 삭제하지 않는다.
