"""
KYY Stock Screener
OpenDartReader + FinanceDataReader (KRX-MARCAP) 조합
- FDR KRX-MARCAP: 전종목 시세/시총 (로그인 불필요)
- FDR KRX: 종목명/시장 리스팅
- DART API: ROE 계산용 재무데이터
"""

import os
import io
import zipfile
import warnings
from datetime import datetime, timedelta

import requests
import pandas as pd

warnings.filterwarnings("ignore")

DART_KEY = os.environ.get("DART_API_KEY", "")
TODAY_DISPLAY = datetime.today().strftime("%Y년 %m월 %d일")


# ──────────────────────────────────────────────
# 1. OpenDartReader로 PER/PBR/배당 수집
# ──────────────────────────────────────────────

def get_krx_fundamental() -> tuple:
    """
    DART API + KRX 상장법인 목록으로 재무지표 수집
    기업개황에서 PER/PBR 대신, marcap 연도 gz 파일로 처리
    """
    # marcap 연도별 gz 파일 직접 다운로드
    year = datetime.today().year
    for y in [year, year - 1]:
        url = f"https://github.com/FinanceData/marcap/raw/master/data/marcap-{y}.csv.gz"
        try:
            print(f"  marcap {y}년 gz 파일 다운로드 중...")
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                df = pd.read_csv(
                    io.BytesIO(resp.content),
                    compression="gzip",
                    dtype={"Code": str}
                )
                df["Code"] = df["Code"].str.zfill(6)
                # 가장 최근 거래일 데이터만 추출
                if "Date" in df.columns:
                    df["Date"] = pd.to_datetime(df["Date"])
                    latest = df["Date"].max()
                    df = df[df["Date"] == latest].copy()
                    date_str = latest.strftime("%Y-%m-%d")
                    print(f"  → 최신 거래일: {date_str}, 종목 수: {len(df)}")
                    return df, date_str
        except Exception as e:
            print(f"  {y}년 시도 실패: {e}")
            continue

    raise RuntimeError("marcap gz 파일을 가져올 수 없습니다.")


# ──────────────────────────────────────────────
# 2. DART로 ROE 계산
# ──────────────────────────────────────────────

def get_dart_roe() -> pd.DataFrame:
    if not DART_KEY:
        print("  DART 키 없음 — ROE 스킵")
        return pd.DataFrame(columns=["Code", "ROE"])

    year = str(datetime.today().year - 1)
    print(f"  DART {year}년 재무데이터 수집 중...")

    try:
        # 기업코드 매핑 먼저
        zip_url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_KEY}"
        zresp = requests.get(zip_url, timeout=30)
        z = zipfile.ZipFile(io.BytesIO(zresp.content))
        xml_data = z.read("CORPCODE.xml").decode("utf-8")
        corp_df = pd.read_xml(io.StringIO(xml_data))
        corp_df = corp_df[corp_df["stock_code"].notna() & (corp_df["stock_code"].astype(str).str.strip() != "")]
        corp_df["stock_code"] = corp_df["stock_code"].astype(str).str.zfill(6)
        code_map = dict(zip(corp_df["corp_code"], corp_df["stock_code"]))

        # 전종목 재무데이터
        url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
        params = {
            "crtfc_key": DART_KEY,
            "year": year,
            "reprt_code": "11011",
            "fs_div": "CFS",
        }
        resp = requests.get(url, params=params, timeout=60)
        data = resp.json()

        if data.get("status") != "000":
            print(f"  DART 오류: {data.get('message')}")
            return pd.DataFrame(columns=["Code", "ROE"])

        df = pd.DataFrame(data["list"])

        def to_num(s):
            try:
                return float(str(s).replace(",", "").replace(" ", ""))
            except:
                return None

        ni = df[df["account_nm"].str.contains("당기순이익", na=False) &
                (df["sj_div"] == "IS")][["corp_code", "thstrm_amount"]].copy()
        ni["net_income"] = ni["thstrm_amount"].apply(to_num)
        ni = ni.dropna(subset=["net_income"])

        eq = df[df["account_nm"].str.contains("자본총계", na=False) &
                (df["sj_div"] == "BS")][["corp_code", "thstrm_amount"]].copy()
        eq["equity"] = eq["thstrm_amount"].apply(to_num)
        eq = eq[eq["equity"] > 0]

        merged = ni[["corp_code","net_income"]].merge(eq[["corp_code","equity"]], on="corp_code", how="inner")
        merged["ROE"] = (merged["net_income"] / merged["equity"] * 100).round(1)
        merged["Code"] = merged["corp_code"].map(code_map)
        merged = merged.dropna(subset=["Code"])
        result = merged.groupby("Code")["ROE"].first().reset_index()

        print(f"  → ROE 계산 완료: {len(result)}종목")
        return result

    except Exception as e:
        print(f"  DART 오류: {e}")
        return pd.DataFrame(columns=["Code", "ROE"])


# ──────────────────────────────────────────────
# 3. 마스터 데이터프레임
# ──────────────────────────────────────────────

def build_master() -> tuple:
    marcap, date_str = get_krx_fundamental()

    # 컬럼명 확인 및 정규화
    print(f"  컬럼: {list(marcap.columns)}")
    rename = {}
    for c in marcap.columns:
        cu = c.strip().upper()
        if cu == "NAME":        rename[c] = "Name"
        elif cu == "MARKET":    rename[c] = "Market"
        elif cu == "PER":       rename[c] = "PER"
        elif cu == "PBR":       rename[c] = "PBR"
        elif cu == "DIV":       rename[c] = "DIV"
        elif cu == "MARCAP":    rename[c] = "Marcap"
        elif cu == "CLOSE":     rename[c] = "Close"
    marcap = marcap.rename(columns=rename)

    for col in ["PER", "PBR", "DIV", "Marcap", "Close"]:
        if col in marcap.columns:
            marcap[col] = pd.to_numeric(marcap[col], errors="coerce")

    marcap["cap_억"] = (marcap.get("Marcap", 0) / 1e8).round(0)

    # DART ROE 병합
    roe_df = get_dart_roe()
    if not roe_df.empty:
        marcap = marcap.merge(roe_df, on="Code", how="left")
    else:
        # 간이 ROE: PBR/PER×100
        marcap["ROE"] = (
            marcap["PBR"] / marcap["PER"].replace(0, float("nan")) * 100
        ).round(1)

    print(f"  → 최종 {len(marcap)}종목")
    return marcap, date_str


# ──────────────────────────────────────────────
# 4. 스크리닝
# ──────────────────────────────────────────────

def screen_value(df):
    mask = (
        (df["PBR"] > 0) & (df["PBR"] < 0.7) &
        (df["PER"] > 0) & (df["PER"] < 15) &
        (df["ROE"] > 5) &
        (df["cap_억"] > 300)
    )
    return df[mask].sort_values("PBR").head(30)

def screen_growth(df):
    mask = (
        (df["ROE"] > 15) &
        (df["PER"] > 0) & (df["PER"] < 25) &
        (df["PBR"] > 0) &
        (df["cap_억"] > 500)
    )
    return df[mask].sort_values("ROE", ascending=False).head(30)

def screen_dividend(df):
    mask = (
        (df["DIV"] > 3.0) &
        (df["PBR"] > 0) & (df["PBR"] < 2) &
        (df["cap_억"] > 500)
    )
    return df[mask].sort_values("DIV", ascending=False).head(30)

def screen_52w_low(df_all):
    """52주 신저가권: 연간 gz에서 52주 고저가 계산"""
    if "Low" not in df_all.columns or "High" not in df_all.columns:
        return pd.DataFrame()

    year = datetime.today().year
    url = f"https://github.com/FinanceData/marcap/raw/master/data/marcap-{year}.csv.gz"
    try:
        print("  52주 데이터 계산 중...")
        resp = requests.get(url, timeout=60)
        hist = pd.read_csv(io.BytesIO(resp.content), compression="gzip", dtype={"Code": str})
        hist["Code"] = hist["Code"].str.zfill(6)
        hist["Date"] = pd.to_datetime(hist["Date"])
        cutoff = datetime.today() - timedelta(days=365)
        hist = hist[hist["Date"] >= cutoff]

        for col in ["Low", "High", "Close"]:
            if col in hist.columns:
                hist[col] = pd.to_numeric(hist[col], errors="coerce")

        agg = hist.groupby("Code").agg(
            low52=("Low", "min"),
            high52=("High", "max")
        ).reset_index()

        latest = df_all[["Code", "Close", "PBR", "PER", "DIV", "cap_억", "Name", "Market"]].copy()
        merged = latest.merge(agg, on="Code", how="inner")
        merged = merged[
            (merged["high52"] > merged["low52"]) &
            (merged["Close"] > 0)
        ]
        merged["52w_위치"] = (
            (merged["Close"] - merged["low52"]) /
            (merged["high52"] - merged["low52"]) * 100
        ).round(1)

        mask = (
            (merged["52w_위치"] < 20) &
            (merged["PBR"] > 0) &
            (merged["cap_억"] > 300)
        )
        return merged[mask].sort_values("52w_위치").head(20)
    except Exception as e:
        print(f"  52주 계산 실패: {e}")
        return pd.DataFrame()


# ──────────────────────────────────────────────
# 5. HTML 생성
# ──────────────────────────────────────────────

CSS = """<style>
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;--text:#e2e8f0;
      --muted:#8892a4;--accent:#6c8ebf;--green:#4ade80;--red:#f87171;--purple:#a78bfa;--amber:#fbbf24}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,sans-serif;font-size:14px;padding-bottom:60px}
header{background:var(--card);border-bottom:1px solid var(--border);padding:18px 24px;display:flex;align-items:center}
header h1{font-size:18px;font-weight:600}
.updated{font-size:12px;color:var(--muted);margin-left:auto}
.tabs{display:flex;gap:4px;padding:16px 24px 0;border-bottom:1px solid var(--border);flex-wrap:wrap}
.tab{padding:8px 16px;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px;color:var(--muted);background:transparent;border:none}
.tab.active{background:var(--card);color:var(--text);font-weight:500}
.tab:hover{color:var(--text)}
.panel{display:none;padding:20px 24px}.panel.active{display:block}
.hint{font-size:12px;color:var(--muted);margin-bottom:10px}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 24px}
.sc{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.sc .lbl{font-size:11px;color:var(--muted);margin-bottom:6px}
.sc .val{font-size:20px;font-weight:600}
table{width:100%;border-collapse:collapse}
th{font-size:11px;color:var(--muted);font-weight:500;padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.03)}
.tk{font-family:monospace;font-size:12px;color:var(--muted)}
.nm{font-weight:500}
.r{text-align:right}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:500}
.kp{background:rgba(108,142,191,.15);color:var(--accent)}
.kd{background:rgba(167,139,250,.15);color:var(--purple)}
.g{color:var(--green)}.rd{color:var(--red)}.mu{color:var(--muted)}.am{color:var(--amber)}
</style>"""

def badge(m):
    if not isinstance(m, str): return ""
    return '<span class="badge kp">KOSPI</span>' if "KOSPI" in m.upper() else '<span class="badge kd">KOSDAQ</span>'

def fp(v):
    try: v=float(v); return f"{v:.1f}" if v>0 else '<span class="mu">-</span>'
    except: return '<span class="mu">-</span>'

def fpbr(v):
    try:
        v=float(v)
        if v<=0: return '<span class="mu">-</span>'
        return f'<span class="{"g" if v<0.5 else "mu"}">{v:.2f}</span>'
    except: return '<span class="mu">-</span>'

def froe(v):
    try:
        v=float(v)
        return f'<span class="{"g" if v>10 else "mu" if v>0 else "rd"}">{v:.1f}%</span>'
    except: return '<span class="mu">-</span>'

def fdiv(v):
    try:
        v=float(v)
        if v<=0: return '<span class="mu">-</span>'
        return f'<span class="{"g" if v>=3 else "mu"}">{v:.1f}%</span>'
    except: return '<span class="mu">-</span>'

def fcap(v):
    try:
        v=int(float(v))
        return f"{v/10000:.1f}조" if v>=10000 else f"{v:,}억"
    except: return "-"

def f52w(v):
    try:
        v=float(v)
        return f'<span class="{"g" if v<10 else "am"}">{v:.1f}%</span>'
    except: return '<span class="mu">-</span>'

def tbl(df, specs):
    if df.empty: return "<p style='color:var(--muted);padding:20px'>데이터 없음</p>"
    hdr="".join(f'<th class="{s[3]}">{s[0]}</th>' for s in specs)
    rows="".join(
        f"<tr>{''.join(f'<td class=\"{s[3]}\">{s[2](r.get(s[1], \"\"))}</td>' for s in specs)}</tr>"
        for _,r in df.iterrows()
    )
    return f"<table><thead><tr>{hdr}</tr></thead><tbody>{rows}</tbody></table>"

def build_html(val, gro, div, low52, run_date, data_date):
    tk=lambda v: f'<span class="tk">{v}</span>'
    nm=lambda v: f'<span class="nm">{v if isinstance(v,str) else ""}</span>'
    base=[("코드","Code",tk,""),("종목명","Name",nm,""),("시장","Market",badge,"")]

    sm=f"""<div class="summary-grid">
      <div class="sc"><div class="lbl">저평가 스크리닝</div><div class="val">{len(val)}<span style="font-size:13px;color:var(--muted)"> 종목</span></div></div>
      <div class="sc"><div class="lbl">고ROE 성장주</div><div class="val">{len(gro)}<span style="font-size:13px;color:var(--muted)"> 종목</span></div></div>
      <div class="sc"><div class="lbl">고배당주</div><div class="val">{len(div)}<span style="font-size:13px;color:var(--muted)"> 종목</span></div></div>
      <div class="sc"><div class="lbl">52주 신저가권</div><div class="val">{len(low52)}<span style="font-size:13px;color:var(--muted)"> 종목</span></div></div>
      <div class="sc"><div class="lbl">데이터 기준일</div><div class="val" style="font-size:14px">{data_date}</div></div>
    </div>"""

    return f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KYY 주식 스크리너</title>{CSS}</head><body>
<header><h1>📈 KYY 주식 스크리너</h1><span class="updated">업데이트: {run_date}</span></header>
{sm}
<div class="tabs">
  <button class="tab active" onclick="sw('value',this)">저평가 스크리닝</button>
  <button class="tab" onclick="sw('growth',this)">고ROE 성장주</button>
  <button class="tab" onclick="sw('dividend',this)">고배당주</button>
  <button class="tab" onclick="sw('low52',this)">52주 신저가권</button>
</div>
<div id="value" class="panel active">
  <p class="hint">PBR &lt; 0.7 · PER &lt; 15 · ROE &gt; 5% · 시총 300억↑ (PBR 오름차순)</p>
  {tbl(val, base+[("PBR","PBR",fpbr,"r"),("PER","PER",fp,"r"),("ROE","ROE",froe,"r"),("배당","DIV",fdiv,"r"),("시총","cap_억",fcap,"r")])}</div>
<div id="growth" class="panel">
  <p class="hint">ROE &gt; 15% · PER &lt; 25 · 시총 500억↑ (ROE 내림차순)</p>
  {tbl(gro, base+[("ROE","ROE",froe,"r"),("PER","PER",fp,"r"),("PBR","PBR",fpbr,"r"),("시총","cap_억",fcap,"r")])}</div>
<div id="dividend" class="panel">
  <p class="hint">배당수익률 &gt; 3% · PBR &lt; 2 · 시총 500억↑ (배당 내림차순)</p>
  {tbl(div, base+[("배당수익률","DIV",fdiv,"r"),("PBR","PBR",fpbr,"r"),("ROE","ROE",froe,"r"),("시총","cap_억",fcap,"r")])}</div>
<div id="low52" class="panel">
  <p class="hint">52주 고저가 범위 내 하위 20% 위치 · 시총 300억↑ (위치 오름차순)</p>
  {tbl(low52, base+[("52주위치","52w_위치",f52w,"r"),("PBR","PBR",fpbr,"r"),("PER","PER",fp,"r"),("시총","cap_억",fcap,"r")])}</div>
<script>
function sw(id,btn){{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');btn.classList.add('active');
}}
</script></body></html>"""


# ──────────────────────────────────────────────
# 6. 실행
# ──────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 스크리너 시작")
    master, data_date = build_master()
    val   = screen_value(master)
    gro   = screen_growth(master)
    div   = screen_dividend(master)
    low52 = screen_52w_low(master)
    print(f"  결과 → 저평가:{len(val)} / 성장:{len(gro)} / 배당:{len(div)} / 52주저가:{len(low52)}")
    html = build_html(val, gro, div, low52, TODAY_DISPLAY, data_date)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 완료!")
