"""KOSIS/관세청 API 탐색 스크립트 (일회성).

1단계 진단에서 두 API 모두 GitHub Actions(해외 IP)에서 정상 응답하는 것을 확인했다.
이제 수출 데이터를 자동 수집하려면 아래를 확정해야 한다.
  (1) 관세청 품목별 API가 최신 몇 월치까지 주는가 (6월/7월 데이터 존재 여부)
  (2) 수출 총액을 어디서 가져올 것인가 (관세청 총계 vs KOSIS 표)
확인이 끝나면 이 파일과 diagnose-kr-apis.yml 워크플로는 삭제한다.
"""

import os
import xml.etree.ElementTree as ET

import requests

CUSTOMS_KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")
KOSIS_KEY = os.environ.get("KOSIS_API_KEY", "")

CUSTOMS_URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"


def customs_call(label, **params):
    print(f"\n=== {label} ===")
    p = {"serviceKey": CUSTOMS_KEY}
    p.update(params)
    print("params:", {k: v for k, v in params.items()})
    try:
        res = requests.get(CUSTOMS_URL, params=p, timeout=30)
    except Exception as e:
        print("실패:", type(e).__name__, e)
        return
    print("HTTP", res.status_code, len(res.content), "bytes")
    try:
        root = ET.fromstring(res.text)
    except Exception as e:
        print("XML 파싱 실패:", e, "| 앞부분:", res.text[:300])
        return
    msg = root.findtext(".//resultMsg")
    print("resultMsg:", msg)
    items = root.findall(".//item")
    print("item 수:", len(items))
    for it in items[:6]:
        print(
            "  year=", it.findtext("year"),
            "hs=", it.findtext("hsCode"),
            "expDlr=", it.findtext("expDlr"),
            "stat=", (it.findtext("statKor") or "")[:30],
        )
    years = sorted({it.findtext("year") for it in items if it.findtext("year")})
    print("응답에 포함된 연월:", years)


# (1) 품목(반도체 HS8542) 최신월 확인 - 6월/7월이 나오는지
customs_call("반도체 8542, 202604~202608", strtYymm="202604", endYymm="202608", hsSgn="8542")

# (2) hsSgn을 비우거나 총계 코드로 두면 전체 수출총액이 나오는지 확인
customs_call("hsSgn 미지정, 202605~202607", strtYymm="202605", endYymm="202607")
customs_call("hsSgn='总' 대신 총계 시도 (빈문자열)", strtYymm="202605", endYymm="202607", hsSgn="")

# (3) 2단위 HS로 조회하면 그 류의 합계가 나오는지 (총액 계산 가능성 확인)
customs_call("HS 2단위 85류, 202606", strtYymm="202606", endYymm="202606", hsSgn="85")

# ------------------------------------------------------------------
# KOSIS: 수출총액이 들어있는 표 찾기
# ------------------------------------------------------------------
def kosis_search(label, search_nm):
    print(f"\n=== KOSIS 검색: {label} ===")
    url = "https://kosis.kr/openapi/statisticsSearch.do"
    params = {
        "method": "getList",
        "apiKey": KOSIS_KEY,
        "format": "json",
        "jsonVD": "Y",
        "searchNm": search_nm,
        "startCount": "1",
        "resultCount": "20",
    }
    try:
        res = requests.get(url, params=params, timeout=30)
    except Exception as e:
        print("실패:", type(e).__name__, e)
        return
    print("HTTP", res.status_code, len(res.content), "bytes")
    try:
        data = res.json()
    except Exception:
        print("JSON 아님, 앞부분:", res.text[:400])
        return
    if isinstance(data, dict):
        print("응답(dict):", str(data)[:400])
        return
    for row in data[:20]:
        print(
            "  ORG_ID=", row.get("ORG_ID"),
            "TBL_ID=", row.get("TBL_ID"),
            "|", (row.get("TBL_NM") or "")[:50],
            "| 수록기간:", row.get("PRD_DE"), row.get("REC_TBL_SE"),
        )


kosis_search("품목별 수출입", "품목별 수출입")
kosis_search("수출입 총괄", "수출입 총괄")

print("\n탐색 완료.")
