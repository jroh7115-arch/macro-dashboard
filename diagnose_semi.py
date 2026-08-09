"""반도체(산업부 기준) HS 조합을 여러 달로 교차검증."""
import os, xml.etree.ElementTree as ET
from collections import defaultdict
import requests

KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")
URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
# 자료(산업부 수출입 동향)의 품목 비중 %
TARGETS = {
    "202605": {"반도체": 42.3},
    "202606": {"반도체": 43.8, "컴퓨터": 4.8, "자동차": 6.6, "선박": 2.8, "이차전지": 0.7, "철강제품": 2.1, "석유제품": 5.5},
}
COMBOS = {
    "8542": ("8542",),
    "8541+8542": ("8541", "8542"),
    "8541+8542+8473": ("8541", "8542", "8473"),
    "8542+8473": ("8542", "8473"),
    "8541+8542+8523": ("8541", "8542", "8523"),
    "8541+8542+8473+8486": ("8541", "8542", "8473", "8486"),
}

def fetch(ym):
    import time
    last = None
    for i in range(6):
        url = URL if i % 2 == 0 else URL.replace("https://", "http://")
        try:
            r = requests.get(url, params={"serviceKey": KEY, "strtYymm": ym, "endYymm": ym, "hsSgn": ""},
                             timeout=(15, 150))
            r.raise_for_status()
            break
        except Exception as e:
            last = e
            print(f"  {ym} 시도 {i+1}/6 실패({type(e).__name__}) 재시도...")
            time.sleep(10 * (i + 1))
    else:
        raise last
    root = ET.fromstring(r.text)
    by, tot = {}, None
    for it in root.findall(".//item"):
        if it.findtext("year") == "총계":
            tot = float(it.findtext("expDlr") or 0); continue
        hs = (it.findtext("hsCode") or "").strip()
        try: v = float(it.findtext("expDlr") or 0)
        except ValueError: continue
        if hs: by[hs] = v
    return by, (tot if tot else sum(by.values()))

for ym, tg in TARGETS.items():
    by, tot = fetch(ym)
    print(f"\n===== {ym} (총수출 {tot:,.0f}) =====")
    t = tg["반도체"]
    print(f"  [반도체] 자료 {t}%")
    for label, pref in COMBOS.items():
        v = sum(x for hs, x in by.items() if hs.startswith(pref))
        share = v / tot * 100
        print(f"    {label:22s} {share:6.2f}%  (자료대비 {share-t:+.2f}%p)")
    if "컴퓨터" in tg:
        agg = defaultdict(float)
        for hs, v in by.items(): agg[hs[:4]] += v
        print(f"  [참고] 8471 컴퓨터 {agg['8471']/tot*100:.2f}% (자료 컴퓨터 {tg['컴퓨터']}%)"
              f" | 8473 {agg['8473']/tot*100:.2f}% | 8523 {agg['8523']/tot*100:.2f}%")
        for cat, code, t2 in [("자동차","8703",tg["자동차"]),("선박","8901",tg["선박"]),
                              ("이차전지","8507",tg["이차전지"]),("석유제품","2710",tg["석유제품"])]:
            print(f"  [참고] {cat} HS{code} {agg[code]/tot*100:.2f}% (자료 {t2}%)")
