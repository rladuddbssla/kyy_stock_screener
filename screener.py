"""
KYY Stock Screener
매일 자동으로 KOSPI/KOSDAQ 종목을 스크리닝하여 index.html을 생성합니다.
"""

import json
import warnings
from datetime import datetime, timedelta

import pandas as pd
from pykrx import stock

warnings.filterwarnings("ignore")

TODAY = datetime.today().strftime("%Y%m%d")
YESTERDAY = (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")
START_DATE = (datetime.today() - timedelta(days=365)).strftime("%Y%m%d")  # 52주


# ──────────────────────────────────────────────
# 1. 데이터 수집
# ──────────────────────────────────────────────

def get_fundamental(market: str) -> pd.DataFrame:
    """PER, PBR, ROE, 배당수익률 수집"""
    df = stock.get_market_fundamental(TODAY, market=market)
    df.index.name = "ticker"
    df = df.reset_index()
    df["market"] = market
    return df


def get_market_cap(market: str) -> pd.DataFrame:
    """시가총액 및 종목명 수집"""
    df = stock.get_market_cap(TODAY, market=market)
    df.index.name = "ticker"
    df = df.reset_index()
    df = df[["ticker", "시가총액", "상장주식수"]]
    return df


def get_ticker_names(tickers: list) -> dict:
    """종목코드 → 종목명 딕셔너리"""
    result = {}
    for t in tickers:
        try:
            result[t] = stock.get_market_ticker_name(t)
        except Exception:
            result[t] = t
    return result


def get_52week_range(ticker: str) -> tuple:
    """52주 최고/최저가 반환"""
    try:
        df = stock.get_market_ohlcv(START_DATE, TODAY, ticker)
        if df.empty:
            return None, None
        return df["저가"].min(), df["고가"].max()
    except Exception:
        return None, None


def get_net_buying(market: str) -> pd.DataFrame:
    """외국인·기관 순매수 (당일)"""
    try:
        df = stock.get_market_trading_volume_by_investor(TODAY, TODAY, market)
        return df
    except Exception:
        return pd.DataFrame()


def get_foreign_institution_daily(market: str) -> pd.DataFrame:
    """외국인·기관 순매수 종목별 (당일)"""
    try:
        df = stock.get_market_net_purchases_of_equities(TODAY, TODAY, market, "외국인")
        df.index.name = "ticker"
        df = df.reset_index()
        df = df.rename(columns={"순매수거래량": "foreign_net_vol", "순매수거래대금": "foreign_net_val"})
        df2 = stock.get_market_net_purchases_of_equities(TODAY, TODAY, market, "기관합계")
        df2.index.name = "ticker"
        df2 = df2.reset_index()
        df2 = df2.rename(columns={"순매수거래량": "inst_net_vol", "순매수거래대금": "inst_net_val"})
        merged = df[["ticker", "foreign_net_val"]].merge(df2[["ticker", "inst_net_val"]], on="ticker", how="outer")
        return merged
    except Exception:
        return pd.DataFrame()


# ──────────────────────────────────────────────
# 2. 스크리닝 로직
# ──────────────────────────────────────────────

def build_master() -> pd.DataFrame:
    """KOSPI + KOSDAQ 통합 마스터 데이터프레임 생성"""
    frames = []
    for market in ["KOSPI", "KOSDAQ"]:
        fund = get_fundamental(market)
        cap = get_market_cap(market)
        merged = fund.merge(cap, on="ticker", how="inner")
        frames.append(merged)
    df = pd.concat(frames, ignore_index=True)

    # 종목명 추가
    names = get_ticker_names(df["ticker"].tolist())
    df["name"] = df["ticker"].map(names)

    # 시가총액 억원 환산
    df["cap_억"] = (df["시가총액"] / 1e8).round(0).astype(int)

    # 불필요 컬럼 정리
    df = df.rename(columns={"BPS": "bps", "PER": "per", "PBR": "pbr",
                             "EPS": "eps", "DIV": "div", "DPS": "dps"})
    return df


def screen_value(df: pd.DataFrame) -> pd.DataFrame:
    """저평가 스크리닝: 저PBR + 적정ROE + 적정PER"""
    mask = (
        (df["pbr"] > 0) & (df["pbr"] < 0.7) &
        (df["per"] > 0) & (df["per"] < 15) &
        (df["ROE"] > 5) &
        (df["cap_억"] > 300)   # 소형주 제외
    )
    return df[mask].sort_values("pbr").head(30)


def screen_growth(df: pd.DataFrame) -> pd.DataFrame:
    """성장주 스크리닝: 높은 ROE + 적정 PER"""
    mask = (
        (df["ROE"] > 15) &
        (df["per"] > 0) & (df["per"] < 25) &
        (df["pbr"] > 0) &
        (df["cap_억"] > 500)
    )
    return df[mask].sort_values("ROE", ascending=False).head(30)


def screen_dividend(df: pd.DataFrame) -> pd.DataFrame:
    """배당주 스크리닝: 배당수익률 높은 종목"""
    mask = (
        (df["div"] > 3.0) &
        (df["pbr"] > 0) & (df["pbr"] < 2) &
        (df["cap_억"] > 500)
    )
    return df[mask].sort_values("div", ascending=False).head(30)


def add_52week(df: pd.DataFrame) -> pd.DataFrame:
    """52주 신저가 근접 비율 계산 (시간 소요 주의 — 상위 종목만)"""
    rows = []
    for _, row in df.iterrows():
        low52, high52 = get_52week_range(row["ticker"])
        if low52 and high52 and high52 > 0:
            # 현재가 ≈ BPS × PBR (근사치)
            cur_price = row["bps"] * row["pbr"] if row["bps"] > 0 else None
            near_low = round((cur_price - low52) / (high52 - low52) * 100, 1) if cur_price else None
        else:
            near_low = None
        rows.append(near_low)
    df = df.copy()
    df["52w_위치(%)"] = rows
    return df


# ──────────────────────────────────────────────
# 3. HTML 생성
# ──────────────────────────────────────────────

TABLE_CSS = """
<style>
  :root { --bg: #0f1117; --card: #1a1d27; --border: #2a2d3e; --text: #e2e8f0;
          --muted: #8892a4; --accent: #6c8ebf; --green: #4ade80; --red: #f87171;
          --amber: #fbbf24; --purple: #a78bfa; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Pretendard', -apple-system, sans-serif;
         font-size: 14px; padding: 0 0 60px; }
  header { background: var(--card); border-bottom: 1px solid var(--border);
           padding: 18px 24px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .updated { font-size: 12px; color: var(--muted); margin-left: auto; }
  .tabs { display: flex; gap: 4px; padding: 16px 24px 0; border-bottom: 1px solid var(--border); }
  .tab { padding: 8px 18px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 13px;
         color: var(--muted); background: transparent; border: none; transition: all .15s; }
  .tab.active { background: var(--card); color: var(--text); font-weight: 500; }
  .tab:hover { color: var(--text); }
  .panel { display: none; padding: 20px 24px; }
  .panel.active { display: block; }
  .section-title { font-size: 13px; color: var(--muted); margin-bottom: 10px; letter-spacing: .04em; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 11px; color: var(--muted); font-weight: 500;
       padding: 7px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,.03); }
  .ticker { font-family: monospace; font-size: 12px; color: var(--muted); }
  .name { font-weight: 500; }
  .num { text-align: right; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 500; }
  .badge.kospi { background: rgba(108,142,191,.15); color: var(--accent); }
  .badge.kosdaq { background: rgba(167,139,250,.15); color: var(--purple); }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neutral { color: var(--muted); }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                  gap: 12px; margin-bottom: 20px; }
  .summary-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
                  padding: 16px; }
  .summary-card .label { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
  .summary-card .value { font-size: 22px; font-weight: 600; }
</style>
"""

def _badge(market: str) -> str:
    cls = "kospi" if market == "KOSPI" else "kosdaq"
    return f'<span class="badge {cls}">{market}</span>'


def _fmt_per(v):
    if pd.isna(v) or v <= 0:
        return '<span class="neutral">-</span>'
    return f"{v:.1f}"


def _fmt_pbr(v):
    if pd.isna(v) or v <= 0:
        return '<span class="neutral">-</span>'
    cls = "pos" if v < 0.5 else "neutral"
    return f'<span class="{cls}">{v:.2f}</span>'


def _fmt_roe(v):
    if pd.isna(v):
        return '<span class="neutral">-</span>'
    cls = "pos" if v > 10 else "neutral" if v > 0 else "neg"
    return f'<span class="{cls}">{v:.1f}%</span>'


def _fmt_div(v):
    if pd.isna(v) or v <= 0:
        return '<span class="neutral">-</span>'
    cls = "pos" if v >= 3 else "neutral"
    return f'<span class="{cls}">{v:.1f}%</span>'


def _fmt_cap(v):
    if v >= 10000:
        return f"{v/10000:.1f}조"
    return f"{v:,}억"


def make_table_value(df: pd.DataFrame) -> str:
    rows = ""
    for _, r in df.iterrows():
        rows += f"""
        <tr>
          <td><span class="ticker">{r['ticker']}</span></td>
          <td class="name">{r.get('name', '')}</td>
          <td>{_badge(r['market'])}</td>
          <td class="num">{_fmt_pbr(r['pbr'])}</td>
          <td class="num">{_fmt_per(r['per'])}</td>
          <td class="num">{_fmt_roe(r['ROE'])}</td>
          <td class="num">{_fmt_div(r['div'])}</td>
          <td class="num">{_fmt_cap(r['cap_억'])}</td>
        </tr>"""
    return f"""
    <table>
      <thead><tr>
        <th>코드</th><th>종목명</th><th>시장</th>
        <th class="num">PBR</th><th class="num">PER</th>
        <th class="num">ROE</th><th class="num">배당</th>
        <th class="num">시가총액</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def make_table_growth(df: pd.DataFrame) -> str:
    rows = ""
    for _, r in df.iterrows():
        rows += f"""
        <tr>
          <td><span class="ticker">{r['ticker']}</span></td>
          <td class="name">{r.get('name', '')}</td>
          <td>{_badge(r['market'])}</td>
          <td class="num">{_fmt_roe(r['ROE'])}</td>
          <td class="num">{_fmt_per(r['per'])}</td>
          <td class="num">{_fmt_pbr(r['pbr'])}</td>
          <td class="num">{_fmt_cap(r['cap_억'])}</td>
        </tr>"""
    return f"""
    <table>
      <thead><tr>
        <th>코드</th><th>종목명</th><th>시장</th>
        <th class="num">ROE</th><th class="num">PER</th>
        <th class="num">PBR</th><th class="num">시가총액</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def make_table_dividend(df: pd.DataFrame) -> str:
    rows = ""
    for _, r in df.iterrows():
        rows += f"""
        <tr>
          <td><span class="ticker">{r['ticker']}</span></td>
          <td class="name">{r.get('name', '')}</td>
          <td>{_badge(r['market'])}</td>
          <td class="num">{_fmt_div(r['div'])}</td>
          <td class="num">{_fmt_pbr(r['pbr'])}</td>
          <td class="num">{_fmt_roe(r['ROE'])}</td>
          <td class="num">{_fmt_cap(r['cap_억'])}</td>
        </tr>"""
    return f"""
    <table>
      <thead><tr>
        <th>코드</th><th>종목명</th><th>시장</th>
        <th class="num">배당수익률</th><th class="num">PBR</th>
        <th class="num">ROE</th><th class="num">시가총액</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def build_html(value_df, growth_df, dividend_df, run_date: str) -> str:
    val_table = make_table_value(value_df)
    growth_table = make_table_growth(growth_df)
    div_table = make_table_dividend(dividend_df)

    summary_cards = f"""
    <div class="summary-grid">
      <div class="summary-card">
        <div class="label">저평가 스크리닝 결과</div>
        <div class="value">{len(value_df)}<span style="font-size:14px;color:var(--muted)"> 종목</span></div>
      </div>
      <div class="summary-card">
        <div class="label">고ROE 성장주</div>
        <div class="value">{len(growth_df)}<span style="font-size:14px;color:var(--muted)"> 종목</span></div>
      </div>
      <div class="summary-card">
        <div class="label">고배당주</div>
        <div class="value">{len(dividend_df)}<span style="font-size:14px;color:var(--muted)"> 종목</span></div>
      </div>
      <div class="summary-card">
        <div class="label">기준일</div>
        <div class="value" style="font-size:16px">{run_date}</div>
      </div>
    </div>
    """

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KYY 주식 스크리너</title>
  {TABLE_CSS}
</head>
<body>
  <header>
    <h1>📈 KYY 주식 스크리너</h1>
    <span class="updated">업데이트: {run_date}</span>
  </header>

  <div style="padding: 16px 24px 0">
    {summary_cards}
  </div>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('value', this)">저평가 스크리닝</button>
    <button class="tab" onclick="switchTab('growth', this)">고ROE 성장주</button>
    <button class="tab" onclick="switchTab('dividend', this)">고배당주</button>
  </div>

  <div id="value" class="panel active">
    <p class="section-title">PBR &lt; 0.7 · PER &lt; 15 · ROE &gt; 5% · 시총 300억↑ (PBR 오름차순)</p>
    {val_table}
  </div>

  <div id="growth" class="panel">
    <p class="section-title">ROE &gt; 15% · PER &lt; 25 · 시총 500억↑ (ROE 내림차순)</p>
    {growth_table}
  </div>

  <div id="dividend" class="panel">
    <p class="section-title">배당수익률 &gt; 3% · PBR &lt; 2 · 시총 500억↑ (배당 내림차순)</p>
    {div_table}
  </div>

  <script>
    function switchTab(id, btn) {{
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      btn.classList.add('active');
    }}
  </script>
</body>
</html>"""


# ──────────────────────────────────────────────
# 4. 실행
# ──────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 데이터 수집 시작...")

    master = build_master()
    print(f"  → 전체 종목 수: {len(master)}")

    val = screen_value(master)
    gro = screen_growth(master)
    div = screen_dividend(master)
    print(f"  → 저평가: {len(val)} / 성장: {len(gro)} / 배당: {len(div)}")

    run_date = datetime.today().strftime("%Y년 %m월 %d일")
    html = build_html(val, gro, div, run_date)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] index.html 생성 완료!")
