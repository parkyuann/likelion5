# BGE-m3 후보상한 GPU 번들

1. Python 3.11/CUDA 12.4 환경에서 `pip install -r requirements.lock.txt`를 실행합니다.
2. `BAAI/bge-m3`를 revision `5617a9f61b028005a4858fdac845db406aefb181`로 다운로드하고 SHA를 기록합니다.
3. `python bge_m3_encode.py --bundle . --output encoded --resume`로 20,000행 shard 인코딩을 재개합니다. 모든 shard가 완결되기 전 final manifest는 생성하지 않습니다.
4. `python bge_m3_search.py --encoded encoded --output search --top-l 100`으로 exact dense/sparse Top-L을 생성합니다.
5. manifest, 원자료, 로그를 회신물로 회수합니다. gold는 데스크톱 번들·인코더·검색기에 넣지 않습니다.
