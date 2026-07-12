r"""
한국 매크로 대시보드용 raw 데이터 수집 스크립트 - GitHub Actions 전용 버전.

이 파일은 로컬 PC용 fetch_macro_data.py와 내용이 거의 같지만, API 키를
소스코드에 직접 적지 않고 환경변수(GitHub Secrets)에서만 읽어옵니다.
그래서 이 파일은 공개 저장소(GitHub)에 올려도 안전합니다.

*** 로컬 PC에서는 이 파일 대신 fetch_macro_data.py(키가 이미 들어있는 버전)를
    사용하세요. 이 파일은 GitHub Actions 워크플로우가 실행하는 용도입니다. ***

필요한 환경변수 (GitHub 저장소 Settings > Secrets and variables > Actions 에서
FRED_API_KEY / KOSIS_API_KEY / CUSTOMS_SERVICE_KEY 세 개를 등록해두면
워크플로우가 자동으로 넘겨줍니다):
    FRED_API_KEY
    KOSIS_API_KEY
    CUSTOMS_SERVICE_KEY

사용법 (GitHub Actions 워크플로우 안에서):
    pip install requests yfinance pandas holidays
    python fetch_macro_data_github.py

완료되면 이 폴더에 data.js가 생성/갱신됩니다.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests

# ------------------------------------------------------------------
# API 키 (환경변수 전용 - 소스코드에 실제 키 값을 적지 않는다)
# ------------------------------------------------------------------
def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"[오류] 환경변수 {name}가 설정되어 있지 않습니다. "
              f"GitHub 저장소 Settings > Secrets and variables > Actions 에서 "
              f"{name}을(를) 등록했는지, 워크플로우 yml의 env: 항목에 넘겨줬는지 확인하세요.")
        sys.exit(1)
    return val


FRED_API_KEY = _required_env("FRED_API_KEY")
KOSIS_API_KEY = _required_env("KOSIS_API_KEY")
CUSTOMS_SERVICE_KEY = _required_env("CUSTOMS_SERVICE_KEY")

COUNTRIES = {
    "KOR": "한국", "USA": "미국", "CHN": "중국", "JPN": "일본", "DEU": "독일",
    "GBR": "영국", "IND": "인도", "BRA": "브라질", "MEX": "멕시코",
    "FRA": "프랑스", "ITA": "이탈리아", "ESP": "스페인", "IDN": "인도네시아",
}

CATEGORIES = {
    "반도체": "8542",
    "자동차": "8703",
    "이차전지": "8507",
    "선박": "8901",
    "철강판재류": "7208",
}

# FRED에서 가져올 미국 매크로 시리즈
FRED_MACRO_SERIES = {
    "DGS10": "미국 국채 10년물 금리",
    "DGS2": "미국 국채 2년물 금리",
    "T10Y2Y": "미국 10Y-2Y 금리차",
    "M2SL": "미국 M2 통화량",
    "VIXCLS": "VIX 변동성지수",
    "SP500": "S&P500",
    "NASDAQCOM": "나스닥종합지수",
    "WALCL": "Fed 대차대조표(총자산)",
    "BAMLH0A0HYM2": "미국 하이일드 스프레드(ICE BofA OAS)",
}

CLI_MONTHS_BACK = 121      # CLI: 10년 (나머지 데이터 기준)
EXPORT_MONTHS_BACK = 61    # 수출입 데이터(KOSIS 총액, 관세청 품목별): 5년
DAILY_YEARS_BACK = 10      # 미국 금리/M2/VIX/지수/WALCL, KOSPI/KOSDAQ150: 10년


def month_range(months_back: int):
    now = datetime.now()
    y, m = now.year, now.month - (months_back - 1)
    while m <= 0:
        m += 12
        y -= 1
    out = []
    for _ in range(months_back):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


# ------------------------------------------------------------------
# 1. FRED: OECD CLI
# ------------------------------------------------------------------
def fetch_fred_cli(country_code: str, months_back: int):
    series_id = f"{country_code}LOLITOAASTSAM"
    raw = fetch_fred_series(series_id, months_back=months_back)
    return {d[:7].replace("-", ""): v for d, v in raw.items()}


def fetch_fred_series(series_id: str, months_back: int = None, start_date: str = None):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
    }
    if start_date:
        params["observation_start"] = start_date
    if months_back:
        params["sort_order"] = "desc"
        params["limit"] = months_back
    res = requests.get(url, params=params, timeout=20)
    res.raise_for_status()
    obs = res.json().get("observations", [])
    out = {}
    for o in obs:
        try:
            out[o["date"]] = float(o["value"])
        except ValueError:
            out[o["date"]] = None
    return out


# ------------------------------------------------------------------
# 2. KOSIS: 수출 총액
# ------------------------------------------------------------------
def fetch_kosis_total(months_back: int):
    url = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
    params = {
        "method": "getList",
        "apiKey": KOSIS_API_KEY,
        "itmId": "13103112831T1",
        "objL1": "13102112831A.A",
        "format": "json",
        "jsonVD": "Y",
        "prdSe": "M",
        "newEstPrdCnt": str(months_back),
        "orgId": "360",
        "tblId": "DT_1R11001_FRM101",
    }
    res = requests.get(url, params=params, timeout=20)
    res.raise_for_status()
    rows = res.json()
    return {row["PRD_DE"]: int(row["DT"]) for row in rows}


# ------------------------------------------------------------------
# 2-1. 월별 조업일수 추정 및 1일평균 수출액 계산
# ------------------------------------------------------------------
def working_days_in_month(yyyymm: str) -> int:
    import calendar
    y, m = int(yyyymm[:4]), int(yyyymm[4:])
    last_day = calendar.monthrange(y, m)[1]
    try:
        import holidays
        kr_holidays = holidays.KR(years=y)
    except ImportError:
        kr_holidays = {}
    cnt = 0
    for d in range(1, last_day + 1):
        dt = datetime(y, m, d).date()
        if dt.weekday() >= 5:
            continue
        if dt in kr_holidays:
            continue
        cnt += 1
    return cnt


def compute_export_daily_avg(export_total: dict) -> dict:
    out = {}
    for ym, total in export_total.items():
        if total is None:
            out[ym] = None
            continue
        wd = working_days_in_month(ym)
        out[ym] = round(total / wd, 1) if wd else None
    return out


# ------------------------------------------------------------------
# 3. 관세청 GW API: 품목별 수출액
# ------------------------------------------------------------------
def fetch_customs_month_total(hs_prefix: str, yyyymm: str):
    url = "http://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
    params = {
        "serviceKey": CUSTOMS_SERVICE_KEY,
        "strtYymm": yyyymm,
        "endYymm": yyyymm,
        "hsSgn": hs_prefix,
        "type": "json",
    }
    try:
        res = requests.get(url, params=params, timeout=15)
        res.raise_for_status()
        try:
            items = res.json()["response"]["body"]["items"]["item"]
        except Exception:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(res.text)
            items = [{c.tag: c.text for c in item} for item in root.findall(".//item")]
        if isinstance(items, dict):
            items = [items]
        for it in items:
            if it.get("hsCode") == "-":
                return float(it.get("expDlr", 0))
    except Exception as e:
        print(f"    에러({hs_prefix},{yyyymm}): {e}")
    return None


# ------------------------------------------------------------------
# MDD(고점대비 낙폭, %) 계산
# ------------------------------------------------------------------
def compute_mdd_series(price_by_date: dict):
    dates = sorted(price_by_date.keys())
    running_max = None
    out = {}
    for d in dates:
        p = price_by_date[d]
        if p is None:
            out[d] = None
            continue
        running_max = p if running_max is None else max(running_max, p)
        out[d] = round((p / running_max - 1) * 100, 3)
    return out


# ------------------------------------------------------------------
# KOSPI / KOSDAQ150 / VKOSPI (yfinance)
# ------------------------------------------------------------------
def fetch_yfinance_series(ticker: str, years_back: int):
    try:
        import yfinance as yf
    except ImportError:
        print("  yfinance가 설치되어 있지 않습니다.")
        return {}
    start = (datetime.now() - timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    df = yf.download(ticker, start=start, progress=False)
    if df.empty:
        print(f"  {ticker}: 데이터 없음")
        return {}
    close_col = "Close" if "Close" in df.columns else df.columns[0]
    out = {}
    for idx, row in df.iterrows():
        d = idx.strftime("%Y-%m-%d")
        val = row[close_col]
        try:
            out[d] = float(val)
        except (TypeError, ValueError):
            out[d] = None
    return out


def main():
    months = month_range(CLI_MONTHS_BACK)
    export_months = month_range(EXPORT_MONTHS_BACK)
    daily_start = (datetime.now() - timedelta(days=365 * DAILY_YEARS_BACK)).strftime("%Y-%m-%d")
    print(f"CLI 조회 기간: {months[0]} ~ {months[-1]} ({len(months)}개월, 10년)")
    print(f"수출입 조회 기간: {export_months[0]} ~ {export_months[-1]} ({len(export_months)}개월, 5년)")
    print(f"일별 지표 조회 시작일: {daily_start} (10년)")

    n_hs = len(CATEGORIES)
    est_calls = n_hs * len(export_months)
    print(f"관세청 API 예상 호출 횟수: 약 {est_calls}회 (일일 한도 1,000회 기준 확인 필요)")

    print("\n[1/8] FRED에서 OECD CLI 수집 중 (10년)...")
    cli = {}
    for code in COUNTRIES:
        print(f"  - {code}")
        cli[code] = fetch_fred_cli(code, CLI_MONTHS_BACK)
        time.sleep(0.2)

    print("\n[2/8] KOSIS에서 수출총액 수집 중 (5년)...")
    export_total = fetch_kosis_total(EXPORT_MONTHS_BACK)
    export_daily_avg = compute_export_daily_avg(export_total)

    print("\n[3/8] 관세청 API에서 품목별 수출액 수집 중 (5년)...")
    product_months = export_months
    product = {cat: {} for cat in CATEGORIES}
    for cat, hs in CATEGORIES.items():
        print(f"  - {cat} (HS {hs})")
        for ym in product_months:
            product[cat][ym] = fetch_customs_month_total(hs, ym)
            time.sleep(0.15)

    print("\n[4/8] FRED에서 미국 금리·M2·VIX·지수 수집 중...")
    fred_macro = {}
    for sid, label in FRED_MACRO_SERIES.items():
        print(f"  - {sid} ({label})")
        fred_macro[sid] = fetch_fred_series(sid, start_date=daily_start)
        time.sleep(0.2)

    print("\n[7/8] S&P500 / 나스닥 MDD 계산 중...")
    mdd = {
        "SP500": compute_mdd_series(fred_macro["SP500"]),
        "NASDAQCOM": compute_mdd_series(fred_macro["NASDAQCOM"]),
    }

    print("\n[8/8] yfinance에서 KOSPI / KOSDAQ150 / VKOSPI 수집 중...")
    kospi = fetch_yfinance_series("^KS11", DAILY_YEARS_BACK)
    kosdaq150 = fetch_yfinance_series("229200.KS", DAILY_YEARS_BACK)
    mdd["KOSPI"] = compute_mdd_series(kospi)
    mdd["KOSDAQ150"] = compute_mdd_series(kosdaq150)

    vkospi = fetch_yfinance_series("^VKOSPI", DAILY_YEARS_BACK)
    if not vkospi:
        print("  ^VKOSPI: 야후 파이낸스에서 데이터를 가져오지 못했습니다 (티커 미지원 가능성).")

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "months": months,
        "countries": COUNTRIES,
        "cli": cli,
        "export_total_1000usd": export_total,
        "export_daily_avg_1000usd": export_daily_avg,
        "categories": list(CATEGORIES.keys()),
        "product_months": product_months,
        "product_1000usd": product,
        "fred_macro": fred_macro,
        "vkospi": vkospi,
        "equities": {"SP500": fred_macro["SP500"], "NASDAQCOM": fred_macro["NASDAQCOM"],
                     "KOSPI": kospi, "KOSDAQ150": kosdaq150},
        "mdd": mdd,
    }

    with open("data.js", "w", encoding="utf-8") as f:
        f.write("window.MACRO_DATA = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";")

    print("\n완료: data.js 생성됨.")


if __name__ == "__main__":
    main()
