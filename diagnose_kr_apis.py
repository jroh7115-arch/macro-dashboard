"""GitHub Actions(해외 IP)에서 KOSIS/관세청 API가 실제로 닿는지 확인하는 일회성 진단 스크립트.

'한국 IP에서만 접속된다'는 기존 가정이 사실인지, 아니면 일시적인 장애였는지를
가리기 위한 것. 결과에 따라 수출 데이터도 자동 갱신 대상에 넣을 수 있는지 결정한다.
확인이 끝나면 이 파일과 diagnose-kr-apis.yml 워크플로는 삭제한다.
"""

import os
import socket
import time

import requests

CUSTOMS_KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")
KOSIS_KEY = os.environ.get("KOSIS_API_KEY", "")

print(f"CUSTOMS_SERVICE_KEY 존재: {bool(CUSTOMS_KEY)} (길이 {len(CUSTOMS_KEY)})")
print(f"KOSIS_API_KEY 존재: {bool(KOSIS_KEY)} (길이 {len(KOSIS_KEY)})")

try:
    print("이 러너의 외부 IP:", requests.get("https://api.ipify.org", timeout=10).text)
except Exception as e:
    print("외부 IP 조회 실패:", e)


def probe_dns(host):
    try:
        return f"DNS OK -> {socket.gethostbyname(host)}"
    except Exception as e:
        return f"DNS 실패: {type(e).__name__}: {e}"


def probe_http(label, url, params=None, timeout=30):
    print(f"\n=== {label} ===")
    print("URL:", url)
    t0 = time.time()
    try:
        res = requests.get(url, params=params, timeout=timeout)
        dt = time.time() - t0
        print(f"HTTP {res.status_code} ({dt:.1f}s), {len(res.content)} bytes")
        body = res.text[:1200]
        print("응답 앞부분:", body.replace("\n", " ")[:1200])
        return res
    except Exception as e:
        dt = time.time() - t0
        print(f"실패 ({dt:.1f}s): {type(e).__name__}: {e}")
        return None


for host in ["kosis.kr", "apis.data.go.kr", "unipass.customs.go.kr", "ecos.bok.or.kr"]:
    print(f"{host}: {probe_dns(host)}")

# 1) 관세청 품목별 수출입실적 (공공데이터포털). 반도체 HS 8542 최근 3개월.
probe_http(
    "관세청 품목별 수출입실적 (apis.data.go.kr)",
    "http://apis.data.go.kr/1220000/Itemtrade/getItemtradeList",
    params={
        "serviceKey": CUSTOMS_KEY,
        "strtYymm": "202605",
        "endYymm": "202607",
        "hsSgn": "8542",
    },
)

# 2) 같은 API의 https 버전 (http가 막히는 경우 대비)
probe_http(
    "관세청 품목별 수출입실적 (https)",
    "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList",
    params={
        "serviceKey": CUSTOMS_KEY,
        "strtYymm": "202605",
        "endYymm": "202607",
        "hsSgn": "8542",
    },
)

# 3) KOSIS 통계목록 API (가장 가벼운 호출로 인증/접속 여부만 확인)
probe_http(
    "KOSIS 통계목록 (kosis.kr)",
    "https://kosis.kr/openapi/statisticsList.do",
    params={
        "method": "getList",
        "apiKey": KOSIS_KEY,
        "vwCd": "MT_ZTITLE",
        "parentListId": "A_1",
        "format": "json",
        "jsonVD": "Y",
    },
)

# 4) KOSIS 루트 (인증과 무관하게 서버 자체가 응답하는지)
probe_http("KOSIS 루트 접속", "https://kosis.kr/index/index.do")

print("\n진단 완료.")
