"""실거래가 시세 조회 (간단 버전)

국토교통부 아파트 실거래가 공개 API로 부산 아파트의 매매·전세·월세 시세를 조회한다.
필요한 Secrets: SERVICE_KEY (공공데이터포털 인증키) 하나뿐.
"""
import datetime as dt
import re
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="실거래가 시세 조회", page_icon="🏠", layout="centered")

# ── 설정 ─────────────────────────────────────────────
SERVICE_KEY = st.secrets["SERVICE_KEY"]
DEFAULT_GU = "부산진구"
TREND_MONTHS = 12  # 월별 흐름에 쓰는 기간(개월)

URLS = {
    "매매": "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
    "전월세": "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
}

REGIONS = {
    "중구": "26110", "서구": "26140", "동구": "26170", "영도구": "26200",
    "부산진구": "26230", "동래구": "26260", "남구": "26290", "북구": "26320",
    "해운대구": "26350", "사하구": "26380", "금정구": "26410", "강서구": "26440",
    "연제구": "26470", "수영구": "26500", "사상구": "26530", "기장군": "26710",
}

# API 응답의 태그 이름 (새 영문 이름, 예전 한글 이름 둘 다 시도)
FIELDS = {
    "apt": ["aptNm", "아파트"],
    "dong": ["umdNm", "법정동"],
    "area": ["excluUseAr", "전용면적"],
    "year": ["dealYear", "년"],
    "month": ["dealMonth", "월"],
    "day": ["dealDay", "일"],
    "floor": ["floor", "층"],
    "price": ["dealAmount", "거래금액"],
    "deposit": ["deposit", "보증금액"],
    "rent": ["monthlyRent", "월세금액"],
    "cancel": ["cdealType", "해제여부"],
}


# ── 도우미 함수 ──────────────────────────────────────
def get(item, key):
    for tag in FIELDS[key]:
        el = item.find(tag)
        if el is not None and el.text is not None:
            return el.text.strip()
    return ""


def to_num(s):
    s = re.sub(r"[^0-9.]", "", s or "")
    return float(s) if s else 0.0


def won(v):
    """만원 단위 숫자를 '4억 5,000' 처럼 표시."""
    if v is None or pd.isna(v):
        return "-"
    e, m = divmod(int(round(v)), 10000)
    if e and m:
        return f"{e}억 {m:,}"
    if e:
        return f"{e}억"
    return f"{m:,}만"


def recent_months(n):
    today = dt.date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)  # 같은 조회는 6시간 동안 다시 부르지 않음
def fetch_month(api_kind, lawd_cd, ymd):
    rows, page = [], 1
    while True:
        r = requests.get(
            URLS[api_kind],
            params={"serviceKey": SERVICE_KEY, "LAWD_CD": lawd_cd, "DEAL_YMD": ymd,
                    "numOfRows": 1000, "pageNo": page},
            timeout=20,
        )
        r.raise_for_status()
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise RuntimeError(r.text[:200])
        auth = root.findtext(".//returnAuthMsg")
        if auth:
            raise RuntimeError(auth)
        code = (root.findtext(".//resultCode") or "").strip()
        if code not in ("", "00", "000"):
            raise RuntimeError(f"{code} {root.findtext('.//resultMsg')}")
        items = root.findall(".//item")
        rows += [{k: get(it, k) for k in FIELDS} for it in items]
        total = int(root.findtext(".//totalCount") or 0)
        if not items or page * 1000 >= total:
            return rows
        page += 1


# ── 입력 화면 ────────────────────────────────────────
st.title("실거래가 시세 조회")
st.caption("국토교통부 실거래가 공개 자료 기준, 참고용 시세입니다.")

gu = st.selectbox("구", list(REGIONS), index=list(REGIONS).index(DEFAULT_GU))
dong_text = st.text_input("동 (선택, 여러 개는 띄어서)", placeholder="예: 양정동 부전동")
kind = st.radio("거래 종류", ["매매", "전세", "월세"], horizontal=True)
pyeong = st.slider("전용면적 (평)", 0, 60, (0, 60))
months = st.slider("기간 (최근 몇 개월)", 1, 12, 6)
apt_kw = st.text_input("단지명 검색 (선택)", placeholder="예: 스위첸")

if st.button("조회하기", type="primary"):
    st.session_state.searched = True
if not st.session_state.get("searched"):
    st.info("조건을 고르고 조회하기를 누르세요.")
    st.stop()


# ── 데이터 불러오기와 정리 ─────────────────────────────
api_kind = "매매" if kind == "매매" else "전월세"
with st.spinner("실거래가를 불러오는 중..."):
    try:
        rows = []
        for ymd in recent_months(max(months, TREND_MONTHS)):
            rows += fetch_month(api_kind, REGIONS[gu], ymd)
    except Exception as e:
        st.error(f"데이터를 불러오지 못했어요: {e}")
        st.stop()

df = pd.DataFrame(rows)
if df.empty:
    st.warning("이 지역의 거래 자료가 없어요.")
    st.stop()

df["평"] = (df["area"].map(to_num) / 3.3058).round(1)
df["계약일"] = pd.to_datetime(
    df["year"] + "-" + df["month"].str.zfill(2) + "-" + df["day"].str.zfill(2), errors="coerce"
)
df = df[df["cancel"].str.upper() != "O"].copy()  # 취소된 거래는 뺀다
if kind == "매매":
    df["가격"] = df["price"].map(to_num)
    df["월세"] = 0.0
else:
    df["가격"] = df["deposit"].map(to_num)
    df["월세"] = df["rent"].map(to_num)
    df = df[df["월세"] == 0] if kind == "전세" else df[df["월세"] > 0]

keys = dong_text.split()
if keys:
    df = df[df["dong"].apply(lambda d: any(k in d for k in keys))]
if apt_kw.strip():
    df = df[df["apt"].str.contains(apt_kw.strip(), regex=False)]
lo, hi = pyeong
df = df[(df["평"] >= lo) & (df["평"] <= (hi if hi < 60 else 1e9))].copy()
df["평형"] = df["평"].round().astype(int).astype(str) + "평"

base = df  # 월별 흐름용 (최근 12개월)
df = df[df["계약일"] >= pd.Timestamp(dt.date.today()) - pd.DateOffset(months=months)]


# ── 결과 화면 ────────────────────────────────────────
label = "매매가" if kind == "매매" else "보증금"
st.subheader(f"{kind}: 조건에 맞는 거래 {len(df)}건")
if df.empty:
    st.warning("조건에 맞는 거래가 없어요. 평수나 기간을 넓혀 보세요.")
    st.stop()

c1, c2 = st.columns(2)
c1.metric(f"{label} 중간값", won(df["가격"].median()))
c2.metric("최저 ~ 최고", f"{won(df['가격'].min())} ~ {won(df['가격'].max())}")

summary = (
    df.sort_values("계약일")
    .groupby(["apt", "평형"])
    .agg(동=("dong", "last"), 거래수=("가격", "size"), 최근가=("가격", "last"),
         최저=("가격", "min"), 최고=("가격", "max"), 최근계약=("계약일", "last"))
    .reset_index()
    .sort_values("최근계약", ascending=False)
)
for col in ["최근가", "최저", "최고"]:
    summary[col] = summary[col].map(won)
summary["최근계약"] = summary["최근계약"].dt.strftime("%y.%m.%d")
summary = summary.rename(columns={"apt": "단지"})

st.markdown("**단지·평형별 요약** (줄 왼쪽을 누르면 상세 정보)")
event = st.dataframe(summary, hide_index=True, on_select="rerun", selection_mode="single-row")

if event.selection.rows:
    sel = summary.iloc[event.selection.rows[0]]
    one = base[(base["apt"] == sel["단지"]) & (base["평형"] == sel["평형"])].sort_values("계약일", ascending=False)
    st.markdown(f"### {sel['단지']} {sel['평형']} ({kind})")
    val_col = "월세" if kind == "월세" else "가격"
    what = "월세" if kind == "월세" else label

    monthly = one.assign(월=one["계약일"].dt.to_period("M").dt.to_timestamp()).groupby("월")[val_col].median()
    if val_col == "가격":
        monthly = (monthly / 10000).round(3)
        unit = "억"
    else:
        unit = "만원"
    st.markdown(f"**월별 {what} 흐름** (최근 {TREND_MONTHS}개월 중간값, {unit})")
    st.line_chart(monthly.rename(f"{what} ({unit})"))

    t = pd.DataFrame({
        "계약일": one["계약일"].dt.strftime("%y.%m.%d"),
        "층": one["floor"],
        label: one["가격"].map(won),
    })
    if kind == "월세":
        t["월세"] = one["월세"].map(lambda v: f"{int(v)}만")
    st.markdown("**최근 거래**")
    st.dataframe(t, hide_index=True)
else:
    st.info("요약표에서 단지 줄 왼쪽 칸을 누르면 월별 흐름과 최근 거래가 나와요.")

st.caption("실거래가는 신고까지 시차가 있어 최근 한두 달 거래가 빠질 수 있고, 현재 호가와 다를 수 있습니다. "
           "전월세는 확정일자 등으로 신고된 거래만 포함됩니다.")
