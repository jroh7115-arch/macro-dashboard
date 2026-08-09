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
import io
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
# 각 값은 산업통상자원부 '수출입 동향'이 발표한 품목 비중을 재현하는지 대조해
# 고른 것이다. 괄호 안은 2026-06 기준 (이 조합 비중 vs 산업부 발표 비중).
CATEGORY_HS = {
    "반도체": ("8541", "8542", "8473"),   # 43.72% vs 43.8% (2026-05도 42.24% vs 42.3%)
    "자동차": ("8703", "8704"),           #  6.51% vs 6.6%  (8703만 쓰면 6.23%로 더 벌어짐)
    "컴퓨터": ("8523",),                  #  5.15% vs 4.8%  (SSD 등 저장매체)
    "석유제품": ("27",),                  #  5.54% vs 5.5%
    "선박": ("8901",),                    #  2.60% vs 2.8%
    "철강제품": ("72",),                  #  1.93% vs 2.1%  (예전 7208만 쓰던 0.41%에서 교정)
    "이차전지": ("8507",),                #  0.68% vs 0.7%
    # 산업부의 '일반기계'와 '석유화학'은 넣지 않았다. MTI 분류가 HS 류와 깔끔히
    # 대응하지 않아 가장 근접한 조합도 오차가 크다(일반기계: 84류에서 컴퓨터·부품을
    # 뺀 값이 5.40% vs 발표 4.0%, 석유화학: 39류 3.22% / 29+39류 4.89% vs 발표 4.0%).
    # 억지로 맞추면 비중이 틀린 채로 표시되므로 뺀다.
}
DEFAULT_CATEGORIES = list(CATEGORY_HS)

# 품목 분류 기준이 바뀌면 과거 달도 새 기준으로 다시 받아야 그래프에 단차가 생기지
# 않는다. 관세청 API가 자주 끊기므로 한 번에 다 받지 않고, 실행마다 아직 새 기준으로
# 바뀌지 않은 달을 조금씩 채워 나간다(product_basis_months에 기록).
CATEGORY_BASIS = "motie-7cat-v1"
# 분류 기준을 바꾼 뒤 과거 달을 다시 받을 때 한 실행에서 처리할 개월 수.
# 한 달치가 2MB가 넘어서 수십 개월을 몰아 받으면 관세청 서버가 그 IP의 접속을
# 한동안 끊어버린다(2026-08-09에 70개월을 세 번 시도했다가 몇 시간 동안
# ConnectTimeout만 났다. 같은 시각 한국에서는 0.1초 만에 정상 응답). 그래서
# 기본값을 작게 두고, 여러 번의 정기 실행에 나눠 채운다.
BACKFILL_PER_RUN = int(os.environ.get("BACKFILL_PER_RUN") or 8)

# 조업일수 계산 기준이 바뀌면 1일평균 수출액 전 구간을 새 기준으로 다시 계산해야
# 한다. data.js에 이 값을 같이 저장해 두고, 코드의 기준과 다르면 전체 재계산한다.
WORKDAY_BASIS = "official-sat-half-v1"

CUSTOMS_URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
# 관세청은 잠정치를 뒤에 확정치로 개정하므로, 매 실행마다 최근 몇 달치를 다시
# 받아 덮어쓴다. 그보다 오래된 달은 기존 값을 그대로 둔다.
# 한 달치 응답이 2MB가 넘고 이 API가 간헐적으로 접속을 거부하기 때문에, 개정이
# 실제로 일어나는 최근 3개월만 다시 받아 서버 부담을 줄인다.
EXPORT_REFRESH_MONTHS = 3

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
    # 아래 3개는 연준 H.10/EIA 계열이라 매주 한 번씩 묶어서 공표된다(예: 일요일에
    # 조회하면 최신값이 지난 금요일이 아니라 그 전 금요일). 차트에는 매일 갱신되는
    # 야후 파이낸스 값을 쓰고, 이 시리즈들은 장기 비교용으로만 남겨둔다.
    "DEXKOUS": "원/달러 환율(연준 H.10, 주간 공표)",
    "DTWEXBGS": "달러인덱스 무역가중 Broad(연준 H.10, 주간 공표)",
    "DCOILWTICO": "WTI 유가(EIA, 주간 공표)",
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
    """관세청·산업통상자원부가 쓰는 조업일수.

    공식 = 평일(법정공휴일과 근로자의 날 제외) + 토요일 × 0.5.
    토요일을 반일로 치는 것이 핵심으로, 이걸 빼면 1일평균 수출액이 7~11%
    부풀려진다. 산업부 '수출입 동향'이 발표한 일평균 수출액에서 역산한
    조업일수(2026년 3~7월 5개월)와 이 공식이 모두 일치하는 것을 확인했다.
    """
    import holidays

    kr = holidays.KR(years=year)
    n = calendar.monthrange(year, month)[1]
    days = [date(year, month, d) for d in range(1, n + 1)]
    labor_day = date(year, 5, 1)  # 근로자의 날: 공휴일은 아니지만 조업일에서 빠진다
    weekdays = [d for d in days if d.weekday() < 5 and d not in kr and d != labor_day]
    saturdays = [d for d in days if d.weekday() == 5 and d not in kr and d != labor_day]
    return len(weekdays) + 0.5 * len(saturdays)


def customs_request(yymm: str, attempts: int = 3):
    """관세청 API 호출. 서버가 간헐적으로 접속 타임아웃을 내므로 재시도한다.

    https가 막히는 경우도 관측돼 마지막 시도는 http로 넘어간다.
    """
    params = {
        "serviceKey": CUSTOMS_SERVICE_KEY,
        "strtYymm": yymm,
        "endYymm": yymm,
        "hsSgn": "",
    }
    last_err = None
    for i in range(attempts):
        url = CUSTOMS_URL if i < attempts - 1 else CUSTOMS_URL.replace("https://", "http://")
        try:
            res = requests.get(url, params=params, timeout=(10, 90))
            res.raise_for_status()
            return res
        except Exception as e:
            last_err = e
            print(f"    시도 {i+1}/{attempts} 실패({type(e).__name__}), 재시도 대기...")
            time.sleep(5 * (i + 1))
    raise last_err


def fetch_customs_month(yymm: str):
    """한 달치 전체 HS코드 수출실적을 받아 (총액USD, {품목: 금액USD})로 정리.

    아직 발표 전인 달은 빈 응답이 오므로 None을 돌려준다.
    """
    res = customs_request(yymm)
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
        implied = total / avg  # 조업일수는 토요일 반일 때문에 .5 단위가 나온다
        calc = korea_working_days(int(ym[:4]), int(ym[4:]))
        if abs(implied - calc) > 0.02:
            mismatches.append((ym, round(implied, 2), calc))
    if mismatches:
        print(f"  [경고] 조업일수 계산이 기존 데이터 {len(mismatches)}개월과 어긋납니다:")
        for ym, implied, calc in mismatches[:10]:
            print(f"    {ym}: 기존 {implied}일 vs 지금 계산 {calc}일")
        print("    -> holidays 패키지 버전이 바뀌었을 수 있습니다. 워크플로의 버전 고정을 "
              "확인하세요. 이대로 두면 1일평균 수출액에 인위적인 단차가 생깁니다.")
        return False
    print(f"  조업일수 계산 검증: 기존 {checked}개월과 모두 일치")
    return True


# ------------------------------------------------------------------
# 수출 속보치 (익월 1일 시점의 월 총수출액)
# ------------------------------------------------------------------
# 품목별 API(Itemtrade)는 HS코드별 확정치라 익월 15일경에야 채워지지만,
# 수출입총괄 API(Newtrade)는 같은 시점에 이미 그 달 총액을 갖고 있다.
# (2026-08-09 확인: Itemtrade는 202607이 빈 응답, Newtrade는 988.87억달러 반환)
# 응답도 500바이트대라 가볍고 기존 키로 그대로 인증된다. 그래서 속보 총액은
# 이 API에서 받고, 실패할 때만 KDI 스크래핑으로 넘어간다.
CUSTOMS_TOTAL_URL = "https://apis.data.go.kr/1220000/Newtrade/getNewtradeList"


def fetch_customs_total(yymm: str):
    """수출입총괄 API에서 그 달 총수출액(달러)을 받아온다. 없으면 None."""
    last_err = None
    for i in range(3):
        url = CUSTOMS_TOTAL_URL if i < 2 else CUSTOMS_TOTAL_URL.replace("https://", "http://")
        try:
            res = requests.get(
                url,
                params={"serviceKey": CUSTOMS_SERVICE_KEY, "strtYymm": yymm, "endYymm": yymm},
                timeout=(10, 60),
            )
            res.raise_for_status()
            root = ET.fromstring(res.text)
            if root.findtext(".//resultCode") not in (None, "00"):
                print(f"    총괄 API 오류: {root.findtext('.//resultMsg')}")
                return None
            for item in root.findall(".//item"):
                if item.findtext("year") == "총계":
                    continue
                try:
                    return float(item.findtext("expDlr") or 0)
                except ValueError:
                    return None
            return None      # 아직 미발표
        except Exception as e:
            last_err = e
            print(f"    총괄 API 시도 {i+1}/3 실패({type(e).__name__})")
            time.sleep(5 * (i + 1))
    raise last_err


# 총괄 API가 막혔을 때의 대비책. 산업부 홈페이지(motir.go.kr)는 숫자가 HWP/PDF
# 첨부에만 있고 첨부 직접 다운로드가 404로 막혀 있어 쓸 수 없다. 대신 KDI
# 경제정보센터가 같은 보도자료 요약을 HTML 본문으로 싣기 때문에 거기서 읽는다.
KDI_LIST = "https://eiec.kdi.re.kr/policy/materialList.do?pp=100&pg={pg}"
KDI_VIEW = "https://eiec.kdi.re.kr/policy/materialView.do?num={num}"
KDI_UA = {"User-Agent": "Mozilla/5.0 (compatible; macro-dashboard/1.0)"}
KDI_MAX_PAGES = 8


def _kdi_text(url):
    res = requests.get(url, timeout=60, headers=KDI_UA)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"
    html = re.sub(r"<script.*?</script>", "", res.text, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)), re.sub(r"\s+", " ", res.text)


# 산업부 보도자료 표의 품목명 -> 이 대시보드의 품목명
MOTIE_ITEM_MAP = {
    "반도체": "반도체", "자동차": "자동차", "컴퓨터": "컴퓨터",
    "석유제품": "석유제품", "선박": "선박", "철강": "철강제품", "이차전지": "이차전지",
}
KDI_DOWNLOAD = "https://eiec.kdi.re.kr/policy/callDownload.do?num={num}&filenum=1"
_ITEM_NAME_RE = re.compile(r"^[가-힣·A-Za-z()\s]+$")
_ITEM_VAL_RE = re.compile(r"^(?:[\d,]+\s*\([+\-−△▲]?[\d.]+\)\s*)+$")


def fetch_preliminary_products(num: str):
    """보도자료 PDF의 '20대 주요품목별 수출액' 표에서 품목별 수출액(달러)을 뽑는다.

    산업부 홈페이지는 첨부 다운로드가 막혀 있지만 KDI는 같은 PDF를 내려준다.
    표는 '품목명들 한 줄 / 값들 한 줄'이 반복되는 구조이고, 값은 억달러 정수와
    증감률이 '410 (+179)' 형태로 붙어 있다(감소는 △로 표기).
    """
    try:
        import pdfplumber
    except ImportError:
        print("    pdfplumber가 없어 품목별 속보는 건너뜁니다.")
        return {}
    try:
        res = requests.get(KDI_DOWNLOAD.format(num=num), timeout=180, headers=KDI_UA)
        res.raise_for_status()
        if res.content[:4] != b"%PDF":
            print("    KDI 첨부가 PDF가 아닙니다. 품목별 속보를 건너뜁니다.")
            return {}
        with pdfplumber.open(io.BytesIO(res.content)) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:4])
    except Exception as e:
        print(f"    보도자료 PDF 처리 실패({type(e).__name__}) - 품목별 속보 건너뜀")
        return {}

    m = re.search(r"20대\s*주요품목별\s*수출액\(억달러\)\s*및\s*증감률\(%\)\s*】(.{0,1200})", text, re.S)
    if not m:
        print("    보도자료에서 품목별 표를 찾지 못했습니다(양식 변경 가능성).")
        return {}
    lines = [l.strip() for l in m.group(1).split("\n") if l.strip()]
    raw = {}
    for i in range(len(lines) - 1):
        if _ITEM_NAME_RE.match(lines[i]) and _ITEM_VAL_RE.match(lines[i + 1]):
            names = lines[i].split()
            vals = re.findall(r"([\d,]+)\s*\(([+\-−△▲]?)([\d.]+)\)", lines[i + 1])
            if len(names) == len(vals):
                for name, (amount, _sign, _rate) in zip(names, vals):
                    raw[name] = float(amount.replace(",", "")) * 1e8   # 억달러 -> 달러

    out = {cat: raw[src] for src, cat in MOTIE_ITEM_MAP.items() if src in raw}
    if out:
        print(f"    품목별 속보 {len(out)}개 확보 "
              f"(반도체 {out.get('반도체', 0)/1e8:,.0f}억달러)")
    return out


def fetch_preliminary_export(yymm: str):
    """산업부 수출입 동향 속보에서 (총수출 달러, YoY %)를 읽어온다. 없으면 None."""
    year, month = int(yymm[:4]), int(yymm[4:])
    title_pat = re.compile(rf"{year}년\s*{month}월\s*수출입\s*동향")
    num = None
    for pg in range(1, KDI_MAX_PAGES + 1):
        try:
            _, raw = _kdi_text(KDI_LIST.format(pg=pg))
        except Exception as e:
            print(f"    KDI 목록 {pg}쪽 실패({type(e).__name__})")
            return None
        m = title_pat.search(raw)
        if m:
            # 목록에서 제목 바로 앞에 그 글의 상세 링크가 온다
            before = re.findall(r"materialView\.do\?num=(\d+)", raw[: m.start()])
            num = before[-1] if before else None
            break
    if not num:
        print(f"    KDI에서 {year}년 {month}월 수출입 동향 글을 찾지 못했습니다(아직 미발표).")
        return None

    items = fetch_preliminary_products(num)
    body, _ = _kdi_text(KDI_VIEW.format(num=num))
    if not title_pat.search(body):
        print(f"    KDI 글 num={num}의 제목이 예상과 다릅니다. 건너뜁니다.")
        return None
    amount = re.search(r"수출은.{0,80}?([\d,]+\.\d)\s*억\s*달러", body)
    yoy = re.search(r"전년\s*동월\s*대비\s*([\d.]+)\s*%\s*(증가|감소)", body)
    if not amount:
        print(f"    KDI 글 num={num}에서 수출액을 찾지 못했습니다(문구 변경 가능성).")
        return None
    total_usd = float(amount.group(1).replace(",", "")) * 1e8
    yoy_pct = None
    if yoy:
        yoy_pct = float(yoy.group(1)) * (1 if yoy.group(2) == "증가" else -1)
    # 한국 월 수출이 400억달러를 밑돌 일은 없으므로, 엉뚱한 숫자를 잡았는지 방어한다.
    if not (4e10 < total_usd < 3e11):
        print(f"    KDI에서 읽은 수출액 {total_usd:,.0f}달러가 비정상이라 무시합니다.")
        return None
    print(f"    속보 총수출 {total_usd/1e8:,.1f}억달러 (YoY {yoy_pct:+.1f}%) [KDI num={num}]")
    return total_usd, yoy_pct, items


def update_export_data(existing):
    """기존 수출 데이터에 최근 몇 달치를 관세청 API로 새로 받아 덮어쓴다."""
    total_1000 = dict(existing.get("export_total_1000usd", {}))
    daily_1000 = dict(existing.get("export_daily_avg_1000usd", {}))
    products = {
        cat: dict(existing.get("product_1000usd", {}).get(cat, {}))
        for cat in CATEGORY_HS
    }

    # 품목 분류 기준이 바뀌었으면 기존 달들의 기준 기록을 버리고 처음부터 다시 채운다.
    basis_ok = set(existing.get("product_basis_months", []))
    if existing.get("product_basis") != CATEGORY_BASIS:
        if basis_ok:
            print(f"  품목 분류 기준이 '{CATEGORY_BASIS}'로 바뀌어 과거 달을 다시 받습니다.")
        basis_ok = set()

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
        # 아직 새 분류 기준으로 받지 않은 과거 달을 최신 달부터 조금씩 채운다.
        pending = [ym for ym in sorted(total_1000, reverse=True)
                   if ym not in basis_ok and ym not in targets]
        if pending:
            print(f"  새 분류 기준으로 다시 받을 과거 달 {len(pending)}개월 중 "
                  f"이번 실행에서 {min(len(pending), BACKFILL_PER_RUN)}개월 처리")
            targets.extend(pending[:BACKFILL_PER_RUN])
        # 조업일수 기준 자체를 바꾼 경우에는 기존 값과 어긋나는 게 당연하므로
        # 검증을 건너뛰고 전 구간을 새 기준으로 다시 계산한다.
        basis_changed = existing.get("export_workday_basis") != WORKDAY_BASIS
        if basis_changed:
            print(f"  조업일수 기준이 '{WORKDAY_BASIS}'로 바뀌어 전 구간을 다시 계산합니다.")
            consistent = True
        else:
            consistent = check_workday_consistency(total_1000, daily_1000, set(targets))
        failures = 0
        for yymm in sorted(targets):
            if failures >= 2:
                print(f"  - {yymm}: 관세청 API가 응답하지 않아 이번 실행은 건너뜁니다 (기존 값 유지)")
                continue
            print(f"  - {yymm}")
            try:
                got = fetch_customs_month(yymm)
            except Exception as e:
                # 서버가 죽어 있으면 남은 달도 마찬가지라 계속 두드리지 않는다.
                failures += 1
                print(f"    수집 실패({type(e).__name__}) - 기존 값 유지")
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
            basis_ok.add(yymm)   # 이 달은 현재 품목 분류 기준으로 받았다
            print(
                f"    총액 {total_usd/1e8:,.1f}억달러, 조업일수 {wd}일, "
                f"반도체 {cats['반도체']/1e8:,.1f}억달러"
            )
            # 한 달치가 2MB가 넘는다. 짧은 간격으로 수십 번 당기면 이 서버가 해당
            # IP의 접속을 한동안 끊어버리는 것으로 보여(한국에서는 같은 시각에도
            # 0.1초 만에 정상 응답), 달 사이에 충분히 쉬어 간다.
            time.sleep(3)

        # 1일평균은 총액/조업일수로 유도되는 값이라, 검증을 통과했을 때는 전 구간을
        # 한 기준으로 다시 계산해 둔다. API가 일시적으로 죽어 일부 달만 갱신되는
        # 경우에도 시리즈가 서로 다른 기준으로 섞이지 않는다.
        if consistent:
            fixed = 0
            for ym, total in total_1000.items():
                wd = korea_working_days(int(ym[:4]), int(ym[4:]))
                new_avg = round(total / wd, 1) if wd else None
                if daily_1000.get(ym) != new_avg:
                    daily_1000[ym] = new_avg
                    fixed += 1
            if fixed:
                print(f"  1일평균 수출액 {fixed}개월을 현재 조업일수 기준으로 재계산했습니다.")

    # 확정치가 아직 없는 최근 달을 속보치로 채운다. 확정치가 들어오면 그 달은
    # 위 루프에서 덮어써지고 속보 딱지도 사라진다.
    prelim = {k: v for k, v in existing.get("export_preliminary", {}).items()
              if k not in basis_ok}
    now = datetime.now()
    y, m = now.year, now.month
    for _ in range(3):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        ym = f"{y:04d}{m:02d}"
        if ym in basis_ok:
            continue          # 품목별 확정치가 이미 들어온 달
        if ym in total_1000 and ym not in prelim:
            continue          # 예전 확정치가 남아 있는 달
        # 속보 총액도 확정 전까지 계속 개정되므로, 이미 받아둔 달도 매번 다시 받는다
        print(f"  - {ym} 속보치 조회")
        total_usd = None
        try:
            total_usd = fetch_customs_total(ym)
            if total_usd:
                print(f"    속보 총수출 {total_usd/1e8:,.1f}억달러 [관세청 수출입총괄 API]")
        except Exception as e:
            print(f"    총괄 API 실패({type(e).__name__}) - KDI로 대체 시도")
        prelim_items = {}
        try:
            got = fetch_preliminary_export(ym)
            if got:
                # 총액은 관세청 API 값을 우선하고(정밀도가 높다), 품목별은
                # 보도자료에만 있으므로 여기서만 얻는다.
                if not total_usd:
                    total_usd = got[0]
                prelim_items = got[2] or {}
        except Exception as e:
            print(f"    KDI 속보 수집 실패({type(e).__name__}: {e})")
        if total_usd:
            wd = korea_working_days(int(ym[:4]), int(ym[4:]))
            total_1000[ym] = round(total_usd / 1000, 1)
            daily_1000[ym] = round(total_usd / 1000 / wd, 1) if wd else None
            for cat, val in prelim_items.items():
                if cat in products:
                    products[cat][ym] = val
            prelim[ym] = True
    if prelim:
        print(f"  속보치로 채운 달: {sorted(prelim)}")

    remaining = [ym for ym in total_1000 if ym not in basis_ok]
    if remaining:
        print(f"  아직 이전 품목 분류 기준인 달: {len(remaining)}개월 "
              f"(다음 실행에서 계속 채웁니다)")
    months_sorted = sorted(total_1000)
    if months_sorted:
        print(f"  수출총액 보유 구간: {months_sorted[0]} ~ {months_sorted[-1]} ({len(months_sorted)}개월)")
    product_months = sorted(set().union(*[set(v) for v in products.values()])) if products else []
    return {
        "export_total_1000usd": total_1000,
        "export_daily_avg_1000usd": daily_1000,
        "export_workday_basis": WORKDAY_BASIS,
        # 속보치로만 채워진 달 (확정치가 들어오면 목록에서 빠진다)
        "export_preliminary": {k: True for k in sorted(prelim)},
        "product_basis": CATEGORY_BASIS,
        "product_basis_months": sorted(basis_ok),
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
    # 환율·달러인덱스·유가는 FRED(연준 H.10, EIA)가 주 1회 묶어서 내보내 최대 일주일까지
    # 묵은 값이 된다. 매일 갱신되는 시장 시세로 대신 보여준다.
    usdkrw = fetch_yfinance_series("KRW=X", DAILY_YEARS_BACK)
    dxy = fetch_yfinance_series("DX-Y.NYB", DAILY_YEARS_BACK)
    wti = fetch_yfinance_series("CL=F", DAILY_YEARS_BACK)
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
        # 매일 갱신되는 시장 시세 (환율·달러인덱스·유가)
        "market_daily": {"USDKRW": usdkrw, "DXY": dxy, "WTI": wti},
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
