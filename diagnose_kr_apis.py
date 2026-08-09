"""품목 HS코드 정의를 여러 달로 교차검증하는 스크립트 (일회성).

2026-05 한 달만으로는 철강판재류 정의가 특정되지 않아, 저장값이 있는 3개월을
모두 받아 후보별 오차를 비교한다. 세 달 모두 일관되게 오차가 작은 후보가
로컬 스크립트가 쓴 정의다.

확인이 끝나면 이 파일과 diagnose-kr-apis.yml 워크플로는 삭제한다.
"""

import os
import xml.etree.ElementTree as ET
from collections import defaultdict

import requests

CUSTOMS_KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")
CUSTOMS_URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"

# data.js에 저장된 실제 값 (달러 원시값). 총액만 천달러 단위라 1000을 곱해둔다.
STORED = {
    "202603": {
        "total": 87320893 * 1000,
        "반도체": 24906009306.0, "자동차": 6078469685.0, "이차전지": 819233212.0,
        "선박": 2595347641.0, "철강판재류": 410043040.0,
    },
    "202604": {
        "total": 85830054 * 1000,
        "반도체": 25239581961.0, "자동차": 5843132201.0, "이차전지": 653328327.0,
        "선박": 2614393838.0, "철강판재류": 402602935.0,
    },
    "202605": {
        "total": 87821386 * 1000,
        "반도체": 29433286302.0, "자동차": 5492266174.0, "이차전지": 687989142.0,
        "선박": 2456481037.0, "철강판재류": 367383321.0,
    },
}

CANDIDATES = {
    "반도체": {"8542": ("8542",), "8541+8542": ("8541", "8542")},
    "자동차": {"8703": ("8703",), "8703+8704": ("8703", "8704")},
    "이차전지": {"8507": ("8507",), "850760": ("850760",)},
    "선박": {"89": ("89",), "8901": ("8901",)},
    "철강판재류": {
        "7208": ("7208",), "7209": ("7209",), "7210": ("7210",),
        "7211": ("7211",), "7212": ("7212",), "7219": ("7219",),
        "7225": ("7225",), "7226": ("7226",),
        "7208+7209": ("7208", "7209"), "7211+7212": ("7211", "7212"),
        "7219+7220": ("7219", "7220"), "7225+7226": ("7225", "7226"),
        "7208~7212": ("7208", "7209", "7210", "7211", "7212"),
    },
}


def fetch_month(yymm):
    res = requests.get(
        CUSTOMS_URL,
        params={"serviceKey": CUSTOMS_KEY, "strtYymm": yymm, "endYymm": yymm, "hsSgn": ""},
        timeout=120,
    )
    root = ET.fromstring(res.text)
    by_hs, total_row = {}, None
    for it in root.findall(".//item"):
        year = it.findtext("year")
        hs = (it.findtext("hsCode") or "").strip()
        try:
            exp = float(it.findtext("expDlr") or 0)
        except ValueError:
            continue
        if year == "총계":
            total_row = exp
        elif hs:
            by_hs[hs] = exp
    print(f"{yymm}: HTTP {res.status_code}, 품목 {len(by_hs)}개, 총계 {total_row:,.0f}")
    return by_hs, total_row


def psum(by_hs, prefixes):
    return sum(v for hs, v in by_hs.items() if hs.startswith(prefixes))


months = {}
for ym in STORED:
    months[ym] = fetch_month(ym)

print("\n=== 총액: API 총계 vs data.js 저장값 ===")
for ym, (by_hs, total_row) in months.items():
    stored = STORED[ym]["total"]
    print(f"  {ym}: API {total_row:>16,.0f} | 저장 {stored:>16,.0f} | 차이 {(total_row/stored-1)*100:+.3f}%")

print("\n=== 품목별 후보 오차 (세 달 모두 작아야 정답) ===")
for cat, cands in CANDIDATES.items():
    print(f"\n[{cat}]")
    rows = []
    for label, pref in cands.items():
        errs = []
        for ym, (by_hs, _) in months.items():
            target = STORED[ym][cat]
            got = psum(by_hs, pref)
            errs.append(abs(got - target) / target * 100 if target else 999)
        rows.append((max(errs), label, errs))
    for worst, label, errs in sorted(rows):
        detail = " ".join(f"{ym}:{e:6.2f}%" for ym, e in zip(months, errs))
        mark = "  <-- 유력" if worst < 0.5 else ""
        print(f"  {label:14s} 최대오차 {worst:7.2f}%  ({detail}){mark}")

# 철강판재류가 여전히 안 맞으면, 세 달 저장값 비율에 맞는 HS 4자리를 직접 탐색
print("\n=== 철강판재류: 세 달 값 패턴이 맞는 HS 4자리 탐색 (각 달 오차 3% 이내) ===")
by4 = {}
for ym, (by_hs, _) in months.items():
    agg = defaultdict(float)
    for hs, v in by_hs.items():
        agg[hs[:4]] += v
    by4[ym] = agg
common = set.intersection(*[set(v) for v in by4.values()])
hits = []
for code in common:
    errs = [abs(by4[ym][code] - STORED[ym]["철강판재류"]) / STORED[ym]["철강판재류"] * 100 for ym in months]
    if max(errs) < 3:
        hits.append((max(errs), code, errs))
for worst, code, errs in sorted(hits)[:10]:
    print(f"  HS {code}: 최대오차 {worst:.2f}%  " + " ".join(f"{ym}:{e:.2f}%" for ym, e in zip(months, errs)))
if not hits:
    print("  3% 이내 단일 4자리 코드 없음")

print("\n탐색 완료.")
