"""산업부 기준 반도체에 해당하는 HS코드 조합을 찾는 일회성 스크립트."""
import os, xml.etree.ElementTree as ET
from collections import defaultdict
import requests

KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")
URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
YM = "202606"
TOTAL_TARGET = 102133608939.0      # 2026-06 총수출(관세청)
SEMI_TARGET = TOTAL_TARGET * 0.438  # 산업부 기준 반도체 43.8%

res = requests.get(URL, params={"serviceKey": KEY, "strtYymm": YM, "endYymm": YM, "hsSgn": ""}, timeout=180)
print("HTTP", res.status_code, len(res.content), "bytes")
root = ET.fromstring(res.text)
print("resultMsg:", root.findtext(".//resultMsg"))
by_hs = {}
for it in root.findall(".//item"):
    if it.findtext("year") == "총계":
        continue
    hs = (it.findtext("hsCode") or "").strip()
    try:
        v = float(it.findtext("expDlr") or 0)
    except ValueError:
        continue
    if hs:
        by_hs[hs] = v
print("품목 수:", len(by_hs), "| 합계:", f"{sum(by_hs.values()):,.0f}")
print(f"목표(산업부 반도체 43.8%): {SEMI_TARGET:,.0f}")

by4 = defaultdict(float)
for hs, v in by_hs.items():
    by4[hs[:4]] += v
print("\n=== 수출액 상위 HS 4자리 20개 ===")
for code, v in sorted(by4.items(), key=lambda x: -x[1])[:20]:
    print(f"  {code}  {v:>16,.0f}  ({v/TOTAL_TARGET*100:5.2f}%)")

print("\n=== 반도체 후보 조합 ===")
combos = {
    "8542": ("8542",),
    "8541+8542": ("8541", "8542"),
    "8541+8542+8523": ("8541", "8542", "8523"),
    "8541+8542+8486": ("8541", "8542", "8486"),
    "8541+8542+8473": ("8541", "8542", "8473"),
    "8541+8542+8523+3818": ("8541", "8542", "8523", "3818"),
    "8541+8542+8534": ("8541", "8542", "8534"),
}
for label, pref in combos.items():
    v = sum(x for hs, x in by_hs.items() if hs.startswith(pref))
    print(f"  {label:24s} {v:>16,.0f}  비중 {v/TOTAL_TARGET*100:5.2f}%  목표대비 {(v/SEMI_TARGET-1)*100:+6.1f}%")

print("\n=== 8542에 더해 목표를 채울 만한 4자리 코드 (부족분에 근접) ===")
gap = SEMI_TARGET - by4["8542"]
print(f"  부족분 {gap:,.0f}")
for code, v in sorted(by4.items(), key=lambda x: abs(x[1] - gap))[:8]:
    print(f"    {code}  {v:>16,.0f}  (부족분 대비 {(v/gap-1)*100:+.1f}%)")
