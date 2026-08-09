"""관세청 API로 기존 data.js 값을 재현할 수 있는지 역추적하는 스크립트 (일회성).

기존 수출 데이터는 로컬 PC의 fetch_macro_data.py가 만든 것이라 어떤 HS코드를
어떤 품목으로 묶었는지 모른다. 새 달을 자동으로 이어붙이려면 그 정의를 똑같이
맞춰야 하므로, 이미 값이 있는 2026-05를 API로 다시 받아 후보 HS코드 조합 중
저장된 값과 일치하는 것을 찾는다.

확인이 끝나면 이 파일과 diagnose-kr-apis.yml 워크플로는 삭제한다.
"""

import os
import xml.etree.ElementTree as ET
from collections import defaultdict

import requests

CUSTOMS_KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")
CUSTOMS_URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"

# data.js에 저장되어 있는 2026-05 실제 값 (역추적 목표)
TARGET_TOTAL_USD = 87821386 * 1000        # export_total_1000usd는 천달러 단위
TARGETS = {
    "반도체": 29433286302.0,
    "자동차": 5492266174.0,
    "이차전지": 687989142.0,
    "선박": 2456481037.0,
    "철강판재류": 367383321.0,
}


def fetch_month(yymm):
    print(f"\n=== {yymm} 전체 품목 조회 ===")
    res = requests.get(
        CUSTOMS_URL,
        params={
            "serviceKey": CUSTOMS_KEY,
            "strtYymm": yymm,
            "endYymm": yymm,
            "hsSgn": "",
        },
        timeout=120,
    )
    print("HTTP", res.status_code, len(res.content), "bytes")
    root = ET.fromstring(res.text)
    print("resultMsg:", root.findtext(".//resultMsg"))

    by_hs = {}
    total_row = None
    for it in root.findall(".//item"):
        year = it.findtext("year")
        hs = (it.findtext("hsCode") or "").strip()
        try:
            exp = float(it.findtext("expDlr") or 0)
        except ValueError:
            continue
        if year == "총계":
            total_row = exp
            continue
        if hs:
            by_hs[hs] = exp
    print("품목 수:", len(by_hs))
    print("총계 행 expDlr:", f"{total_row:,.0f}" if total_row is not None else None)
    print("전체 합산       :", f"{sum(by_hs.values()):,.0f}")
    print("data.js 저장 총액:", f"{TARGET_TOTAL_USD:,.0f}")
    return by_hs, total_row


def psum(by_hs, *prefixes):
    return sum(v for hs, v in by_hs.items() if hs.startswith(prefixes))


def report(by_hs):
    print("\n=== 후보 HS코드별 합계 (2026-05) ===")
    candidates = {
        "8541 (반도체소자)": ("8541",),
        "8542 (집적회로)": ("8542",),
        "8541+8542": ("8541", "8542"),
        "8703 (승용차)": ("8703",),
        "8704 (화물차)": ("8704",),
        "8703+8704": ("8703", "8704"),
        "8507 (축전지)": ("8507",),
        "850760 (리튬이온)": ("850760",),
        "89 (선박류 전체)": ("89",),
        "8901": ("8901",),
        "8902": ("8902",),
        "8904": ("8904",),
        "8905": ("8905",),
        "8906": ("8906",),
        "8901+8902+8904+8905+8906": ("8901", "8902", "8904", "8905", "8906"),
        "72 (철강 전체)": ("72",),
        "7208": ("7208",),
        "7209": ("7209",),
        "7210": ("7210",),
        "7211": ("7211",),
        "7212": ("7212",),
        "7208~7212": ("7208", "7209", "7210", "7211", "7212"),
        "7219": ("7219",),
        "7225": ("7225",),
    }
    for label, pref in candidates.items():
        print(f"  {label:32s} = {psum(by_hs, *pref):>18,.0f}")

    print("\n=== 저장값과 일치하는 조합 찾기 ===")
    for cat, target in TARGETS.items():
        hits = []
        for label, pref in candidates.items():
            got = psum(by_hs, *pref)
            if target and abs(got - target) / target < 0.005:
                hits.append(f"{label} (오차 {abs(got-target)/target*100:.3f}%)")
        print(f"  {cat} (목표 {target:,.0f}): {hits if hits else '단일 후보로는 일치 없음'}")

    # 일치가 없으면 목표값에 가까운 개별 HS 4자리 묶음을 직접 탐색
    print("\n=== 목표값에 근접한 HS 4자리 코드 (오차 1% 이내) ===")
    by4 = defaultdict(float)
    for hs, v in by_hs.items():
        by4[hs[:4]] += v
    for cat, target in TARGETS.items():
        near = [(k, v) for k, v in by4.items() if target and abs(v - target) / target < 0.01]
        print(f"  {cat}: {sorted(near, key=lambda x: -x[1])[:5]}")


by_hs, total_row = fetch_month("202605")
report(by_hs)

# 새로 추가할 달(2026-06)도 같은 방식으로 받을 수 있는지 확인
by_hs6, total6 = fetch_month("202606")
print("\n2026-06 총계(달러):", f"{total6:,.0f}" if total6 else None)

print("\n탐색 완료.")
