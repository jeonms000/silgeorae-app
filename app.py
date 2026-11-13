import datetime as dt
import re
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="실거래가 시세 조회", page_icon="🏠", layout="centered")

# ── 설정 ─────────────────────────────────────────────
SERVICE_KEY = st.secrets["SERVICE_KEY"]  # .streamlit/secrets.toml 에서 읽음
DEFAULT_GU = "연제구"  # 사무소가 있는 구로 바꾸세요
TREND_MONTHS = 12      # 가격 변동 계산에 쓰는 기간(개월)

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


def parse_money(text):
    """'4억5천', '4억 5000', '45000' 을 만원 단위 숫자로 바꿉니다."""
    s = re.sub(r"[,\s원]", "", text or "")
    if not s:
        return None
    total = 0.0
    if "억" in s:
        a, s = s.split("억", 1)
        total += float(a or 0) * 10000
    s = s.replace("만", "")
    if "천" in s:
        total += float(s.replace("천", "") or 0) * 1000
    elif s:
        total += float(s)
    return total or None


def money(v):
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


def month_start(ymd):
    return pd.Timestamp(f"{ymd[:4]}-{ymd[4:]}-01")


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)  # 같은 조회는 6시간 동안 다시 부르지 않음
def fetch_month(kind, lawd_cd, ymd):
    rows, page = [], 1
    while True:
        r = requests.get(
            URLS[kind],
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

gu_names = list(REGIONS)
gu = st.selectbox("구", gu_names, index=gu_names.index(DEFAULT_GU))
with st.expander("서울 외 지역"):
    other_code = st.text_input("지역코드 5자리", placeholder="예: 41135 (성남시 분당구)")
lawd_cd = other_code.strip() or REGIONS[gu]

dong_text = st.text_input("동 (선택, 여러 개는 띄어서)", placeholder="예: 행당동 금호동4가")
kind = st.radio("거래 종류", ["매매", "전세", "월세"], horizontal=True)
budget_text = st.text_input("예산 최대 (선택)", placeholder="매매가 또는 보증금, 예: 4억5천")
rent_max = 0
if kind == "월세":
    rent_max = st.number_input("월세 최대 (만원, 0이면 제한 없음)", min_value=0, step=10)
pyeong = st.slider("전용면적 (평)", 0, 60, (0, 60))
months = st.select_slider("기간", options=[3, 6, 12], value=6,
                          format_func=lambda x: f"최근 {x}개월")
apt_kw = st.text_input("단지명 검색 (선택)", placeholder="예: 대림")

if st.button("조회하기", type="primary"):
    st.session_state.searched = True
if not st.session_state.get("searched"):
    st.info("조건을 고르고 조회하기를 누르세요.")
    st.stop()

try:
    budget = parse_money(budget_text)
except ValueError:
    st.error("예산은 4억5천, 45000 처럼 적어 주세요.")
    st.stop()

# ── 데이터 불러오기 ──────────────────────────────────
api_kind = "매매" if kind == "매매" else "전월세"
rows = []
with st.spinner("실거래가를 불러오는 중..."):
    try:
        for ymd in recent_months(max(months, TREND_MONTHS)):
            rows += fetch_month(api_kind, lawd_cd, ymd)
    except Exception as e:
        st.error(f"데이터를 불러오지 못했어요: {e}")
        st.stop()

df = pd.DataFrame(rows)
if df.empty:
    st.warning("이 지역, 이 기간에 신고된 거래가 없어요.")
    st.stop()

# ── 정리와 필터 ──────────────────────────────────────
df["평"] = (df["area"].map(to_num) / 3.3058).round(1)
df["계약일"] = pd.to_datetime(
    df["year"] + "-" + df["month"].str.zfill(2) + "-" + df["day"].str.zfill(2),
    errors="coerce",
)
df = df[df["cancel"].str.upper() != "O"].copy()  # 해제(취소)된 거래 제외

if kind == "매매":
    df["가격"] = df["price"].map(to_num)
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
df = df[(df["평"] >= lo) & (df["평"] <= (hi if hi < 60 else 1e9))]
df["평형"] = df["평"].round().astype(int).astype(str) + "평"

# 가격 변동은 예산과 상관없이 같은 단지·평형 전체 거래로 계산
base = df.copy()
val_col = "월세" if kind == "월세" else "가격"   # 월세는 월세 금액의 변동을 봄

# 화면에 보여줄 거래는 고른 기간과 예산으로 한 번 더 거름
df = df[df["계약일"] >= month_start(recent_months(months)[-1])]
if budget:
    df = df[df["가격"] <= budget]
if rent_max:
    df = df[df["월세"] <= rent_max]

# ── 결과 ────────────────────────────────────────────
st.subheader(f"조건에 맞는 거래 {len(df)}건")
if df.empty:
    st.warning("조건에 맞는 거래가 없어요. 예산이나 평수, 기간을 넓혀 보세요.")
    st.stop()

label = "매매가" if kind == "매매" else "보증금"
c1, c2 = st.columns(2)
c1.metric(f"{label} 중간값", money(df["가격"].median()))
c2.metric("최저 ~ 최고", f"{money(df['가격'].min())} ~ {money(df['가격'].max())}")

agg = {
    "동": ("dong", "last"),
    "거래수": ("가격", "size"),
    "최근가": ("가격", "last"),
    "최저": ("가격", "min"),
    "최고": ("가격", "max"),
    "최근계약": ("계약일", "last"),
}
if kind == "월세":
    agg["최근월세"] = ("월세", "last")
summary = (df.sort_values("계약일").groupby(["apt", "평형"]).agg(**agg)
           .reset_index().sort_values("최근계약", ascending=False))
for col in ["최근가", "최저", "최고"]:
    summary[col] = summary[col].map(money)
summary["최근계약"] = summary["최근계약"].dt.strftime("%y.%m.%d")
if kind == "월세":
    summary["최근월세"] = summary["최근월세"].map(lambda v: f"{int(v)}만")

# ── 가격 변동: 최근 3개월 중간값 vs 그 이전(4~12개월 전) 중간값 ──
recent_start = month_start(recent_months(3)[-1])
new = base[base["계약일"] >= recent_start].groupby(["apt", "평형"])[val_col].agg(["median", "size"])
old = base[base["계약일"] < recent_start].groupby(["apt", "평형"])[val_col].agg(["median", "size"])
chg = new.join(old, lsuffix="_new", rsuffix="_old", how="inner")


def change_text(r):
    pct = (r["median_new"] / r["median_old"] - 1) * 100
    mark = "▲" if pct > 0.5 else ("▼" if pct < -0.5 else "→")
    note = " (거래 적음)" if min(r["size_new"], r["size_old"]) < 2 else ""
    return f"{mark} {pct:+.1f}%{note}"


chg["변동"] = chg.apply(change_text, axis=1) if not chg.empty else []
summary = summary.merge(chg[["변동"]].reset_index(), on=["apt", "평형"], how="left")
summary["변동"] = summary["변동"].fillna("-")
summary = summary.rename(columns={"apt": "단지"})

st.markdown("**단지·평형별 요약** (최근 거래 순)")
st.dataframe(summary, hide_index=True)
what = "월세" if kind == "월세" else label
st.caption(f"변동: 최근 3개월 {what} 중간값을 그 이전 9개월 중간값과 비교한 값. "
           "거래가 적으면 한두 건에 크게 흔들리니 참고만 하세요.")

# ── 단지 하나의 월별 시세 흐름 ──
st.markdown(f"**단지 시세 흐름** (최근 {TREND_MONTHS}개월, 월별 중간값)")
pick = st.selectbox("단지·평형 선택", (summary["단지"] + " " + summary["평형"]).tolist())
apt_name, py = pick.rsplit(" ", 1)
one = base[(base["apt"] == apt_name) & (base["평형"] == py)].copy()
one["월"] = one["계약일"].dt.to_period("M").dt.to_timestamp()
monthly = one.groupby("월")[val_col].median().rename(f"{what} (만원)")
st.line_chart(monthly)
st.caption(f"이 단지·평형 거래 {len(one)}건. 거래가 없는 달은 앞뒤 점을 이어서 그립니다.")

with st.expander("개별 거래 전체 보기"):
    cols = ["계약일", "apt", "dong", "평", "floor", "가격"] + (["월세"] if kind == "월세" else [])
    detail = df.sort_values("계약일", ascending=False)[cols].copy()
    detail["계약일"] = detail["계약일"].dt.strftime("%y.%m.%d")
    detail["가격"] = detail["가격"].map(money)
    if kind == "월세":
        detail["월세"] = detail["월세"].map(lambda v: f"{int(v)}만")
    detail = detail.rename(columns={"apt": "단지", "dong": "동", "floor": "층", "가격": label})
    st.dataframe(detail, hide_index=True)

st.caption("실거래가는 신고까지 시차가 있어 최근 한두 달 거래가 빠질 수 있고, "
           "현재 호가와 다를 수 있습니다. 전월세는 확정일자 등으로 신고된 거래만 포함됩니다.")