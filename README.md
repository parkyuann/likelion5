# likelion5
AI 기반 뉴스 사실검증 시스템
# KOSIS API 데이터 탐색

`.env`에 있는 `KOSIS_API_KEY`를 사용해 KOSIS 통계목록 API의 목록과 통계표를 확인합니다.

```powershell
python .\kosis_explorer.py
```

출력된 `[목록]`의 `목록ID`를 다음 조회의 `--parent`에 넣으면 하위 단계로 이동할 수 있습니다.

```powershell
python .\kosis_explorer.py --parent <목록ID>
python .\kosis_explorer.py --parent <목록ID> --save output\kosis-list.json
```

기본 서비스뷰는 국내통계 주제별(`MT_ZTITLE`)입니다. 기관별 목록 등은 `--view`로 변경할 수 있습니다.

```powershell
python .\kosis_explorer.py --view MT_OTITLE --parent A
```
