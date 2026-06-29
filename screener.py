"""
KYY Stock Screener
DART Open API + marcap 조합 (로그인 불필요, 안정적)
- marcap: 주가/시총/PER/PBR/배당수익률
- DART: ROE 계산용 재무데이터 (자본/당기순이익)
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
# 1. marcap으로 전종목 시세 + PER/PBR/배당 수집
# ──────────────────────────────────────────────

def get_marcap() -> pd.DataFrame:
    """
    marcap-data GitHub에서 직접 CSV 다운로드
    컬럼: Code, Name, Market, Close, Marcap, Shares, PER, PBR, EPS, BPS, DPS, DIV
    """
    # 최근 거래일 탐색 (최대 7일)
    for days_ago in range(1, 8):
        d = datetime.today() - timedelta(days=days_ago)
        if d.weekday() >= 5:   # 토/일 건너뜀
            continue
        date_str = d.strftime("%Y-%m-%d")
        year = d.strftime("%Y")
        url = f"https://raw.githubusercontent.com/FinanceData/marcap/master/data/{year}/{date_str}.csv"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                df = pd.read_csv(io.StringIO(resp.text))
                if not df.empty:
                    print(f"  → marcap 기준일: {date_str} ({len(df)}종목)")
                    return df, date_str
        except Exception as e:
            print(f"  {date_str} 시도 실패: {e}")
            continue
    raise RuntimeError("marcap 데이터를 가져올 수 없습니다.")


# ──────────────────────────────────────────────
# 2. DART로 ROE 계산 (자기자본 + 당기순이익)
# ──────────────────────────────────────────────

def get_dart_roe() -> pd.DataFrame:
    """DART 전종목 재무데이터로 ROE 계산"""
    if not DART_KEY:
        print("  DART 키 없음 — ROE 스킵")
        return pd.DataFrame(columns=["Code", "ROE"])

    # 최근 사업연도 (전년도)
    year = str(datetime.today().year - 1)

    print(f"  DART {year}년 재무데이터 수집 중...")
    url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
    params = {
        "crtfc_key": DART_KEY,
        "year": year,
        "reprt_code": "11011",   # 사업보고서
        "fs_div": "CFS",         # 연결재무제표
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        if data.get("status") != "000":
            print(f"  DART 오류: {data.get('message')} — ROE 스킵")
            return pd.DataFrame(columns=["Code", "ROE"])

        df = pd.DataFrame(data["list"])

        # 당기순이익 (지배주주)
        ni = df[df["account_nm"].str.contains("당기순이익", na=False) &
                (df["sj_div"] == "IS")][["corp_code", "thstrm_amount"]].copy()
        ni = ni.rename(columns={"thstrm_amount": "net_income"})
        ni["net_income"] = pd.to_numeric(ni["net_income"].str.replace(",", ""), errors="coerce")

        # 자기자본 (자본총계)
        eq = df[df["account_nm"].str.contains("자본총계", na=False) &
                (df["sj_div"] == "BS")][["corp_code", "thstrm_amount"]].copy()
        eq = eq.rename(columns={"thstrm_amount": "equity"})
        eq["equity"] = pd.to_numeric(eq["equity"].str.replace(",", ""), errors="coerce")

        merged = ni.merge(eq, on="corp_code", how="inner")
        merged = merged[merged["equity"] > 0]
        merged["ROE_dart"] = (merged["net_income"] / merged["equity"] * 100).round(1)

        # corp_code → 종목코드 매핑
        corp_url = "https://opendart.fss.or.kr/api/company.json"
        # 공시기업 목록으로 corp_code ↔ stock_code 매핑
        zip_url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_KEY}"
        zresp = requests.get(zip_url, timeout=30)
        z = zipfile.ZipFile(io.BytesIO(zresp.content))
        xml_data = z.read("CORPCODE.xml").decode("utf-8")
        corp_df = pd.read_xml(io.StringIO(xml_data))
        corp_df = corp_df[corp_df["stock_code"].notna() & (corp_df["stock_code"] != " ")]
        corp_df["stock_code"] = corp_df["stock_code"].astype(str).str.zfill(6)

        result = merged.merge(corp_df[["corp_code", "stock_code"]], on="corp_code", how="inner")
        result = result[["stock_code", "ROE_dart"]].rename(columns={"stock_code": "Code", "ROE_dart": "ROE"})
        result = result.groupby("Code")["ROE"].first().reset_index()

        print(f"  → DART ROE 계산 완료: {len(result)}종목")
        return result

    except Exception as e:
        print(f"  DART 오류: {e} — ROE 스킵")
        return pd.DataFrame(columns=["Code", "ROE"])


# ──────────────────────────────────────────────
# 3. 마스터 데이터프레임 조합
# ──────────────────────────────────────────────

def build_master() -> tuple:
    print("  marcap 데이터 수집 중...")
    marcap, date_str = get_marcap()

    # 컬럼 정규화
    marcap.columns = [c.strip() for c in marcap.columns]
    print(f"  → 컬럼: {list(marcap.columns)}")

    # 필수 컬럼 확인 및 이름 맞추기
    rename = {}
    for c in marcap.columns:
        cu = c.upper()
        if cu in ("CODE", "종목코드"):       rename[c] = "Code"
        elif cu in ("NAME", "종목명"):        rename[c] = "Name"
        elif cu in ("MARKET", "시장"):        rename[c] = "Market"
        elif cu in ("PER",):                  rename[c] = "PER"
        elif cu in ("PBR",):                  rename[c] = "PBR"
        elif cu in ("DIV", "배당수익률"):     rename[c] = "DIV"
        elif cu in ("MARCAP", "시가총액"):    rename[c] = "Marcap"
    marcap = marcap.rename(columns=rename)

    for col in ["PER", "PBR", "DIV", "Marcap"]:
        if col in marcap.columns:
            marcap[col] = pd.to_numeric(marcap[col], errors="coerce")

    marcap["Code"] = marcap["Code"].astype(str).str.zfill(6)
    marcap["cap_억"] = (marcap["Marcap"] / 1e8).round(0)

    # DART ROE 병합
    roe_df = get_dart_roe()
    if not roe_df.empty:
        marcap = marcap.merge(roe_df, on="Code", how="left")
    else:
        # ROE 없으면 PBR/PER로 간이 추정 (ROE ≈ PBR/PER × 100)
        marcap["ROE"] = (marcap["PBR"] / marcap["PER"].replace(0, float("nan")) * 100).round(1)

    print(f"  → 최종 종목 수: {len(marcap)}")
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


def screen_52w_low(df, date_str):
    """52주 신저가 근처 종목 — marcap 1년치 데이터로 계산"""
    print("  52주 신저가 계산 중...")
    year_ago = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    rows = []
    base_url = "https://raw.githubusercontent.com/FinanceData/marcap/master/data"

    # 연/월별 파일 목록 생성 (지난 12개월)
    dates = pd.date_range(start=year_ago, end=date_str, freq="B")  # 영업일
    monthly = sorted(set(d.strftime("%Y-%m") for d in dates))

    price_dict = {}  # code → [low prices]
    for ym in monthly:
        year, month = ym.split("-")
        # 해당 월의 마지막 거래일 파일 1개만 샘플로 사용 (속도 최적화)
        for day in range(28, 0, -1):
            try:
                d_str = f"{year}-{month}-{day:02d}"
                url = f"{base_url}/{year}/{d_str}.csv"
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    tmp = pd.read_csv(io.StringIO(r.text), usecols=lambda c: c in ["Code","Low","Close"])
                    if not tmp.empty:
                        for _, row in tmp.iterrows():
                            code = str(row.get("Code","")).zfill(6)
                            low = row.get("Low", row.get("Close", None))
                            if code not in price_dict:
                                price_dict[code] = {"low": [], "high": []}
                            if pd.notna(low):
                                price_dict[code]["low"].append(float(low))
                                high = row.get("High", row.get("Close", None))
                                if pd.notna(high):
                                    price_dict[code]["high"].append(float(high))
                    break
            except Exception:
                continue

    result = []
    for _, row in df.iterrows():
        code = row["Code"]
        cur = row.get("Close", None)
        if code in price_dict and cur and price_dict[code]["low"]:
            lo = min(price_dict[code]["low"])
            hi = max(price_dict[code]["high"]) if price_dict[code]["high"] else None
            if hi and hi > lo:
                pct = round((float(cur) - lo) / (hi - lo) * 100, 1)
                result.append({**row.to_dict(), "52w_위치": pct})
        else:
            result.append({**row.to_dict(), "52w_위치": None})

    out = pd.DataFrame(result)
    mask = (
        (out["52w_위치"].notna()) &
        (out["52w_위치"] < 20) &   # 52주 저가 대비 20% 이내
        (out["PBR"] > 0) &
        (out["cap_억"] > 300)
    )
    return out[mask].sort_values("52w_위치").head(20)


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
        c="g" if v<0.5 else "mu"
        return f'<span class="{c}">{v:.2f}</span>'
    except: return '<span class="mu">-</span>'

def froe(v):
    try:
        v=float(v)
        c="g" if v>10 else "mu" if v>0 else "rd"
        return f'<span class="{c}">{v:.1f}%</span>'
    except: return '<span class="mu">-</span>'

def fdiv(v):
    try:
        v=float(v)
        if v<=0: return '<span class="mu">-</span>'
        c="g" if v>=3 else "mu"
        return f'<span class="{c}">{v:.1f}%</span>'
    except: return '<span class="mu">-</span>'

def fcap(v):
    try:
        v=int(float(v))
        return f"{v/10000:.1f}조" if v>=10000 else f"{v:,}억"
    except: return "-"

def f52w(v):
    try:
        v=float(v)
        c="g" if v<10 else "am" if v<20 else "mu"
        return f'<span class="{c}">{v:.1f}%</span>'
    except: return '<span class="mu">-</span>'

def tbl(df, specs):
    hdr="".join(f'<th class="{s[3]}">{s[0]}</th>' for s in specs)
    rows=""
    for _,r in df.iterrows():
        cells="".join(f'<td class="{s[3]}">{s[2](r.get(s[1],""))}</td>' for s in specs)
        rows+=f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{hdr}</tr></thead><tbody>{rows}</tbody></table>"

def build_html(val, gro, div, low52, run_date, data_date):
    tk=lambda v:f'<span class="tk">{v}</span>'
    nm=lambda v:f'<span class="nm">{v if isinstance(v,str) else ""}</span>'

    common_cols = [("코드","Code",tk,""),("종목명","Name",nm,""),("시장","Market",badge,"")]

    vt=tbl(val, common_cols+[("PBR","PBR",fpbr,"r"),("PER","PER",fp,"r"),
                              ("ROE","ROE",froe,"r"),("배당","DIV",fdiv,"r"),("시총","cap_억",fcap,"r")])
    gt=tbl(gro, common_cols+[("ROE","ROE",froe,"r"),("PER","PER",fp,"r"),
                              ("PBR","PBR",fpbr,"r"),("시총","cap_억",fcap,"r")])
    dt=tbl(div, common_cols+[("배당수익률","DIV",fdiv,"r"),("PBR","PBR",fpbr,"r"),
                              ("ROE","ROE",froe,"r"),("시총","cap_억",fcap,"r")])
    lt=tbl(low52, common_cols+[("52주위치","52w_위치",f52w,"r"),("PBR","PBR",fpbr,"r"),
                                ("PER","PER",fp,"r"),("시총","cap_억",fcap,"r")])

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
  <p class="hint">PBR &lt; 0.7 · PER &lt; 15 · ROE &gt; 5% · 시총 300억↑ (PBR 오름차순)</p>{vt}</div>
<div id="growth" class="panel">
  <p class="hint">ROE &gt; 15% · PER &lt; 25 · 시총 500억↑ (ROE 내림차순)</p>{gt}</div>
<div id="dividend" class="panel">
  <p class="hint">배당수익률 &gt; 3% · PBR &lt; 2 · 시총 500억↑ (배당 내림차순)</p>{dt}</div>
<div id="low52" class="panel">
  <p class="hint">52주 고저가 범위 내 하위 20% 위치 · 시총 300억↑ (위치 오름차순)</p>{lt}</div>
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
    low52 = screen_52w_low(master, data_date)

    print(f"  → 저평가:{len(val)} / 성장:{len(gro)} / 배당:{len(div)} / 52주저가:{len(low52)}")

    html = build_html(val, gro, div, low52, TODAY_DISPLAY, data_date)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 완료! index.html 생성됨")
