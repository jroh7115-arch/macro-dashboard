r"""
한국 매크로 대시보드용 raw 데이터 수집 스크립트 - GitHub Actions 전용 "하이브리드" 버전.

이 버전은 FRED(OECD CLI, 미국 금리/M2/VIX/지수/하이일드)와 야후 파이낸스
(KOSPI/KOSDAQ150/VKOSPI)만 수집합니다. KOSIS·관세청 API는 한국 국내 IP에서만
접근이 되는 것으로 확인되어(GitHub Actions 서버=해외 IP라 접속이 타임아웃 남),
이 버전에서는 시도하지 않습니다.

대신 기존 data.js에 이미 들어있는 수출총액/일평균수출액/품목별수출액
(export_total_1000usd / export_daily_avg_1000usd / categories / product_months /
product_1000usd)은 그대로 보존해서 덮어쓰지 않습니다. 그 부분은 로컬 PC에서
fetch_macro_data.py를 실행했을 때만 갱신됩니다.

즉 이 스크립트가 GitHub Actions에서 매달 자동으로 돌면서 CLI/금리/VIX/M2/
WALCL/하이일드/KOSPI/나스닥/S&P500/MDD를 계속 최신으로 유지하고, 수출입 관련
5개 차트만 로컬 PC를 켜서 한 번씩 돌려줘야 최신화됩니다.

*** 로컬 PC에서는 이 파일 대신 fetch_macro_data.py(모든 지표를 다 가져오는
    버전)를 계속 사용하세요. 이 파일은 GitHub Actions 전용입니다. ***

필요한 환경변수 (GitHub 저장소 Settings > Secrets and variables > Actions):
    FRED_API_KEY

사용법 (GitHub Actions 워크플로우 안에서):
    pip install requests yfinance pandas
    python fetch_macro_data_github.py
"""

import json
import os
import re
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

COUNTRIES = {
    "KOR": "한국", "USA": "미국", "CHN": "중국", "JPN": "일본", "DEU": "독일",
    "GBR": "영국", "IND": "인도", "BRA": "브라질", "MEX": "멕시코",
    "FRA": "프랑스", "ITA": "이탈리아", "ESP": "스페인", "IDN": "인도네시아",
}

# 로컬 스크립트와 카테고리 목록을 맞춰두기 위한 기본값(첫 실행 등, 기존
# data.js에서 categories를 못 읽어왔을 때만 사용됨).
DEFAULT_CATEGORIES = ["반도체", "자동차", "이차전지", "선박", "철강판재류"]

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

CLI_MONTHS_BACK = 121      # CLI: 10년
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
# 기존 data.js 읽기 (KOSIS/관세청 필드를 보존하기 위해)
# ------------------------------------------------------------------
def load_existing_data(path="data.js"):
    if not os.path.exists(path):
        print(f"참고: {path} 파일이 없습니다 (최초 실행). 수출입 관련 항목은 빈 값으로 채워집니다.")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"window\.MACRO_DATA\s*=\s*(\{.*\});?\s*$", text, re.DOTALL)
        if not m:
            print(f"경고: {path}에서 JSON 부분을 찾지 못했습니다. 수출입 항목은 빈 값으로 채워집니다.")
            return {}
        return json.loads(m.group(1))
    except Exception as e:
        print(f"경고: {path} 읽기/파싱 실패({e}). 수출입 항목은 빈 값으로 채워집니다.")
        return {}


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
    existing = load_existing_data()

    months = month_range(CLI_MONTHS_BACK)
    daily_start = (datetime.now() - timedelta(days=365 * DAILY_YEARS_BACK)).strftime("%Y-%m-%d")
    print(f"CLI 조회 기간: {months[0]} ~ {months[-1]} ({len(months)}개월, 10년)")
    print(f"일별 지표 조회 시작일: {daily_start} (10년)")

    print("\n[1/4] FRED에서 OECD CLI 수집 중 (10년)...")
    cli = {}
    for code in COUNTRIES:
        print(f"  - {code}")
        cli[code] = fetch_fred_cli(code, CLI_MONTHS_BACK)
        time.sleep(0.2)

    print("\n[2/4] FRED에서 미국 금리·M2·VIX·지수·하이일드 수집 중...")
    fred_macro = {}
    for sid, label in FRED_MACRO_SERIES.items():
        print(f"  - {sid} ({label})")
        fred_macro[sid] = fetch_fred_series(sid, start_date=daily_start)
        time.sleep(0.2)

    print("\n[3/4] S&P500 / 나스닥 MDD 계산 중...")
    mdd = {
        "SP500": compute_mdd_series(fred_macro["SP500"]),
        "NASDAQCOM": compute_mdd_series(fred_macro["NASDAQCOM"]),
    }

    print("\n[4/4] yfinance에서 KOSPI / KOSDAQ150 / VKOSPI 수집 중...")
    kospi = fetch_yfinance_series("^KS11", DAILY_YEARS_BACK)
    kosdaq150 = fetch_yfinance_series("229200.KS", DAILY_YEARS_BACK)
    mdd["KOSPI"] = compute_mdd_series(kospi)
    mdd["KOSDAQ150"] = compute_mdd_series(kosdaq150)

    vkospi = fetch_yfinance_series("^VKOSPI", DAILY_YEARS_BACK)
    if not vkospi:
        print("  ^VKOSPI: 야후 파이낸스에서 데이터를 가져오지 못했습니다 (티커 미지원 가능성).")

    # KOSIS/관세청 관련 필드는 기존 data.js 값을 그대로 보존한다 (이 스크립트는
    # 그 부분을 수집하지 않음. 로컬 PC의 fetch_macro_data.py가 담당).
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "months": months,
        "countries": COUNTRIES,
        "cli": cli,
        "export_total_1000usd": existing.get("export_total_1000usd", {}),
        "export_daily_avg_1000usd": existing.get("export_daily_avg_1000usd", {}),
        "categories": existing.get("categories", DEFAULT_CATEGORIES),
        "product_months": existing.get("product_months", []),
        "product_1000usd": existing.get("product_1000usd", {}),
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

    print("\n완료: data.js 생성됨 (수출입 관련 항목은 기존 값 보존).")


if __name__ == "__main__":
    main()
