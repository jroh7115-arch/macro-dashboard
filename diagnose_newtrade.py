"""관세청 수출입총괄(GW) API가 속보 시점에 이미 값을 주는지 확인하는 일회성 스크립트."""
import os, time, xml.etree.ElementTree as ET
import requests

KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")
BASES = [
    ("Newtrade(수출입총괄)", "apis.data.go.kr/1220000/Newtrade/getNewtradeList"),
    ("Itemtrade(품목별)", "apis.data.go.kr/1220000/Itemtrade/getItemtradeList"),
]

def call(label, base, ym, extra=None):
    params = {"serviceKey": KEY, "strtYymm": ym, "endYymm": ym}
    if extra: params.update(extra)
    for scheme in ("https", "http"):
        url = f"{scheme}://{base}"
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=(15, 120))
                print(f"\n[{label} {ym}] {scheme} HTTP {r.status_code} {len(r.content)}bytes")
                body = r.text[:600].replace("\n", " ")
                print("  응답:", body)
                try:
                    root = ET.fromstring(r.text)
                    print("  resultCode:", root.findtext(".//resultCode"),
                          "| resultMsg:", root.findtext(".//resultMsg"))
                    items = root.findall(".//item")
                    print("  item 수:", len(items))
                    for it in items[:4]:
                        print("   ", {c.tag: c.text for c in it})
                except Exception as e:
                    print("  XML 파싱 불가:", type(e).__name__)
                return
            except Exception as e:
                print(f"  {scheme} 시도 {attempt+1}/3 실패({type(e).__name__})")
                time.sleep(8 * (attempt + 1))
    print(f"[{label} {ym}] 접속 불가")

print("외부 IP:", requests.get("https://api.ipify.org", timeout=10).text)
for label, base in BASES:
    for ym in ("202606", "202607"):
        call(label, base, ym)
