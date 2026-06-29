"""
KYY Stock Screener
FinanceDataReader + pykrx 조합으로 KRX 로그인 없이 데이터 수집
"""

import warnings
from datetime import datetime, timedelta

import pandas as pd
import FinanceDataReader as fdr
from pykrx import stock

warnings.filterwarnings("ignore")


def get_latest_trading_date() -> str:
    """가장 최근 거래일 반환 (최대 7일 탐색)"""
    for days_ago in range(1, 8):
        candidate = (datetime.today() - timedelta(days=days_ago)).strftime("%Y%m%d")
        try:
            df = fdr.StockListing("KRX")
            if df is not None and not df.empty:
                print(f"  → 기준일: {candidate}")
                return candidate
        except Exception:
            continue
    return (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")


TODAY = (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")
TODAY_DISPLAY = datetime.today().strftime("%Y년 %m월 %d일")
START_DATE = (datetime.today() - timedelta(days=365)).strftime("%Y%m%d")


# ──────────────────────────────────────────────
# 1. 데이터 수집
# ──────────────────────────────────────────────

def build_master() -> pd.DataFrame:
    """KRX 전종목 리스팅 + 재무지표 수집"""
    print("  KRX 종목 리스트 수집 중...")
    krx = fdr.StockListing("KRX")

    # 컬럼명 정규화
    krx.columns = [c.strip() for c in krx.columns]
    print(f"  → 컬럼: {list(krx.columns)}")

    # Symbol/Code 컬럼 통일
    if "Symbol" in krx.columns:
        krx = krx.rename(columns={"Symbol": "ticker"})
    elif "Code" in krx.columns:
        krx = krx.rename(columns={"Code": "ticker"})

    # 종목명 컬럼 통일
    if "Name" in krx.columns:
        krx = krx.rename(columns={"Name": "name"})
    elif "종목명" in krx.columns:
        krx = krx.rename(columns={"종목명": "name"})

    # 시장 컬럼
    if "Market" in krx.columns:
        krx = krx.rename(columns={"Market": "market"})
    elif "마켓" in krx.columns:
        krx = krx.rename(columns={"마켓": "market"})

    krx = krx[["ticker", "name", "market"]].copy()
    krx["ticker"] = krx["ticker"].astype(str).str.zfill(6)

    print("  pykrx 재무지표 수집 중 (KOSPI)...")
    fund_kospi = stock.get_market_fundamental(TODAY, market="KOSPI")
    print("  pykrx 재무지표 수집 중 (KOSDAQ)...")
    fund_kosdaq = stock.get_market_fundamental(TODAY, market="KOSDAQ")

    fund = pd.concat([fund_kospi, fund_kosdaq])
    fund.index.name = "ticker"
    fund = fund.reset_index()
    fund["ticker"] = fund["ticker"].astype(str).str.zfill(6)

    print("  pykrx 시가총액 수집 중 (KOSPI)...")
    cap_kospi = stock.get_market_cap(TODAY, market="KOSPI")
    print("  pykrx 시가총액 수집 중 (KOSDAQ)...")
    cap_kosdaq = stock.get_market_cap(TODAY, market="KOSDAQ")

    cap = pd.concat([cap_kospi, cap_kosdaq])
    cap.index.name = "ticker"
    cap = cap.reset_index()
    cap["ticker"] = cap["ticker"].astype(str).str.zfill(6)
    cap = cap[["ticker", "시가총액"]]

    df = fund.merge(cap, on="ticker", how="inner")
    df = df.merge(krx, on="ticker", how="left")

    df["cap_억"] = (df["시가총액"] / 1e8).round(0).astype(int)

    # 컬럼명 소문자 정규화
    df = df.rename(columns={"BPS": "bps", "PER": "per", "PBR": "pbr",
                             "EPS": "eps", "DIV": "div", "DPS": "dps"})

    print(f"  → 전체 종목 수: {len(df)}")
    return df


# ──────────────────────────────────────────────
# 2. 스크리닝
# ──────────────────────────────────────────────

def screen_value(df):
    mask = (
        (df["pbr"] > 0) & (df["pbr"] < 0.7) &
        (df["per"] > 0) & (df["per"] < 15) &
        (df["ROE"] > 5) &
        (df["cap_억"] > 300)
    )
    return df[mask].sort_values("pbr").head(30)


def screen_growth(df):
    mask = (
        (df["ROE"] > 15) &
        (df["per"] > 0) & (df["per"] < 25) &
        (df["pbr"] > 0) &
        (df["cap_억"] > 500)
    )
    return df[mask].sort_values("ROE", ascending=False).head(30)


def screen_dividend(df):
    mask = (
        (df["div"] > 3.0) &
        (df["pbr"] > 0) & (df["pbr"] < 2) &
        (df["cap_억"] > 500)
    )
    return df[mask].sort_values("div", ascending=False).head(30)


# ──────────────────────────────────────────────
# 3. HTML 생성
# ──────────────────────────────────────────────

TABLE_CSS = """
<style>
  :root { --bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;--text:#e2e8f0;
          --muted:#8892a4;--accent:#6c8ebf;--green:#4ade80;--red:#f87171;
          --amber:#fbbf24;--purple:#a78bfa; }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,sans-serif;
       font-size:14px;padding:0 0 60px}
  header{background:var(--card);border-bottom:1px solid var(--border);
         padding:18px 24px;display:flex;align-items:center;gap:12px}
  header h1{font-size:18px;font-weight:600}
  header .updated{font-size:12px;color:var(--muted);margin-left:auto}
  .tabs{display:flex;gap:4px;padding:16px 24px 0;border-bottom:1px solid var(--border)}
  .tab{padding:8px 18px;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px;
       color:var(--muted);background:transparent;border:none;transition:all .15s}
  .tab.active{background:var(--card);color:var(--text);font-weight:500}
  .tab:hover{color:var(--text)}
  .panel{display:none;padding:20px 24px}
  .panel.active{display:block}
  .section-title{font-size:12px;color:var(--muted);margin-bottom:10px;letter-spacing:.04em}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;font-size:11px;color:var(--muted);font-weight:500;
     padding:7px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,.03)}
  .ticker{font-family:monospace;font-size:12px;color:var(--muted)}
  .name{font-weight:500}
  .num{text-align:right}
  .badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:500}
  .badge.kospi{background:rgba(108,142,191,.15);color:var(--accent)}
  .badge.kosdaq{background:rgba(167,139,250,.15);color:var(--purple)}
  .pos{color:var(--green)} .neg{color:var(--red)} .neutral{color:var(--muted)}
  .summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
                gap:12px;margin-bottom:20px}
  .summary-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
  .summary-card .label{font-size:11px;color:var(--muted);margin-bottom:6px}
  .summary-card .value{font-size:22px;font-weight:600}
</style>
"""

def _badge(market):
    if not isinstance(market, str): return ""
    cls = "kospi" if "KOSPI" in market.upper() else "kosdaq"
    label = "KOSPI" if "KOSPI" in market.upper() else "KOSDAQ"
    return f'<span class="badge {cls}">{label}</span>'

def _fmt_per(v):
    try:
        v = float(v)
        return f"{v:.1f}" if v > 0 else '<span class="neutral">-</span>'
    except: return '<span class="neutral">-</span>'

def _fmt_pbr(v):
    try:
        v = float(v)
        if v <= 0: return '<span class="neutral">-</span>'
        cls = "pos" if v < 0.5 else "neutral"
        return f'<span class="{cls}">{v:.2f}</span>'
    except: return '<span class="neutral">-</span>'

def _fmt_roe(v):
    try:
        v = float(v)
        cls = "pos" if v > 10 else "neutral" if v > 0 else "neg"
        return f'<span class="{cls}">{v:.1f}%</span>'
    except: return '<span class="neutral">-</span>'

def _fmt_div(v):
    try:
        v = float(v)
        if v <= 0: return '<span class="neutral">-</span>'
        cls = "pos" if v >= 3 else "neutral"
        return f'<span class="{cls}">{v:.1f}%</span>'
    except: return '<span class="neutral">-</span>'

def _fmt_cap(v):
    try:
        v = int(v)
        return f"{v/10000:.1f}조" if v >= 10000 else f"{v:,}억"
    except: return "-"

def make_table(df, cols):
    """cols: list of (헤더명, 컬럼키, 포맷함수, 클래스)"""
    thead = "".join(f'<th class="{c[3]}">{c[0]}</th>' for c in cols)
    rows = ""
    for _, r in df.iterrows():
        cells = ""
        for col in cols:
            val = r.get(col[1], "")
            formatted = col[2](val) if col[2] else str(val)
            cells += f'<td class="{col[3]}">{formatted}</td>'
        rows += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{thead}</tr></thead><tbody>{rows}</tbody></table>"

def build_html(val, gro, div_df, run_date):
    def ticker_fmt(v): return f'<span class="ticker">{v}</span>'
    def name_fmt(v): return f'<span class="name">{v}</span>'

    val_tbl = make_table(val, [
        ("코드","ticker",ticker_fmt,""),("종목명","name",name_fmt,"name"),("시장","market",_badge,""),
        ("PBR","pbr",_fmt_pbr,"num"),("PER","per",_fmt_per,"num"),
        ("ROE","ROE",_fmt_roe,"num"),("배당","div",_fmt_div,"num"),("시총","cap_억",_fmt_cap,"num"),
    ])
    gro_tbl = make_table(gro, [
        ("코드","ticker",ticker_fmt,""),("종목명","name",name_fmt,"name"),("시장","market",_badge,""),
        ("ROE","ROE",_fmt_roe,"num"),("PER","per",_fmt_per,"num"),
        ("PBR","pbr",_fmt_pbr,"num"),("시총","cap_억",_fmt_cap,"num"),
    ])
    div_tbl = make_table(div_df, [
        ("코드","ticker",ticker_fmt,""),("종목명","name",name_fmt,"name"),("시장","market",_badge,""),
        ("배당수익률","div",_fmt_div,"num"),("PBR","pbr",_fmt_pbr,"num"),
        ("ROE","ROE",_fmt_roe,"num"),("시총","cap_억",_fmt_cap,"num"),
    ])

    summary = f"""
    <div class="summary-grid">
      <div class="summary-card"><div class="label">저평가 스크리닝</div>
        <div class="value">{len(val)}<span style="font-size:14px;color:var(--muted)"> 종목</span></div></div>
      <div class="summary-card"><div class="label">고ROE 성장주</div>
        <div class="value">{len(gro)}<span style="font-size:14px;color:var(--muted)"> 종목</span></div></div>
      <div class="summary-card"><div class="label">고배당주</div>
        <div class="value">{len(div_df)}<span style="font-size:14px;color:var(--muted)"> 종목</span></div></div>
      <div class="summary-card"><div class="label">기준일</div>
        <div class="value" style="font-size:15px">{run_date}</div></div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>KYY 주식 스크리너</title>{TABLE_CSS}
</head><body>
  <header><h1>📈 KYY 주식 스크리너</h1>
    <span class="updated">업데이트: {run_date}</span></header>
  <div style="padding:16px 24px 0">{summary}</div>
  <div class="tabs">
    <button class="tab active" onclick="switchTab('value',this)">저평가 스크리닝</button>
    <button class="tab" onclick="switchTab('growth',this)">고ROE 성장주</button>
    <button class="tab" onclick="switchTab('dividend',this)">고배당주</button>
  </div>
  <div id="value" class="panel active">
    <p class="section-title">PBR &lt; 0.7 · PER &lt; 15 · ROE &gt; 5% · 시총 300억↑ (PBR 오름차순)</p>
    {val_tbl}</div>
  <div id="growth" class="panel">
    <p class="section-title">ROE &gt; 15% · PER &lt; 25 · 시총 500억↑ (ROE 내림차순)</p>
    {gro_tbl}</div>
  <div id="dividend" class="panel">
    <p class="section-title">배당수익률 &gt; 3% · PBR &lt; 2 · 시총 500억↑ (배당 내림차순)</p>
    {div_tbl}</div>
  <script>
    function switchTab(id,btn){{
      document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
      document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      btn.classList.add('active');
    }}
  </script>
</body></html>"""


# ──────────────────────────────────────────────
# 4. 실행
# ──────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 스크리너 시작 (기준일: {TODAY})")

    master = build_master()

    val = screen_value(master)
    gro = screen_growth(master)
    div = screen_dividend(master)
    print(f"  → 저평가: {len(val)} / 성장: {len(gro)} / 배당: {len(div)}")

    html = build_html(val, gro, div, TODAY_DISPLAY)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 완료! index.html 생성됨")
