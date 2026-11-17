import datetime as dt
import html as html_lib
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="실거래가 시세 조회", page_icon="🏠", layout="centered")

# ── 설정 ─────────────────────────────────────────────
SERVICE_KEY = st.secrets["SERVICE_KEY"]  # .streamlit/secrets.toml 에서 읽음
DEFAULT_GU = "부산진구"  # 처음 열 때 선택돼 있을 구
TREND_MONTHS = 12      # 가격 변동 계산에 쓰는 기간(개월)
REGION_PREFIX = "부산"  # 주소 검색에 붙일 시·도 이름
KAKAO_KEY = st.secrets.get("KAKAO_KEY", "")  # 주변 시설 검색용 (없으면 그 칸만 숨김)
SHEET_URL = st.secrets.get("SHEET_URL", "")  # 조건 저장용 구글 시트 주소 (없으면 저장 기능 숨김)
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")  # 앱 비밀번호 (없으면 잠금 없음)

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
    "jibun": ["jibun", "지번"],
    "build": ["buildYear", "건축년도"],
    "dealing": ["dealingGbn", "거래유형"],      # 매매: 중개거래 / 직거래
    "ctype": ["contractType", "계약구분"],      # 전월세: 신규 / 갱신
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
    """예산 입력을 만원 단위 숫자로 바꿉니다.
    숫자만 적으면 억 단위로 읽음: '4.5' → 4.5억. '4억5천', '3000만'처럼 단위를 붙여도 됩니다.
    (숫자만 1000 이상이면 예전처럼 만원 단위로 읽음: '45000' → 4.5억)"""
    s = re.sub(r"[,\s원]", "", text or "")
    if not s:
        return None
    has_unit = any(u in s for u in "억천만")
    total = 0.0
    if "억" in s:
        a, s = s.split("억", 1)
        total += float(a or 0) * 10000
    s = s.replace("만", "")
    if "천" in s:
        total += float(s.replace("천", "") or 0) * 1000
    elif s:
        n = float(s)
        total += n if (has_unit or n >= 1000) else n * 10000
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


def eok(v):
    """만원 단위 숫자를 억 단위로 (소수점 셋째 자리까지, 끝의 0은 뺌). 예: 47250 → 4.725억"""
    if v is None or pd.isna(v):
        return "-"
    text = f"{v / 10000:.3f}".rstrip("0").rstrip(".")
    return f"{text}억"


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


# ── 카카오 지도: 단지 위치와 주변 시설 ─────────────────
@st.cache_data(ttl=60 * 60 * 24 * 7, show_spinner=False)  # 같은 검색은 7일 동안 저장
def kakao(kind, **params):
    r = requests.get(f"https://dapi.kakao.com/v2/local/search/{kind}.json",
                     headers={"Authorization": f"KakaoAK {KAKAO_KEY}"}, params=params, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:150]}")
    return r.json()


def locate(address, keyword):
    for kind, q in (("address", address), ("keyword", keyword)):
        docs = kakao(kind, query=q).get("documents", [])
        if docs:
            return docs[0]["x"], docs[0]["y"]
    return None


def nearby(x, y, radius, code=None, query=None):
    params = {"x": x, "y": y, "radius": radius, "sort": "distance"}
    if code:
        data = kakao("category", category_group_code=code, **params)
    else:
        data = kakao("keyword", query=query, **params)
    return data.get("documents", []), data.get("meta", {}).get("total_count", 0)


def dist(m):
    m = int(m)
    return f"{m}m" if m < 1000 else f"{m / 1000:.1f}km"


def places(docs, n):
    return ", ".join(f"{d['place_name']} {dist(d['distance'])}" for d in docs[:n])


# ── 손님용 공개 모드 (Secrets에 PUBLIC_MODE = true 일 때) ──
PUBLIC_MODE = bool(st.secrets.get("PUBLIC_MODE", False))
OFFICE = {
    "name": st.secrets.get("OFFICE_NAME", "온누리부동산공인중개사사무소"),
    "phone": st.secrets.get("OFFICE_PHONE", "051-866-2009"),
    "addr": st.secrets.get("OFFICE_ADDR", "부산광역시 부산진구 양연로 36 상가 1-3호 (양정동, 양정포레힐즈스위첸 1단지)"),
    "ceo": st.secrets.get("OFFICE_CEO", "김애덕"),
    "reg": st.secrets.get("OFFICE_REG", "26230-2026-00071"),
    "map": st.secrets.get("OFFICE_MAP", "https://naver.me/539d3Fo1"),
}
if PUBLIC_MODE:
    # 중개 가능한 근처 구만 남김. Secrets의 PUBLIC_GU로 바꿀 수 있음 (예: ["부산진구", "연제구"])
    keep = list(st.secrets.get("PUBLIC_GU", ["부산진구", "연제구"]))
    REGIONS = {g: REGIONS[g] for g in keep if g in REGIONS}
    DEFAULT_GU = next(iter(REGIONS))
    APP_PASSWORD = ""  # 손님용은 잠금 없음
    SHEET_URL = ""     # 손님용은 조건 저장 없음


def call_button(label=None):
    if OFFICE["phone"]:
        label = label or f"전화 상담 {OFFICE['phone']}"
        st.link_button(label, "tel:" + re.sub(r"[^0-9+]", "", OFFICE["phone"]),
                       type="primary", use_container_width=True)


def office_footer():
    lines = [f"상호 {OFFICE['name']}"]
    if OFFICE["ceo"] or OFFICE["reg"]:
        lines.append(f"대표 공인중개사 {OFFICE['ceo'] or '-'} · 등록번호 {OFFICE['reg'] or '-'}")
    lines.append(f"소재지 {OFFICE['addr']}")
    if OFFICE["phone"]:
        lines.append(f"전화 {OFFICE['phone']}")
    st.divider()
    st.caption("  \n".join(lines))


# ── 비밀번호 잠금 ────────────────────────────────────
if PUBLIC_MODE:
    st.title(f"{OFFICE['name'].replace('공인중개사사무소', '')} 실거래가 조회")
    st.write(f"{OFFICE['name']} · {OFFICE['addr']}")
    b1, b2 = st.columns(2)
    with b1:
        call_button()
    b2.link_button("네이버 지도로 길찾기", OFFICE["map"], use_container_width=True)
else:
    st.title("실거래가 시세 조회")
if APP_PASSWORD and not st.session_state.get("authed"):
    pw = st.text_input("비밀번호", type="password")
    if pw == APP_PASSWORD:
        st.session_state.authed = True
        st.rerun()
    elif pw:
        st.error("비밀번호가 달라요.")
    st.stop()
st.caption("국토교통부 실거래가 공개 자료 기준, 참고용 시세입니다.")


# ── 조건 저장하기 (구글 시트) ────────────────────────
PRESET_COLS = ["이름", "구", "지역코드", "동", "거래종류", "예산", "월세최대",
               "평수최소", "평수최대", "기간", "단지명", "저장일"]
DEFAULTS = {"gu": DEFAULT_GU, "other_code": "", "dong_text": "", "choice": "전체",
            "budget_text": "", "rent_max": 0, "pyeong": (0, 60), "months": 6, "apt_kw": ""}
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)


@st.cache_resource(show_spinner=False)
def sheet():
    import gspread
    info = dict(st.secrets["gcp_service_account"])
    # 개인 키 줄바꿈이 글자 그대로(\\n) 들어간 경우도 읽을 수 있게 정리
    info["private_key"] = info["private_key"].replace("\\n", "\n").strip() + "\n"
    gc = gspread.service_account_from_dict(info)
    book = gc.open_by_url(SHEET_URL)
    try:
        return book.worksheet("저장한 조건")
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet("저장한 조건", rows=200, cols=len(PRESET_COLS))
        ws.append_row(PRESET_COLS)
        return ws


@st.cache_data(ttl=300, show_spinner=False)
def load_presets():
    return sheet().get_all_records(numericise_ignore=["all"])


def why(e):
    """오류 종류까지 보여줘서 원인을 찾기 쉽게."""
    name = type(e).__name__
    if name == "SpreadsheetNotFound":
        return ("시트를 찾지 못했어요. SHEET_URL 주소가 맞는지, 시트를 서비스 계정 이메일과 "
                "편집자로 공유했는지 확인해 주세요.")
    if name == "PermissionError" or "PERMISSION_DENIED" in str(e):
        detail = str(e)
        if "has not been used" in detail or "disabled" in detail:
            return ("구글 프로젝트에서 Google Sheets API(또는 Google Drive API)가 꺼져 있어요. "
                    "Google Cloud 콘솔에서 '사용'을 누르고 몇 분 뒤 다시 시도해 주세요.")
        return ("시트에 접근 권한이 없어요. 서비스 계정 이메일을 편집자로 공유했는지, "
                f"SHEET_URL이 그 시트 주소인지 확인해 주세요. (원문: {detail[:200]})")
    return f"{name}: {e}"


def find_row(name):
    names_col = sheet().col_values(1)
    return names_col.index(name) + 1 if name in names_col[1:] else None


def save_preset():
    ss = st.session_state
    name = ss.get("preset_name", "").strip()
    if not name:
        ss.preset_msg = ("warning", "저장할 이름을 적어 주세요.")
        return
    row = [name, ss.gu, ss.other_code, ss.dong_text, ss.choice, ss.budget_text,
           ss.get("rent_max", 0), ss.pyeong[0], ss.pyeong[1], ss.months, ss.apt_kw,
           dt.date.today().isoformat()]
    row = [str(v) for v in row]
    try:
        r = find_row(name)
        if r:
            sheet().update(range_name=f"A{r}:L{r}", values=[row], value_input_option="RAW")
        else:
            sheet().append_row(row, value_input_option="RAW")
        load_presets.clear()
        ss.preset_msg = ("success", f"'{name}' 조건을 저장했어요.")
    except Exception as e:
        ss.preset_msg = ("error", f"저장하지 못했어요: {why(e)}")


def apply_preset():
    ss = st.session_state
    p = next((x for x in load_presets() if x["이름"] == ss.get("preset_pick")), None)
    if not p:
        return

    def num(v, default):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    ss.gu = p["구"] if p["구"] in REGIONS else DEFAULT_GU
    ss.other_code = p["지역코드"]
    ss.dong_text = p["동"]
    ss.choice = p["거래종류"] if p["거래종류"] in ("전체", "매매", "전세", "월세") else "전체"
    ss.budget_text = p["예산"]
    ss.rent_max = num(p["월세최대"], 0)
    ss.pyeong = (num(p["평수최소"], 0), num(p["평수최대"], 60))
    ss.months = min(max(num(p["기간"], 6), 1), 12)
    ss.apt_kw = p["단지명"]
    ss.searched = True
    ss.preset_msg = ("success", f"'{p['이름']}' 조건을 불러왔어요.")


def delete_preset():
    ss = st.session_state
    name = ss.get("preset_pick")
    try:
        r = find_row(name)
        if r:
            sheet().delete_rows(r)
        load_presets.clear()
        ss.preset_msg = ("success", f"'{name}' 조건을 지웠어요.")
    except Exception as e:
        ss.preset_msg = ("error", f"지우지 못했어요: {why(e)}")


if SHEET_URL:
    with st.expander("저장한 조건"):
        try:
            presets = load_presets()
        except Exception as e:
            presets = []
            st.error(f"저장 공간에 연결하지 못했어요: {why(e)}")
        if presets:
            st.selectbox("불러올 조건", [p["이름"] for p in presets], key="preset_pick")
            p1, p2 = st.columns(2)
            p1.button("불러오기", on_click=apply_preset, type="primary")
            p2.button("지우기", on_click=delete_preset)
        else:
            st.caption("아직 저장한 조건이 없어요.")
        st.text_input("지금 조건을 저장할 이름", key="preset_name",
                      placeholder="예: 김OO 손님 전세 (실명·전화번호는 적지 마세요)")
        st.button("지금 조건 저장", on_click=save_preset)
        st.caption("같은 이름으로 저장하면 덮어써요.")
    msg = st.session_state.pop("preset_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])


# ── 입력 화면 ────────────────────────────────────────
gu_names = list(REGIONS)
gu = st.selectbox("구", gu_names, key="gu")
if PUBLIC_MODE:
    other_code = ""
else:
    with st.expander("부산 외 지역"):
        other_code = st.text_input("지역코드 5자리", placeholder="예: 48121 (창원시 의창구)", key="other_code")
lawd_cd = other_code.strip() or REGIONS[gu]

dong_text = st.text_input("동 (선택, 여러 개는 띄어서)", placeholder="예: 연산동 거제동", key="dong_text")
choice = st.radio("거래 종류", ["전체", "매매", "전세", "월세"], horizontal=True, key="choice")
budget_text = st.text_input("예산 최대 (억, 선택)", placeholder="억 단위, 예: 4.5 (매매는 매매가, 전월세는 보증금 기준)",
                            key="budget_text")
rent_max = 0
if choice in ("월세", "전체"):
    rent_max = st.number_input("월세 최대 (만원, 0이면 제한 없음)", min_value=0, step=10, key="rent_max")
pyeong = st.slider("전용면적 (평)", 0, 60, key="pyeong")
months = st.slider("기간 (최근 몇 개월)", 1, 12, key="months")
apt_kw = st.text_input("단지명 검색 (선택)", placeholder="예: 롯데캐슬", key="apt_kw")

if st.button("조회하기", type="primary"):
    st.session_state.searched = True
if not st.session_state.get("searched"):
    st.info("조건을 고르고 조회하기를 누르세요.")
    st.stop()

try:
    budget = parse_money(budget_text)
except ValueError:
    st.error("예산은 4.5 처럼 억 단위 숫자로 적어 주세요.")
    st.stop()


# ── 데이터 불러오기와 정리 ─────────────────────────────
def load(api_kind):
    rows = []
    for ymd in recent_months(max(months + 1, TREND_MONTHS)):
        rows += fetch_month(api_kind, lawd_cd, ymd)
    df = pd.DataFrame(rows)
    for k in FIELDS:  # 예전에 저장된 데이터에 없는 칸은 빈칸으로 채움
        if k not in df:
            df[k] = ""
    return df


def clean(raw, kind):
    """취소 거래를 빼고, 거래 종류와 동·단지·평수 조건으로 거른 12개월 치 거래."""
    df = raw.copy()
    df["평"] = (df["area"].map(to_num) / 3.3058).round(1)
    df["계약일"] = pd.to_datetime(
        df["year"] + "-" + df["month"].str.zfill(2) + "-" + df["day"].str.zfill(2),
        errors="coerce",
    )
    df = df[df["cancel"].str.upper() != "O"].copy()
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
    return df


def in_period(base, kind):
    """고른 기간과 예산으로 한 번 더 거른 거래 (화면 표시용)."""
    df = base[base["계약일"] >= pd.Timestamp(dt.date.today()) - pd.DateOffset(months=months)]
    if budget:
        df = df[df["가격"] <= budget]
    if rent_max and kind == "월세":
        df = df[df["월세"] <= rent_max]
    return df


kinds = ["매매", "전세", "월세"] if choice == "전체" else [choice]
bases = {}
with st.spinner("실거래가를 불러오는 중..."):
    try:
        raw = {}
        if "매매" in kinds:
            raw["매매"] = load("매매")
        if "전세" in kinds or "월세" in kinds:
            raw["전월세"] = load("전월세")
    except Exception as e:
        st.error(f"데이터를 불러오지 못했어요: {e}")
        st.stop()
for k in kinds:
    r = raw["매매" if k == "매매" else "전월세"]
    bases[k] = clean(r, k) if not r.empty else pd.DataFrame()


# ── 공통 도우미 ──────────────────────────────────────
def names(kind):
    label = "매매가" if kind == "매매" else "보증금"
    what = "월세" if kind == "월세" else label
    val_col = "월세" if kind == "월세" else "가격"   # 월세는 월세 금액의 변동을 봄
    return label, what, val_col


def won(v, kind):
    """매매가·전세 보증금은 억 단위, 월세 보증금은 기존 표기(예: 1억 5,000)."""
    return money(v) if kind == "월세" else eok(v)


def amount(v, val_col):
    return eok(v) if val_col == "가격" else f"{int(v)}만"


def fmt(kind, price, rent):
    return won(price, kind) + (f" / {int(rent)}만" if kind == "월세" else "")


def change_table(base, val_col):
    """최근 3개월 중간값 vs 그 이전(4~12개월 전) 중간값."""
    start = month_start(recent_months(3)[-1])
    new = base[base["계약일"] >= start].groupby(["apt", "평형"])[val_col].agg(["median", "size"])
    old = base[base["계약일"] < start].groupby(["apt", "평형"])[val_col].agg(["median", "size"])
    chg = new.join(old, lsuffix="_new", rsuffix="_old", how="inner")

    def text(r):
        pct = (r["median_new"] / r["median_old"] - 1) * 100
        mark = "▲" if pct > 0.5 else ("▼" if pct < -0.5 else "→")
        note = " (거래 적음)" if min(r["size_new"], r["size_old"]) < 2 else ""
        return f"{mark} {pct:+.1f}%{note}"

    chg["변동"] = chg.apply(text, axis=1) if not chg.empty else []
    return chg


def show_nearby(dong, jibun, apt_name):
    if not KAKAO_KEY:
        return
    area = REGION_PREFIX if other_code.strip() else f"{REGION_PREFIX} {gu}"
    try:
        loc = locate(f"{area} {dong} {jibun}", f"{area} {dong} {apt_name}")
        if not loc:
            st.caption("단지 위치를 찾지 못해 주변 시설을 표시하지 못했어요.")
            return
        x, y = loc
        subway, _ = nearby(x, y, 2000, code="SW8")
        bus, _ = nearby(x, y, 1000, query="버스정류장")
        mart, _ = nearby(x, y, 3000, code="MT1")
        _, n_cs = nearby(x, y, 500, code="CS2")
        _, n_cafe = nearby(x, y, 500, code="CE7")
    except Exception as e:
        st.caption(f"주변 시설을 불러오지 못했어요: {e}")
        return
    lines = [
        "지하철역: " + (places(subway, 2) or "2km 안에 없음"),
        "버스정류장: " + (places(bus, 1) or "1km 안에 없음"),
        "대형마트: " + (places(mart, 2) or "3km 안에 없음"),
        f"500m 안 편의점 {n_cs}곳, 카페 {n_cafe}곳",
    ]
    st.markdown("**주변 시설** (단지에서 직선거리, 실제 걷는 거리는 더 길어요)")
    st.markdown("\n".join(f"- {t}" for t in lines))


# ── 단지 상세 정보 ───────────────────────────────────
def show_detail(kind, base, chg, apt_name, py):
    label, what, val_col = names(kind)
    one = base[(base["apt"] == apt_name) & (base["평형"] == py)].sort_values("계약일", ascending=False)
    if one.empty:
        st.warning("이 단지·평형의 최근 12개월 거래가 없어요.")
        return
    first = one.iloc[0]
    st.markdown(f"### {apt_name} {py} ({kind})")

    by = int(to_num(first["build"]))
    age = dt.date.today().year - by + 1 if by else None
    info = [f"{first['dong']} {first['jibun']}".strip(), f"전용 {one['평'].median():.1f}평"]
    if age:
        info.insert(1, f"{by}년 준공, {age}년차")
    st.write(" / ".join(info))

    if PUBLIC_MODE:
        call_button(f"{apt_name} {py} {kind} 상담 문의")
    else:
        q = f"{first['dong']} {apt_name}"
        b1, b2 = st.columns(2)
        b1.link_button("네이버 부동산 매물", "https://m.land.naver.com/search/result/" + quote(q))
        b2.link_button("네이버 검색", "https://search.naver.com/search.naver?query=" + quote(q + " 아파트"))

    show_nearby(first["dong"], first["jibun"], apt_name)

    c1, c2, c3 = st.columns(3)
    c1.metric("최근 거래", fmt(kind, first["가격"], first["월세"]))
    mid = one[val_col].median()
    c2.metric(f"12개월 {what} 중간값", amount(mid, val_col))
    c3.metric("3개월 변동", chg["변동"].get((apt_name, py), "-") if not chg.empty else "-")

    feats = []
    if age:
        if age <= 5:
            feats.append(f"준공 {age}년차 신축 단지예요.")
        elif age <= 10:
            feats.append(f"준공 {age}년차로 비교적 새 단지예요.")
        elif age >= 30:
            feats.append(f"준공 {age}년차 오래된 단지예요. 재건축 진행 상황은 따로 확인해 보세요.")
        elif age >= 20:
            feats.append(f"준공 {age}년차 구축 단지예요.")

    size = one["평"].median()
    peers = base[(base["dong"] == first["dong"]) & (base["apt"] != apt_name)
                 & ((base["평"] - size).abs() <= 3)]
    if len(peers) >= 3:
        diff = (mid / peers[val_col].median() - 1) * 100
        if abs(diff) >= 1:
            feats.append(f"같은 동 비슷한 평수(±3평) 다른 단지보다 중간값이 {abs(diff):.0f}% "
                         f"{'높아요' if diff > 0 else '낮아요'}. (비교 거래 {len(peers)}건)")

    note = " 거래가 드물어 시세가 한두 건에 좌우될 수 있어요." if len(one) < 4 else ""
    feats.append(f"최근 {TREND_MONTHS}개월 이 평형 거래는 {len(one)}건이에요.{note}")

    fl = one["floor"].map(to_num)
    if len(one) >= 6 and fl.nunique() > 2:
        hi_p = one[fl >= fl.quantile(0.67)][val_col].median()
        lo_p = one[fl <= fl.quantile(0.33)][val_col].median()
        if lo_p:
            d = (hi_p / lo_p - 1) * 100
            if abs(d) >= 1:
                feats.append(f"높은 층이 낮은 층보다 약 {abs(d):.0f}% {'비싸게' if d > 0 else '싸게'} 거래됐어요.")

    if kind == "매매":
        direct = (one["dealing"] == "직거래").sum()
        if direct:
            feats.append(f"직거래 {direct}건이 섞여 있어요. 가족 간 거래 등은 시세보다 낮을 수 있어요.")
    else:
        renew = one["ctype"].str.contains("갱신").sum()
        new_c = one[one["ctype"].str.contains("신규")]
        if renew:
            feats.append(f"{len(one)}건 중 {renew}건이 갱신 계약이에요. 갱신은 인상 폭이 제한돼 "
                         "신규 계약 시세보다 낮게 잡히는 편이에요.")
        if renew and len(new_c):
            feats.append(f"신규 계약 {len(new_c)}건만 보면 {what} 중간값은 "
                         f"{amount(new_c[val_col].median(), val_col)}이에요.")

    if not PUBLIC_MODE:  # 단지 평가처럼 읽힐 수 있는 문구는 손님용에서 숨김
        st.markdown("**특징**")
        st.markdown("\n".join(f"- {f}" for f in feats))

    one = one.copy()
    one["월"] = one["계약일"].dt.to_period("M").dt.to_timestamp()
    unit = "억" if val_col == "가격" else "만원"
    monthly = one.groupby("월")[val_col].median()
    if val_col == "가격":
        monthly = (monthly / 10000).round(3)
    st.markdown(f"**월별 {what} 흐름** (최근 {TREND_MONTHS}개월 중간값, {unit})")
    st.line_chart(monthly.rename(f"{what} ({unit})"))

    t = pd.DataFrame({
        "계약일": one["계약일"].dt.strftime("%y.%m.%d"),
        "층": one["floor"],
        label: one["가격"].map(lambda v: won(v, kind)),
    })
    if kind == "월세":
        t["월세"] = one["월세"].map(lambda v: f"{int(v)}만")
    t["구분"] = one["dealing"] if kind == "매매" else one["ctype"]
    st.markdown("**최근 거래**")
    st.dataframe(t, hide_index=True)
    st.caption("실거래 자료로 계산한 내용이에요. 역세권, 학군, 향 같은 정보는 실거래 자료에 없어서 넣지 않았어요.")


# ── 거래 종류 하나의 결과 화면 ───────────────────────
def render(kind, base):
    label, what, val_col = names(kind)
    df = in_period(base, kind) if not base.empty else base
    st.subheader(f"{kind}: 조건에 맞는 거래 {len(df)}건")
    if df.empty:
        st.warning("조건에 맞는 거래가 없어요. 예산이나 평수, 기간을 넓혀 보세요.")
        return

    c1, c2 = st.columns(2)
    c1.metric(f"{label} 중간값", won(df["가격"].median(), kind))
    c2.metric("최저 ~ 최고", f"{won(df['가격'].min(), kind)} ~ {won(df['가격'].max(), kind)}")

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
        summary[col] = summary[col].map(lambda v: won(v, kind))
    summary["최근계약"] = summary["최근계약"].dt.strftime("%y.%m.%d")
    if kind == "월세":
        summary["최근월세"] = summary["최근월세"].map(lambda v: f"{int(v)}만")

    chg = change_table(base, val_col)
    summary = summary.merge(chg[["변동"]].reset_index(), on=["apt", "평형"], how="left")
    summary["변동"] = summary["변동"].fillna("-")
    summary = summary.rename(columns={"apt": "단지"})

    st.markdown("**단지·평형별 요약** (줄 왼쪽을 누르면 상세 정보)")
    event = st.dataframe(summary, hide_index=True, on_select="rerun",
                         selection_mode="single-row", key=f"summary_{kind}")
    st.caption(f"변동: 최근 3개월 {what} 중간값을 그 이전 9개월 중간값과 비교한 값. "
               "거래가 적으면 한두 건에 크게 흔들리니 참고만 하세요.")

    if event.selection.rows:
        sel = summary.iloc[event.selection.rows[0]]
        show_detail(kind, base, chg, sel["단지"], sel["평형"])
    else:
        st.info("요약표에서 단지 줄 왼쪽 칸을 누르면 그 단지의 상세 정보와 특징이 나와요.")

    with st.expander("개별 거래 전체 보기"):
        cols = ["계약일", "apt", "dong", "평", "floor", "가격"] + (["월세"] if kind == "월세" else [])
        detail = df.sort_values("계약일", ascending=False)[cols].copy()
        detail["계약일"] = detail["계약일"].dt.strftime("%y.%m.%d")
        detail["가격"] = detail["가격"].map(lambda v: won(v, kind))
        if kind == "월세":
            detail["월세"] = detail["월세"].map(lambda v: f"{int(v)}만")
        detail = detail.rename(columns={"apt": "단지", "dong": "동", "floor": "층", "가격": label})
        st.dataframe(detail, hide_index=True)


# ── 세 가지 한눈에 비교 ──────────────────────────────
def render_compare(bases):
    parts = []
    for kind in ["매매", "전세", "월세"]:
        base = bases[kind]
        if base.empty:
            continue
        df = in_period(base, kind).sort_values("계약일")
        if df.empty:
            continue
        g = df.groupby(["apt", "평형"])
        last = g.last()
        part = pd.DataFrame({
            f"{kind} 최근가": [fmt(kind, p, r) for p, r in zip(last["가격"], last["월세"])],
            f"{kind} 건수": g.size(),
        }, index=last.index)
        parts.append(part)
    if not parts:
        st.warning("조건에 맞는 거래가 없어요. 예산이나 평수, 기간을 넓혀 보세요.")
        return
    table = pd.concat(parts, axis=1)
    for kind in ["매매", "전세", "월세"]:
        if f"{kind} 건수" not in table:
            table[f"{kind} 최근가"], table[f"{kind} 건수"] = "-", 0
    table = table.fillna({"매매 최근가": "-", "전세 최근가": "-", "월세 최근가": "-"})
    for kind in ["매매", "전세", "월세"]:
        table[f"{kind} 건수"] = table[f"{kind} 건수"].fillna(0).astype(int)

    # 전세가율: 12개월 전세 중간값 ÷ 12개월 매매 중간값
    sale = bases["매매"].groupby(["apt", "평형"])["가격"].median() if not bases["매매"].empty else pd.Series(dtype=float)
    jeonse = bases["전세"].groupby(["apt", "평형"])["가격"].median() if not bases["전세"].empty else pd.Series(dtype=float)
    ratio = (jeonse / sale * 100).reindex(table.index)
    table["전세가율"] = ratio.map(lambda v: "-" if pd.isna(v) else f"{v:.0f}%")

    table["총 거래"] = table[["매매 건수", "전세 건수", "월세 건수"]].sum(axis=1)
    table = table.sort_values("총 거래", ascending=False).reset_index()
    table = table.rename(columns={"apt": "단지"})
    cols = ["단지", "평형", "매매 최근가", "전세 최근가", "월세 최근가", "전세가율",
            "매매 건수", "전세 건수", "월세 건수"]
    st.subheader(f"단지·평형 {len(table)}곳 한눈에 비교")
    st.dataframe(table[cols], hide_index=True)
    st.caption("월세 최근가는 보증금 / 월세. 전세가율은 최근 12개월 전세 중간값을 매매 중간값으로 나눈 비율이에요. "
               "전세가율이 높을수록 매매가가 내려갈 때 보증금을 돌려받기 어려워질 수 있어, "
               "전세 계약 전에 꼭 확인해 보세요. 상세 정보는 위쪽 매매·전세·월세 탭에서 볼 수 있어요.")


# ── 우리 동네 최근 거래 ──────────────────────────────
def signed_change(diff, kind):
    if diff == 0:
        return "±0"
    if kind == "월세":
        return f"{diff:+.0f}만"
    text = f"{abs(diff) / 10000:.3f}".rstrip("0").rstrip(".")
    return f"{'+' if diff > 0 else '-'}{text}억"


def render_recent(bases):
    days = st.radio("기간", [7, 14, 30], index=2, horizontal=True,
                    format_func=lambda d: f"최근 {d}일", key="recent_days")
    since = pd.Timestamp(dt.date.today()) - pd.Timedelta(days=days)
    rows = []
    for kind in ["매매", "전세", "월세"]:
        base = bases.get(kind)
        if base is None or base.empty:
            continue
        _, _, val_col = names(kind)
        b = base.sort_values("계약일").copy()
        g = b.groupby(["apt", "평형"])
        b["직전값"] = g[val_col].shift(1)
        b["직전일"] = g["계약일"].shift(1)
        for _, r in b[b["계약일"] >= since].iterrows():
            if pd.isna(r["직전값"]):
                vs = "첫 거래"
            else:
                vs = f"{signed_change(r[val_col] - r['직전값'], kind)} (직전 {r['직전일']:%y.%m.%d})"
            rows.append({
                "계약일": r["계약일"],
                "종류": kind,
                "단지": r["apt"],
                "평형": r["평형"],
                "층": r["floor"],
                "금액": fmt(kind, r["가격"], r["월세"]),
                "직전 거래 대비": vs,
                "구분": r["dealing"] if kind == "매매" else r["ctype"],
            })
    if not rows:
        st.info(f"최근 {days}일 안에 계약된 거래가 아직 없어요. 신고가 늦게 들어오니 기간을 늘려 보세요.")
        return
    feed = pd.DataFrame(rows).sort_values("계약일", ascending=False)
    feed["계약일"] = feed["계약일"].dt.strftime("%y.%m.%d")
    counts = feed["종류"].value_counts()
    st.subheader(f"최근 {days}일 계약 {len(feed)}건")
    st.write(" / ".join(f"{k} {counts.get(k, 0)}건" for k in ["매매", "전세", "월세"]))
    st.dataframe(feed, hide_index=True)
    st.caption("계약일 기준으로 최근 거래를 모았어요. 실거래 자료에는 신고한 날짜가 없어서, "
               "며칠 전 계약이 신고가 늦어 오늘 새로 나타날 수도 있어요. 자료는 6시간마다 새로 불러와요. "
               "직전 거래 대비는 같은 단지·같은 평형·같은 종류의 바로 앞 거래와 비교한 값이에요 "
               "(월세는 월세 금액 비교). 예산 조건은 적용하지 않고 동·단지·평수 조건만 적용해요.")


# ── 단지별 실거래 요약 글 (어머니용) ───────────────────
HOME_APT = st.secrets.get("HOME_APT", "양정포레힐즈")  # 단지 게시용 기본 단지 (이름 일부만 맞아도 됨)
PUBLIC_URL = st.secrets.get("PUBLIC_URL", "")  # 손님용 앱 주소 (문 앞 포스터 QR에 들어감)


def digest_period(option):
    today = dt.date.today()
    first = today.replace(day=1)
    if option == "지난달":
        start = (first - dt.timedelta(days=1)).replace(day=1)
        return pd.Timestamp(start), pd.Timestamp(first), f"{start.year}년 {start.month}월"
    if option == "이번 달":
        return (pd.Timestamp(first), pd.Timestamp(today) + pd.Timedelta(days=1),
                f"{today.year}년 {today.month}월({today.day}일까지)")
    start = today - dt.timedelta(days=30)
    return (pd.Timestamp(start), pd.Timestamp(today) + pd.Timedelta(days=1),
            f"최근 30일({start.month}월 {start.day}일~{today.month}월 {today.day}일)")


def py_sort(py):
    return int(re.sub(r"[^0-9]", "", py) or 0)


def digest_parts(bases, start, end, apt, show_floor):
    """한 단지의 거래 종류별 요약 줄. [(종류, 건수, 설명)]"""
    out = []
    for kind in ["매매", "전세", "월세"]:
        base = bases.get(kind)
        if base is None or base.empty:
            continue
        one = base[base["apt"] == apt]
        cur = one[(one["계약일"] >= start) & (one["계약일"] < end)].sort_values("계약일")
        if cur.empty:
            continue
        before = one[(one["계약일"] < start) & (one["계약일"] >= start - pd.DateOffset(months=3))]
        pieces = []
        for py in sorted(cur["평형"].unique(), key=py_sort):
            g = cur[cur["평형"] == py]
            if kind == "월세":
                items = [f"{money(p)}/{int(r)}만" + (f"({f}층)" if show_floor else "")
                         for p, r, f in zip(g["가격"], g["월세"], g["floor"])]
                text = f"{py} " + ", ".join(items)
            elif len(g) <= 2:
                items = [eok(p) + (f"({f}층)" if show_floor else "") for p, f in zip(g["가격"], g["floor"])]
                text = f"{py} " + ", ".join(items)
            else:
                text = f"{py} {len(g)}건 {eok(g['가격'].min())}~{eok(g['가격'].max())}"
            notes = []
            if kind == "전세" and g["ctype"].str.contains("갱신").any():
                notes.append(f"갱신 {g['ctype'].str.contains('갱신').sum()}건 포함")
            if kind == "매매" and (g["dealing"] == "직거래").any():
                notes.append(f"직거래 {(g['dealing'] == '직거래').sum()}건 포함")
            prev = before[before["평형"] == py]["가격"]
            if kind != "월세" and len(g) >= 2 and len(prev) >= 2:
                pct = (g["가격"].median() / prev.median() - 1) * 100
                if abs(pct) >= 0.5:
                    notes.append(f"직전 3개월보다 {pct:+.1f}%")
            if notes:
                text += " (" + ", ".join(notes) + ")"
            pieces.append(text)
        out.append((kind, len(cur), pieces))
    return out


def office_lines():
    lines = [OFFICE["name"]]
    if OFFICE["ceo"] or OFFICE["reg"]:
        lines.append(f"대표 공인중개사 {OFFICE['ceo'] or '[성명]'} · 등록번호 {OFFICE['reg'] or '[등록번호]'}")
    lines.append(OFFICE["addr"])
    lines.append(f"전화 {OFFICE['phone'] or '[전화번호]'}")
    return lines


def build_digest(style, bases, start, end, label, apts, area, show_floor):
    source = (f"※ 국토교통부 실거래가 공개 자료 기준({dt.date.today().month}월 {dt.date.today().day}일 조회). "
              "계약 후 30일 안에 신고하면 되기 때문에 늦게 신고된 거래는 빠져 있을 수 있어요.\n"
              "※ 평형은 전용면적 기준이에요.")
    if style == "단지 게시":
        apt = apts[0]
        lines = [f"{apt} {label} 실거래 소식", ""]
        for kind, n, pieces in digest_parts(bases, start, end, apt, show_floor):
            lines.append(f"[{kind} {n}건]")
            lines += [f"  · 전용 {p}" for p in pieces]
            lines.append("")
        lines += [source, "", " · ".join([OFFICE["name"], f"전화 {OFFICE['phone'] or '[전화번호]'}"])]
        return "\n".join(lines)

    totals = {"매매": 0, "전세": 0, "월세": 0}
    blocks = []
    for apt in apts:
        parts = digest_parts(bases, start, end, apt, show_floor)
        if not parts:
            continue
        for kind, n, _ in parts:
            totals[kind] += n
        dong = ""
        for kind in ["매매", "전세", "월세"]:
            b = bases.get(kind)
            if b is not None and not b.empty and (b["apt"] == apt).any():
                dong = b[b["apt"] == apt]["dong"].mode().iat[0]
                break
        blocks.append((apt, dong, parts))
    count_line = " / ".join(f"{k} {v}건" for k, v in totals.items() if v)

    if style == "당근·카톡":
        lines = [f"[{area}] {label} 아파트 실거래 소식", count_line, ""]
        for apt, dong, parts in blocks:
            short = ", ".join(f"{k} {n}건" for k, n, _ in parts)
            lines.append(f"· {apt}({dong}): {short}")
            for k, n, pieces in parts:
                lines.append(f"   {k} {' / '.join(pieces)}")
        lines += ["", "평형은 전용면적 기준, 국토교통부 실거래가 공개 자료예요.",
                  "지금 나와 있는 매물과 정확한 시세는 편하게 물어보세요.",
                  f"{OFFICE['name']} {OFFICE['phone'] or '[전화번호]'}"]
        return "\n".join(lines)

    # 블로그
    lines = [f"[{area}] {label} 아파트 실거래 정리", "",
             f"안녕하세요, {OFFICE['name'].replace('공인중개사사무소', '')}입니다.",
             f"{label} 신고된 {area} 아파트 실거래를 단지별로 정리했어요. ({count_line})", ""]
    for apt, dong, parts in blocks:
        lines.append(f"■ {apt} ({dong})")
        for kind, n, pieces in parts:
            lines.append(f"· {kind} {n}건: " + " / ".join(pieces))
        lines.append("")
    lines += [source, "", "지금 나와 있는 매물과 자세한 시세는 편하게 문의 주세요.", ""] + office_lines()
    return "\n".join(lines)


# ── 문 앞 포스터 (A4 인쇄용) ──────────────────────────
def poster_rows(bases, start, end, apt):
    """한 단지의 평형별 [평형, 매매, 전세, 월세] 칸."""
    cells = {}
    for kind in ["매매", "전세", "월세"]:
        b = bases.get(kind)
        if b is None or b.empty:
            continue
        cur = b[(b["apt"] == apt) & (b["계약일"] >= start) & (b["계약일"] < end)]
        for py, g in cur.groupby("평형"):
            n = len(g)
            if kind == "월세":
                lo, hi = int(g["월세"].min()), int(g["월세"].max())
                if n == 1:
                    text = f"{money(g['가격'].iat[0])} / {lo}만"
                else:
                    text = f"월 {lo}만" if lo == hi else f"월 {lo}~{hi}만"
                    text += f" ({n}건)"
            else:
                lo, hi = g["가격"].min(), g["가격"].max()
                text = eok(lo) if lo == hi else f"{eok(lo)}~{eok(hi)}"
                if n > 1:
                    text += f" ({n}건)"
            cells.setdefault(py, {})[kind] = text
    return [[py] + [cells[py].get(k, "-") for k in ["매매", "전세", "월세"]]
            for py in sorted(cells, key=py_sort)]


def build_poster(bases, start, end, label, area, home, others):
    e = html_lib.escape
    max_rows = 16
    def table(apts, show_name):
        body, used = [], 0
        for apt in apts:
            rows = poster_rows(bases, start, end, apt)
            if not rows or used + len(rows) > max_rows:
                continue
            used += len(rows)
            for i, r in enumerate(rows):
                name = (f'<th scope="row" rowspan="{len(rows)}">{e(apt)}</th>'
                        if show_name and i == 0 else "")
                body.append("<tr>" + name + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>")
        head = ("<th>단지</th>" if show_name else "") + "<th>전용</th><th>매매</th><th>전세</th><th>월세 (보증금/월)</th>"
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>", used

    home_html, _ = table([home], False) if home else ("", 0)
    others_html, used = table(others, True)
    today = dt.date.today()
    office = e(OFFICE["name"])
    contact = " · ".join(x for x in [
        f"대표 공인중개사 {e(OFFICE['ceo'])}" if OFFICE["ceo"] else "",
        f"등록번호 {e(OFFICE['reg'])}" if OFFICE["reg"] else "",
        f"전화 {e(OFFICE['phone'])}" if OFFICE["phone"] else ""] if x)
    qr = ""
    if PUBLIC_URL:
        try:  # 파이썬 qrcode 라이브러리로 그림을 직접 넣음 (requirements.txt에 qrcode)
            import qrcode
            import qrcode.image.svg
            q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
            q.add_data(PUBLIC_URL)
            q.make(fit=True)
            svg = q.make_image(image_factory=qrcode.image.svg.SvgPathImage).to_string()
            svg = svg.decode("utf-8") if isinstance(svg, bytes) else svg
            svg = svg[svg.find("<svg"):]
            qr = (f'<div class="qr"><div id="qr" class="svg">{svg}</div>'
                  f'<p><b>근처 다른 단지 시세가 궁금하면<br>QR을 찍어 보세요</b><br>'
                  f'<span class="url">{e(PUBLIC_URL)}</span></p></div>')
        except Exception:
            qr = ""
    if PUBLIC_URL and not qr:  # 라이브러리가 없으면 인쇄할 때 브라우저가 QR을 그림
        qr = f"""<div class="qr"><div id="qr"></div>
<p><b>근처 다른 단지 시세가 궁금하면<br>QR을 찍어 보세요</b><br><span class="url">{e(PUBLIC_URL)}</span></p></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>try{{new QRCode(document.getElementById("qr"),{{text:{PUBLIC_URL!r},width:132,height:132,correctLevel:QRCode.CorrectLevel.M}});}}catch(err){{}}</script>"""
    home_block = (f'<section class="home"><h2>우리 단지 · {e(home)}</h2>{home_html}</section>'
                  if home_html and "<td>" in home_html else "")
    others_block = (f'<section><h2>{e(area)} 근처 단지</h2>{others_html}</section>'
                    if used else "")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>{e(label)} 실거래 소식</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700;900&display=swap">
<style>
@page {{ size: A4; margin: 12mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: "Noto Sans KR", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
  color: #1B2427; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.sheet {{ width: 186mm; min-height: 262mm; margin: 0 auto; display: flex; flex-direction: column; gap: 6mm; padding: 4mm 0; }}
header {{ border-bottom: 1.2mm solid #1E5B5A; padding-bottom: 4mm; }}
.kicker {{ margin: 0; font-size: 13pt; font-weight: 700; color: #1E5B5A; }}
h1 {{ margin: 1mm 0 0; font-size: 30pt; font-weight: 900; line-height: 1.15; }}
h2 {{ margin: 0 0 2.5mm; font-size: 15pt; font-weight: 700; }}
.home {{ background: #EAF2F1; border-radius: 3mm; padding: 4mm 5mm; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12pt; font-variant-numeric: tabular-nums; }}
th, td {{ border-bottom: 0.3mm solid #C7D1D0; padding: 2mm 2.5mm; text-align: left; vertical-align: top; }}
thead th {{ font-size: 10pt; color: #4A585C; font-weight: 700; border-bottom: 0.5mm solid #1B2427; }}
tbody th {{ font-weight: 700; width: 30%; }}
.home td {{ font-size: 13.5pt; }}
.notes {{ margin: 0; font-size: 9.5pt; line-height: 1.6; color: #4A585C; }}
footer {{ margin-top: auto; display: flex; align-items: center; justify-content: space-between; gap: 6mm;
  border-top: 0.5mm solid #1B2427; padding-top: 5mm; }}
.office b {{ font-size: 15pt; }}
.office p {{ margin: 1mm 0 0; font-size: 10.5pt; line-height: 1.6; }}
.qr {{ display: flex; align-items: center; gap: 4mm; }}
.qr p {{ margin: 0; font-size: 11pt; line-height: 1.5; }}
.qr .url {{ font-size: 8.5pt; color: #4A585C; word-break: break-all; }}
#qr img, #qr canvas, #qr svg {{ width: 32mm !important; height: 32mm !important; display: block; }}
</style></head>
<body><div class="sheet">
<header><p class="kicker">{e(area)} 아파트</p><h1>{e(label)} 실거래 소식</h1></header>
{home_block}
{others_block}
<p class="notes">국토교통부 실거래가 공개 자료 기준({today.month}월 {today.day}일 조회). 금액은 계약 금액이며 평형은 전용면적 기준이에요.
계약 후 30일 안에 신고하면 되기 때문에 늦게 신고된 거래는 빠져 있을 수 있어요. 지금 나와 있는 매물의 가격과는 다를 수 있어요.</p>
<footer><div class="office"><b>{office}</b><p>{e(OFFICE['addr'])}<br>{contact}</p>
<p>매물 상담은 편하게 들어오세요.</p></div>{qr}</footer>
</div></body></html>"""


def render_poster(bases, apts, labels, start, end, label, area):
    home_i = next((i for i, a in enumerate(apts) if HOME_APT and HOME_APT in a), None)
    options = ["(넣지 않음)"] + apts
    home = st.selectbox("맨 위에 크게 넣을 우리 단지", options,
                        index=(home_i + 1) if home_i is not None else 0,
                        format_func=lambda a: labels.get(a, a), key="pt_home")
    home = "" if home == "(넣지 않음)" else home
    rest = [a for a in apts if a != home]
    others = st.multiselect("아래 표에 넣을 근처 단지 (거래 많은 순)", rest, default=rest[:6],
                            format_func=labels.get, key="pt_others")
    if not PUBLIC_URL:
        st.warning("Secrets에 PUBLIC_URL(손님용 앱 주소)을 넣으면 포스터 아래에 QR 코드가 생겨요.")
    poster = build_poster(bases, start, end, label, area, home, others)
    st.download_button("인쇄용 파일 내려받기", poster.encode("utf-8"),
                       file_name=f"실거래소식_{label.split('(')[0].replace(' ', '')}.html",
                       mime="text/html", type="primary")
    st.caption("내려받은 파일을 열고 Ctrl+P(맥은 Cmd+P)로 A4 세로 인쇄하세요. "
               "인쇄 설정에서 '머리글과 바닥글'은 끄고 '배경 그래픽'은 켜 주세요. "
               "표에 다 들어가지 않는 단지는 자동으로 빠져요.")
    import streamlit.components.v1 as components
    components.html(poster, height=1180, scrolling=True)


def render_digest(bases):
    st.markdown("**단지별 실거래 요약 글** (블로그, 당근, 카톡, 단지 게시판에 붙여넣기용)")
    c1, c2 = st.columns(2)
    with c1:
        period = st.radio("기간", ["지난달", "이번 달", "최근 30일"], horizontal=True, key="dg_period")
    with c2:
        style = st.radio("글 종류", ["블로그", "당근·카톡", "단지 게시", "문 앞 포스터"],
                         horizontal=True, key="dg_style")
    start, end, label = digest_period(period)

    counts = {}
    for kind in ["매매", "전세", "월세"]:
        b = bases.get(kind)
        if b is None or b.empty:
            continue
        cur = b[(b["계약일"] >= start) & (b["계약일"] < end)]
        for apt, n in cur["apt"].value_counts().items():
            counts[apt] = counts.get(apt, 0) + n
    if not counts:
        st.info(f"{label}에 신고된 거래가 아직 없어요. 다른 기간을 골라 보세요.")
        return
    apts = sorted(counts, key=lambda a: -counts[a])
    labels = {a: f"{a} ({counts[a]}건)" for a in apts}

    if other_code.strip():
        area = dong_text.strip() or "우리 동네"
    else:
        area = f"{gu} {dong_text.strip()}".strip()
    if style == "문 앞 포스터":
        render_poster(bases, apts, labels, start, end, label, area)
        return

    if style == "단지 게시":
        home = next((i for i, a in enumerate(apts) if HOME_APT and HOME_APT in a), 0)
        pick = st.selectbox("단지", apts, index=home, format_func=labels.get, key="dg_home")
        chosen = [pick]
    else:
        default = apts[:10] if style == "블로그" else apts[:5]
        chosen = st.multiselect("넣을 단지 (거래 많은 순)", apts, default=default,
                                format_func=labels.get, key=f"dg_apts_{style}")
    show_floor = st.checkbox("층 표시", value=False, key="dg_floor")
    if not chosen:
        st.info("넣을 단지를 하나 이상 골라 주세요.")
        return

    text = build_digest(style, bases, start, end, label, chosen, area, show_floor)
    try:
        st.code(text, language=None, wrap_lines=True)
    except TypeError:
        st.code(text, language=None)
    st.caption("오른쪽 위 복사 버튼을 누르면 글 전체가 복사돼요. 숫자는 실거래 자료 그대로이고, "
               "평가나 추천 문구는 넣지 않았어요. 특정 매물을 함께 소개할 때는 사무소 정보와 "
               "매물의 소재지·면적·가격 등을 꼭 함께 적어야 해요.")
    if period == "지난달" and dt.date.today().day < 30:
        st.caption("지난달 거래는 이달 말까지 신고가 더 들어올 수 있어요. 월말에 한 번 더 뽑으면 더 정확해요.")


# ── 화면 그리기 ──────────────────────────────────────
if choice == "전체":
    names_ = ["최근 거래", "한눈에 비교", "매매", "전세", "월세"]
    if not PUBLIC_MODE:
        names_.insert(1, "요약 글")
    tabs = st.tabs(names_)
    with tabs[0]:
        render_recent(bases)
    rest = tabs[1:]
    if not PUBLIC_MODE:
        with rest[0]:
            render_digest(bases)
        rest = rest[1:]
    with rest[0]:
        render_compare(bases)
    for tab, kind in zip(rest[1:], kinds):
        with tab:
            render(kind, bases[kind])
else:
    render(choice, bases[choice])

st.caption("실거래가는 신고까지 시차가 있어 최근 한두 달 거래가 빠질 수 있고, "
           "현재 호가와 다를 수 있습니다. 전월세는 확정일자 등으로 신고된 거래만 포함됩니다.")
if PUBLIC_MODE:
    st.markdown("**지금 나와 있는 매물과 정확한 시세는 전화로 편하게 물어보세요.**")
    call_button()
    office_footer()
