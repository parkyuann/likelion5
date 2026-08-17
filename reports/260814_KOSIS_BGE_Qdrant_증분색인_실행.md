# KOSIS BGE-M3 + Qdrant 증분 색인 실행 안내

실행 파일은 `src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py`이다. 이 파일 하나가 다음을 함께 처리한다.

- BGE-M3 임베딩: 새 표, 텍스트가 바뀐 표, 과거 벡터가 누락된 표만 GPU에서 새로 생성한다.
- 벡터 재사용: `table_key`와 `title`/`meta`/`item` 텍스트의 SHA-256 해시가 모두 같은 경우에만 이전 BGE 벡터를 재사용한다. 따라서 카탈로그 행 순서가 바뀌어도 잘못된 벡터가 붙지 않는다.
- Qdrant 최신화: 새/변경 point와 vector만 upsert하고, 변경된 payload만 갱신한다. `--delete-stale`를 주면 최신 카탈로그에 사라진 표도 삭제한다.
- 다음 업데이트용 상태 생성: `--output-bundle`에 완전한 벡터 묶음과 `documents.jsonl`을 저장한다. 다음 실행 때 이 폴더를 그대로 `--reuse-bundle`으로 사용한다.

## 독립성 및 벡터 계약

이 스크립트는 `bge_m3_encode.py`나 기존 Qdrant 색인기를 import하거나 호출하지 않는 독립 실행형이다. 다만 기존 v4 벡터를 재사용할 때는 그 결과 파일 형식(`dense.npy`, `sparse.json`, `rows.json`)을 입력으로 읽는다.

BGE-M3 벡터는 유안님 원본 계약을 그대로 따른다.

- `title`: `tbl_name`
- `meta`: `doc_meta_text`
- `item`: `items[].itm_nm`을 원래 순서대로 중복 제거해 이어 붙인 텍스트

따라서 dense 3개(`title_dense`, `meta_dense`, `item_dense`)와 sparse 3개가 Qdrant에 저장된다. `doc_item_index`는 새 임베딩 입력으로 바꾸지 않으며, 원문 그대로 payload에 보관한다.

payload에는 `table_key`, `stat_id`, 기관, 분류 경로, 항목 ID·명칭, 차원축 ID·명칭, 차원값 ID·명칭·상위 ID, 주기·최신시점·단위, 표 메타데이터가 들어간다. 자주 필터링할 `stat_id`, `period_types`, `units`, `item_names`, `dim_axis_names`, `dim_value_names` 등에는 keyword payload index도 생성한다.

## GPU 컴퓨터 준비

GPU 컴퓨터로 다음을 복사한다.

- 최신 카탈로그: 예를 들어 `data/크롤링_v5/kosis_catalog_v5_260814.jsonl`
- 새 실행 파일: `src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py`
- 첫 마이그레이션에서 재사용할 v4 BGE 묶음: `src/bge_encoding/encoded/`
- 첫 마이그레이션에서 재사용할 문서 상태: `src/bge_encoding/documents.jsonl`

Python 가상환경을 만든 후 기본 의존성을 설치한다. GPU용 PyTorch는 해당 컴퓨터의 CUDA 버전에 맞는 명령으로 먼저 설치해야 한다. 이후에는 다음 패키지가 필요하다.

```powershell
python -m pip install -r requirements.txt
python -m pip install FlagEmbedding
```

이번 실행은 `--qdrant-url` 없이 `--db-path`로 만드는 **embedded 로컬 Qdrant DB**를 사용한다. Qdrant 서버나 Docker를 설치하거나 실행할 필요가 없다. DB는 지정한 폴더에 파일 형태로 저장되며, 검색 프로그램도 같은 폴더를 `QdrantClient(path=...)`로 열어 사용하면 된다.

## 0. GPU 없이 사전 확인

아래 명령은 최신 카탈로그와 이전 문서의 텍스트 해시만 비교한다. 임베딩, Qdrant 변경, 출력 파일 생성이 전혀 없다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --catalog "data/크롤링_v5/kosis_catalog_v5_260814.jsonl" `
  --reuse-bundle "src/bge_encoding/encoded" `
  --reuse-documents "src/bge_encoding/documents.jsonl" `
  --output-bundle "src\bge_encoding\bge_bundle_260814" `
  --dry-run
```

## 1. v4 BGE 벡터를 재사용해 최신 v5를 첫 색인

아래는 새 structured collection을 만드는 안전한 권장 명령이다. 이전 v4 벡터 가운데 텍스트가 동일한 것은 재사용하고, v5에만 있거나 바뀐 부분 및 기존 title 벡터가 빠진 부분만 GPU에서 굽는다.

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

`kosis_tables_structured`은 카탈로그 버전에 묶이지 않는 collection 이름이다. `src\bge_encoding\qdrant_structured`가 로컬 Qdrant DB 폴더이며, 검색 프로그램에서도 같은 경로를 `QdrantClient(path="src/bge_encoding/qdrant_structured")`로 열어 사용한다. 동시에 두 프로세스가 같은 embedded DB를 열지는 않는다.

처음부터 전수 색인이 필요하고 재사용할 BGE 묶음이 없다면 아래처럼 실행한다. 새 표가 전부 GPU에서 임베딩된다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --mode full `
  --catalog "data/크롤링_v5/kosis_catalog_v5_260814.jsonl" `
  --output-bundle "src\bge_encoding\bge_bundle_260814" `
  --db-path "src\bge_encoding\qdrant_structured" `
  --collection "kosis_tables_structured" `
  --recreate --delete-stale
```

## 2. 다음 카탈로그 날짜의 증분 업데이트

다음 카탈로그가 생기면 바로 전 성공 실행의 output bundle을 reuse bundle로 넘긴다. 새 output bundle은 다른 날짜 폴더여야 한다.

```powershell
python src/bge_encoding/kosis_bge_qdrant_updatable_indexer.py `
  --catalog "data/크롤링_v6/kosis_catalog_v6_YYYYMMDD.jsonl" `
  --reuse-bundle "src\bge_encoding\bge_bundle_260814" `
  --output-bundle "src\bge_encoding\bge_bundle_YYYYMMDD" `
  --db-path "src\bge_encoding\qdrant_structured" `
  --collection "kosis_tables_structured" `
  --delete-stale
```

두 번째 실행부터는 `--reuse-documents`가 필요 없다. 이전 output bundle 안의 `documents.jsonl`과 `manifest.json`이 안전한 비교 기준이 된다.

## 주의할 점

- `--output-bundle`은 덮어쓰지 않는다. 같은 위치를 다시 써야 할 때만 명시적으로 `--overwrite-output`을 사용한다.
- `--delete-stale`은 현재 카탈로그에 없는 point를 실제로 삭제한다. 전체 최신 카탈로그를 사용했을 때만 붙인다. `--limit`을 사용한 smoke test와는 함께 쓸 수 없다.
- `--recreate`는 해당 collection을 삭제하고 새로 만든다. 기존 collection을 증분 최신화하는 실행에는 붙이지 않는다.
- `item` 텍스트가 BGE 최대 길이를 넘으면 유안님 원본 정책처럼 청크로 자르고 첫 청크 벡터를 사용한다. 차원값과 전체 항목 정보는 payload에 빠짐없이 남으므로 structured filter에는 영향을 주지 않는다.
