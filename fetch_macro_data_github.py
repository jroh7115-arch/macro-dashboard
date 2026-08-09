r"""
한국 매크로 대시보드용 raw 데이터 수집 스크립트 - GitHub Actions 전용.

수집 대상:
  - FRED: OECD CLI, 미국 금리/M2/VIX/지수/하이일드/환율/유가/산업생산 등
  - 야후 파이낸스: KOSPI / KOSDAQ150 / SOX
  - 관세청 품목별 수출입실적 API: 수출총액 / 1일평균 수출액 / 품목별 수출액

과거에는 KOSIS·관세청 API가 한국 국내 IP에서만 접근된다고 보고 수출입 항목을
로컬 PC에서만 갱신했지만, 2026-08 GitHub Actions 러너에서 직접 확인한 결과 두
API 모두 해외 IP에서 정상 응답했습니다(HTTP 200, resultCode 00). 그래서 지금은
수출 데이터도 이 스크립트가 자동으로 갱신합니다.

품목 분류에 쓰는 HS코드는 기존 data.js 값을 그대로 재현하는지 검증해서 확정한
것입니다(2026-03·2026-04 두 달에 대해 오차 0.00%). CATEGORY_HS 참고.

필요한 환경변수 (GitHub 저장소 Settings > Secrets and variables > Actions):
    FRED_API_KEY          (필수)
    CUSTOMS_SERVICE_KEY   (선택 - 없으면 수출 항목은 기존 값을 그대로 보존)

사용법 (GitHub Actions 워크플로우 안에서):
    pip install requests yfinance pandas holidays
    python fetch_macro_data_github.py
"""

import calendar
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

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
# 없으면 수출 항목만 기존 값을 보존하고 나머지는 정상 수집한다.
CUSTOMS_SERVICE_KEY = os.environ.get("CUSTOMS_SERVICE_KEY", "")

COUNTRIES = {
    "KOR": "한국", "USA": "미국", "CHN": "중국", "JPN": "일본", "DEU": "독일",
    "GBR": "영국", "IND": "인도", "BRA": "브라질", "MEX": "멕시코",
    "FRA": "프랑스", "ITA": "이탈리아", "ESP": "스페인", "IDN": "인도네시아",
}

# 품목별 수출액을 묶는 HS코드 접두사.
# 기존 data.js(로컬 fetch_macro_data.py가 만든 값)를 그대로 재현하는지 2026-03·
# 2026-04 두 달로 검증해 확정한 정의로, 두 달 모두 오차 0.00%로 일치했다.
# 선박은 8901(여객선·화물선)만, 철강판재류는 7208(열연강판)만 잡히므로 실제
# 업계 통계보다 과소집계된다 - 기존 시리즈와의 연속성을 위해 그대로 유지한다.
CATEGORY_HS = {
    "반도체": ("8542",),
    "자동차": ("8703",),
    "이차전지": ("8507",),
    "선박": ("8901",),
    "철강판재류": ("7208",),
}
DEFAULT_CATEGORIES = list(CATEGORY_HS)

CUSTOMS_URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
# 관세청은 잠정치를 뒤에 확정치로 개정하므로, 매 실행마다 최근 몇 달치를 다시
# 받아 덮어쓴다. 그보다 오래된 달은 기존 값을 그대로 둔다.
EXPORT_REFRESH_MONTHS = 6

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
    "DEXKOUS": "원/달러 환율",
    "DTWEXBGS": "달러인덱스(무역가중 Broad)",
    "DCOILWTICO": "WTI 유가",
    # ISM PMI 원본은 유료 라이선스라 FRED 제공이 중단되어, 무료로 받을 수 있는
    # 제조업 경기 지표들로 대신한다.
    "INDPRO": "미국 산업생산지수",
    "NEWORDER": "미국 핵심자본재 신규수주(항공기 제외 비국방)",
    "GACDFSA066MSFRBPHI": "필라델피아 연준 제조업지수",
    "GACDISA066MSFRBNY": "뉴욕(엠파이어스테이트) 연준 제조업지수",
}

# YoY(전년동월대비)를 첫 표시월부터 그리려면 12개월 전 값이 필요한 월별 시리즈들.
# 다른 지표보다 13개월 정도 더 과거부터 수집한다.
MONTHLY_YOY_SERIES = {"M2SL", "INDPRO", "NEWORDER"}

CLI_MONTHS_BACK = 122      # CLI: 10년 + 1개월 (전월차가 첫 표시월부터 계산되도록 여유분)
DAILY_YEARS_BACK = 10      # 미국 금리/M2/VIX/지수/WALCL, KOSPI/KOSDAQ150: 10년
M2_EXTRA_DAYS = 400        # YoY 표시 지표(M2/산업생산/신규수주)는 13개월치를 더 수집 (MONTHLY_YOY_SERIES 참고)


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
        import pandas as pd
    except ImportError:
        print("  yfinance가 설치되어 있지 않습니다.")
        return {}
    start = (datetime.now() - timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    if df.empty:
        print(f"  {ticker}: 데이터 없음")
        return {}
    # yfinance 신버전은 단일 티커도 (가격종류, 티커) 2단 컬럼(MultiIndex)으로 반환하는데,
    # 이때 row["Close"]가 스칼라가 아닌 Series로 나와서 최신 pandas에서는 float() 변환이
    # TypeError로 실패하고 전부 None으로 저장된다(KOSPI/KOSDAQ150 차트가 비어 보였던 원인).
    # 컬럼을 1단으로 평탄화해서 어떤 버전 조합에서도 스칼라가 나오게 한다.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        print(f"  {ticker}: Close 컬럼을 찾지 못했습니다 (columns={list(df.columns)})")
        return {}
    out = {}
    for idx, val in df["Close"].items():
        d = idx.strftime("%Y-%m-%d")
        try:
            out[d] = round(float(val), 4)
        except (TypeError, ValueError):
            out[d] = None
    n_valid = sum(1 for v in out.values() if v is not None)
    print(f"  {ticker}: {len(out)}개 중 유효값 {n_valid}개")
    return out


# ------------------------------------------------------------------
# 관세청 품목별 수출입실적 -> 수출총액 / 품목별 수출액 / 1일평균 수출액
# ------------------------------------------------------------------
def korea_working_days(year: int, month: int):
    """토·일요일과 한국 법정공휴일을 뺀 조업일수(추정).

    기존 data.js의 1일평균 수출액 61개월치를 이 방식으로 전부 재현되는지
    확인했고 61/61 모두 일치했으므로, 로컬 스크립트와 같은 계산이다.
    """
    import holidays

    kr = holidays.KR(years=year)
    days = calendar.monthrange(year, month)[1]
    return sum(
        1
        for d in range(1, days + 1)
        if date(year, month, d).weekday() < 5 and date(year, month, d) not in kr
    )


def fetch_customs_month(yymm: str):
    """한 달치 전체 HS코드 수출실적을 받아 (총액USD, {품목: 금액USD})로 정리.

    아직 발표 전인 달은 빈 응답이 오므로 None을 돌려준다.
    """
    res = requests.get(
        CUSTOMS_URL,
        params={
            "serviceKey": CUSTOMS_SERVICE_KEY,
            "strtYymm": yymm,
            "endYymm": yymm,
            "hsSgn": "",
        },
        timeout=120,
    )
    res.raise_for_status()
    root = ET.fromstring(res.text)
    code = root.findtext(".//resultCode")
    if code not in (None, "00"):
        print(f"    API 오류 resultCode={code} ({root.findtext('.//resultMsg')})")
        return None

    by_hs, total_row = {}, None
    for item in root.findall(".//item"):
        year = item.findtext("year")
        hs = (item.findtext("hsCode") or "").strip()
        try:
            exp = float(item.findtext("expDlr") or 0)
        except ValueError:
            continue
        if year == "총계":
            total_row = exp
        elif hs:
            by_hs[hs] = exp

    # 발표 전이면 품목이 거의 없거나 총액이 비정상적으로 작다. 한국 월 수출은
    # 최근 기준 400억달러를 크게 웃돌므로 그 아래면 미발표/부분집계로 본다.
    total = total_row if total_row is not None else sum(by_hs.values())
    if len(by_hs) < 1000 or total < 2e10:
        print(f"    {yymm}: 아직 미발표로 판단 (품목 {len(by_hs)}개, 총액 {total:,.0f}달러)")
        return None

    cats = {
        cat: sum(v for hs, v in by_hs.items() if hs.startswith(prefixes))
        for cat, prefixes in CATEGORY_HS.items()
    }
    return total, cats


def check_workday_consistency(total_1000, daily_1000, skip_months):
    """기존 값에서 역산한 조업일수가 지금 계산한 값과 같은지 확인한다.

    holidays 패키지 버전이 바뀌면 공휴일 판정이 달라져(실제로 0.101에서 근로자의
    날이 추가됨) 새로 쓰는 달만 다른 기준으로 계산되고, 1일평균 수출액 그래프에
    실제 수출과 무관한 단차가 생긴다. 갱신 대상이 아닌(=그대로 남는) 달들로
    검증해 그런 어긋남을 조용히 넘어가지 않도록 한다.
    """
    mismatches = []
    checked = 0
    for ym in sorted(total_1000):
        if ym in skip_months:
            continue
        total, avg = total_1000.get(ym), daily_1000.get(ym)
        if not total or not avg:
            continue
        checked += 1
        implied = round(total / avg)
        calc = korea_working_days(int(ym[:4]), int(ym[4:]))
        if implied != calc:
            mismatches.append((ym, implied, calc))
    if mismatches:
        print(f"  [경고] 조업일수 계산이 기존 데이터 {len(mismatches)}개월과 어긋납니다:")
        for ym, implied, calc in mismatches[:10]:
            print(f"    {ym}: 기존 {implied}일 vs 지금 계산 {calc}일")
        print("    -> holidays 패키지 버전이 바뀌었을 수 있습니다. 워크플로의 버전 고정을 "
              "확인하세요. 이대로 두면 1일평균 수출액에 인위적인 단차가 생깁니다.")
    else:
        print(f"  조업일수 계산 검증: 기존 {checked}개월과 모두 일치")


def update_export_data(existing):
    """기존 수출 데이터에 최근 몇 달치를 관세청 API로 새로 받아 덮어쓴다."""
    total_1000 = dict(existing.get("export_total_1000usd", {}))
    daily_1000 = dict(existing.get("export_daily_avg_1000usd", {}))
    products = {
        cat: dict(existing.get("product_1000usd", {}).get(cat, {}))
        for cat in CATEGORY_HS
    }

    if not CUSTOMS_SERVICE_KEY:
        print("  CUSTOMS_SERVICE_KEY가 없어 수출 데이터는 기존 값을 그대로 둡니다.")
    else:
        now = datetime.now()
        targets = []
        y, m = now.year, now.month
        for _ in range(EXPORT_REFRESH_MONTHS):
            targets.append(f"{y:04d}{m:02d}")
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        check_workday_consistency(total_1000, daily_1000, set(targets))
        for yymm in sorted(targets):
            print(f"  - {yymm}")
            try:
                got = fetch_customs_month(yymm)
            except Exception as e:
                print(f"    수집 실패({type(e).__name__}: {e}) - 기존 값 유지")
                continue
            if got is None:
                continue
            total_usd, cats = got
            year, month = int(yymm[:4]), int(yymm[4:])
            wd = korea_working_days(year, month)
            total_1000[yymm] = round(total_usd / 1000, 1)
            daily_1000[yymm] = round(total_usd / 1000 / wd, 1) if wd else None
            for cat, val in cats.items():
                products[cat][yymm] = val
            print(
                f"    총액 {total_usd/1e8:,.1f}억달러, 조업일수 {wd}일, "
                f"반도체 {cats['반도체']/1e8:,.1f}억달러"
            )
            time.sleep(0.5)

    months_sorted = sorted(total_1000)
    if months_sorted:
        print(f"  수출총액 보유 구간: {months_sorted[0]} ~ {months_sorted[-1]} ({len(months_sorted)}개월)")
    product_months = sorted(set().union(*[set(v) for v in products.values()])) if products else []
    return {
        "export_total_1000usd": total_1000,
        "export_daily_avg_1000usd": daily_1000,
        "categories": DEFAULT_CATEGORIES,
        "product_months": product_months,
        "product_1000usd": products,
    }


def main():
    existing = load_existing_data()

    months = month_range(CLI_MONTHS_BACK)
    daily_start = (datetime.now() - timedelta(days=365 * DAILY_YEARS_BACK)).strftime("%Y-%m-%d")
    print(f"CLI 조회 기간: {months[0]} ~ {months[-1]} ({len(months)}개월, 10년)")
    print(f"일별 지표 조회 시작일: {daily_start} (10년)")

    print("\n[1/5] FRED에서 OECD CLI 수집 중 (10년)...")
    cli = {}
    for code in COUNTRIES:
        print(f"  - {code}")
        cli[code] = fetch_fred_cli(code, CLI_MONTHS_BACK)
        time.sleep(0.2)

    print("\n[2/5] FRED에서 미국 금리·M2·VIX·지수·하이일드 수집 중...")
    # M2·산업생산·신규수주는 YoY를 계산해서 보여주는 지표라 12개월 전 값이 있어야
    # 첫 표시월부터 YoY가 그려진다. 다른 지표보다 13개월 더 과거부터 수집한다
    # (대시보드에서 YoY가 없는 앞쪽 구간은 잘라내고 표시).
    yoy_start = (datetime.now() - timedelta(days=365 * DAILY_YEARS_BACK + M2_EXTRA_DAYS)).strftime("%Y-%m-%d")
    fred_macro = {}
    for sid, label in FRED_MACRO_SERIES.items():
        print(f"  - {sid} ({label})")
        start = yoy_start if sid in MONTHLY_YOY_SERIES else daily_start
        fred_macro[sid] = fetch_fred_series(sid, start_date=start)
        time.sleep(0.2)

    print("\n[3/5] S&P500 / 나스닥 MDD 계산 중...")
    mdd = {
        "SP500": compute_mdd_series(fred_macro["SP500"]),
        "NASDAQCOM": compute_mdd_series(fred_macro["NASDAQCOM"]),
    }

    print("\n[4/5] yfinance에서 KOSPI / KOSDAQ150 / SOX / VKOSPI 수집 중...")
    kospi = fetch_yfinance_series("^KS11", DAILY_YEARS_BACK)
    kosdaq150 = fetch_yfinance_series("229200.KS", DAILY_YEARS_BACK)
    sox = fetch_yfinance_series("^SOX", DAILY_YEARS_BACK)
    mdd["KOSPI"] = compute_mdd_series(kospi)
    mdd["KOSDAQ150"] = compute_mdd_series(kosdaq150)

    vkospi = fetch_yfinance_series("^VKOSPI", DAILY_YEARS_BACK)
    if not vkospi:
        print("  ^VKOSPI: 야후 파이낸스에서 데이터를 가져오지 못했습니다 (티커 미지원 가능성).")

    print("\n[5/5] 관세청에서 수출총액·품목별 수출액 수집 중...")
    exports = update_export_data(existing)

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "months": months,
        "countries": COUNTRIES,
        "cli": cli,
        **exports,
        "fred_macro": fred_macro,
        "vkospi": vkospi,
        "equities": {"SP500": fred_macro["SP500"], "NASDAQCOM": fred_macro["NASDAQCOM"],
                     "KOSPI": kospi, "KOSDAQ150": kosdaq150, "SOX": sox},
        "mdd": mdd,
    }

    with open("data.js", "w", encoding="utf-8") as f:
        f.write("window.MACRO_DATA = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";")

    print("\n완료: data.js 생성됨 (수출입 관련 항목은 기존 값 보존).")


if __name__ == "__main__":
    main()
