# `src` 모듈 구성

`src`는 실행 가능한 파이썬 모듈을 import하는 테스트와 CLI가 함께 사용하는 디렉터리입니다. 따라서 현재 활성 모듈은 import 경로 안정성을 위해 루트에 유지합니다.

## 구성 원칙

- **핵심 파이프라인**: `claim_*`, `retrieval_*`, `kosis_*`, `source_scope_*`, `news_preprocessor.py`
- **평가·골드셋**: `evaluate_*`, `create_*evaluation*`, `build_*labeling*`, `collect_*gold*`, `analyze_*gold*`
- **실험·비교 도구**: `hcx_*`, `hybrid_*`, `ncp_*`, `run_*`, `select_*`, `report_*`, `prepare_*`
- **검증·산출물 도구**: `validate_*`, `build_kosis_v4_*`, `convert_*`, `summarize_*`

구형 스크립트는 이름만 보고 이동하지 않고, 저장소 전체 참조(`tests`, `configs`, `docs`, 실행 문서)를 확인한 뒤 `archive/YYYY-MM-DD/src_구형/`으로 이동합니다. 이동 후에는 참조 갱신과 전체 테스트를 수행합니다.

## 이번 정리 결과 (20260724)

현재 루트의 후보 스크립트들은 테스트·설정·실행 문서 또는 최근 산출물과 연결되어 있어 안전하게 이동할 수 있는 무참조 파일이 확인되지 않았습니다. 따라서 이번에는 import 경로를 깨뜨리는 이동을 하지 않고, 위 분류와 보존 기준을 명시했습니다. 명확히 폐기할 실험이 확정되면 별도 승인된 작업으로 아카이브합니다.
