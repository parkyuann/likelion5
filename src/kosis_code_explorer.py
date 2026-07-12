"""
KOSIS 통계표의 objL1~8 / itmId 실제 코드값을 알아내는 도구.

Open API는 분류코드(objL1 등)와 항목코드(itmId)를 '코드값'으로 요구하지만,
그 코드값 목록을 제공하는 API 엔드포인트는 공개 문서에서 찾지 못했다.
대신 kosis.kr의 통계표 화면(statHtml.do)이 브라우저에서 렌더링될 때
그 표에 쓰이는 코드값을 fancytree 위젯의 노드 데이터(.data.itmId)로 들고 있어서,
Selenium으로 그 화면을 열어 코드값을 읽어내는 방식으로 우회한다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/kosis_code_explorer.py 101 DT_1EA1019
"""
import sys
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


def explore_table_codes(org_id: str, tbl_id: str) -> list[list[dict]]:
    """표 화면에 있는 모든 분류/항목 트리(fancytree)의 노드를 코드값과 함께 반환한다.

    반환값은 트리별 노드 리스트의 리스트. 표마다 분류축 개수가 달라서
    어떤 트리가 objL1인지 objL2인지 itmId인지는 title/scrEng를 보고 직접 판단해야 한다.
    """
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)
    trees: list[list[dict]] = []
    try:
        driver.get(f"https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}")
        WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(5)  # jqGrid/fancytree 비동기 렌더링 대기

        frames = driver.find_elements(By.TAG_NAME, "iframe")
        driver.switch_to.frame(frames[1])  # iframe_rightMenu
        inner_frames = driver.find_elements(By.TAG_NAME, "iframe")
        driver.switch_to.frame(inner_frames[0])  # iframe_centerMenu
        time.sleep(2)

        tree_count = driver.execute_script(
            "return document.querySelectorAll('[id^=fancytree_]').length;"
        )
        for i in range(tree_count):
            nodes = driver.execute_script(f"""
                try {{
                    var tree = $('#fancytree_{i}').fancytree('getTree');
                    var nodes = [];
                    tree.visit(function(n){{ nodes.push({{key: n.key, title: n.title, data: n.data}}); }});
                    return nodes;
                }} catch(e) {{ return null; }}
            """)
            if nodes:
                trees.append(nodes)
    finally:
        driver.quit()

    return trees


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 기본 코드페이지에서 한글 깨짐 방지

    if len(sys.argv) != 3:
        print("사용법: python kosis_code_explorer.py <orgId> <tblId>")
        print("예시:   python kosis_code_explorer.py 101 DT_1EA1019")
        sys.exit(1)

    org_id, tbl_id = sys.argv[1], sys.argv[2]
    for idx, tree in enumerate(explore_table_codes(org_id, tbl_id)):
        print(f"\n=== fancytree_{idx} ({len(tree)} nodes) ===")
        for node in tree:
            d = node.get("data") or {}
            print(f"  itmId={d.get('itmId')!s:<10} scrKor={d.get('scrKor')}  scrEng={d.get('scrEng')}")
