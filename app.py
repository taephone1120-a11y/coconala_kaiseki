import re
import time
import unicodedata
from datetime import date, timedelta

import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

# =====================================================================
# 定数・マスタデータ
# =====================================================================

# 「占い」カテゴリ配下のサブカテゴリ（会話中で確認できたものを列挙）
CATEGORY_OPTIONS = {
    "占い全般": 661,
    "占術別（すべて）": 3,
    "恋愛占い": 656,
    "結婚占い": 657,
    "スピリチュアル鑑定": 658,
    "総合運鑑定": 659,
    "仕事運・仕事占い": 660,
    "夢占い": 816,
    "ペットの気持ち占い": 817,
    "占いレッスン": 80,
    "その他占い": 79,
}

# 占術（technique_ids）フィルタ
TECHNIQUE_OPTIONS = {
    "霊視・透視": 21,
    "タロット": 17,
    "リーディング": 19,
    "四柱推命": 24,
    "オラクルカード": 18,
    "手相": 27,
    "西洋占星術": 22,
    "チャネリング": 35,
    "ヒーリング": 20,
    "算命学": 30,
    "数秘術": 23,
    "ルノルマンカード": 291,
    "易占い": 28,
    "祈祷・祈願": 36,
    "エネルギーワーク": 34,
    "思念伝達": 37,
    "九星気学": 25,
    "東洋占星術": 31,
    "姓名判断": 26,
    "ツインレイ": 290,
    "宿曜": 33,
    "ルーン": 32,
    "アチューメント": 173,
    "ダウジング": 172,
    "風水": 29,
}

SERVICE_KIND_OPTIONS = {
    "すべて": None,
    "メッセージ・チャット占い": 0,
    "電話占い": 1,
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# =====================================================================
# スクレイピング用の関数群
# =====================================================================

def build_category_url(category_id, technique_ids, service_kind):
    url = f"https://coconala.com/categories/{category_id}"
    params = []
    if service_kind is not None:
        params.append(f"service_kind={service_kind}")
    for t_id in technique_ids:
        params.append(f"technique_ids%5B%5D={t_id}")
    if params:
        url += "?" + "&".join(params)
    return url


def get_service_urls_page(category_url, page):
    paged_url = f"{category_url}&page={page}" if "?" in category_url else f"{category_url}?page={page}"
    if page == 1:
        paged_url = category_url
    res = requests.get(paged_url, headers=HEADERS, timeout=10)
    soup = BeautifulSoup(res.text, "html.parser")
    links = soup.find_all("a", href=re.compile(r"^/services/\d+$"))
    urls = []
    seen_local = set()
    for link in links:
        href = "https://coconala.com" + link["href"]
        if href not in seen_local:
            seen_local.add(href)
            urls.append(href)
    return urls


def collect_service_urls(category_url, max_count, sleep_sec, stop_check=None):
    """複数ページを回ってservice URLを収集する。stop_checkはTrueで中断するcallable。"""
    all_urls = []
    seen = set()
    page = 1
    while len(all_urls) < max_count and page <= 20:
        if stop_check and stop_check():
            break
        page_urls = get_service_urls_page(category_url, page)
        if not page_urls:
            break
        for u in page_urls:
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
            if len(all_urls) >= max_count:
                break
        page += 1
        if len(all_urls) < max_count:
            time.sleep(sleep_sec)
    return all_urls[:max_count]


def parse_review_date(date_str, today):
    date_str = date_str.strip()
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_str)
    if m:
        year, month, day = map(int, m.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})月(\d{1,2})日", date_str)
    if m:
        month, day = map(int, m.groups())
        year = today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate > today:
            candidate = date(year - 1, month, day)
        return candidate
    return None


def unescape_js_string(s):
    def repl(m):
        seq = m.group(0)
        mapping = {'\\r': '\r', '\\n': '\n', '\\t': '\t', '\\"': '"', '\\\\': '\\'}
        if seq in mapping:
            return mapping[seq]
        if seq.startswith('\\u'):
            return chr(int(seq[2:], 16))
        return seq
    return re.sub(r'\\u[0-9a-fA-F]{4}|\\r|\\n|\\t|\\"|\\\\', repl, s)


def extract_full_schedule(html):
    matches = re.findall(r'schedule:"((?:[^"\\]|\\.)*)"', html)
    if not matches:
        return None
    decoded = [unescape_js_string(m) for m in matches]
    return max(decoded, key=len)


def extract_review_total(text_all):
    m = re.search(r"評価・感想[（(]([\d,]+)\s*件[）)]", text_all)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"評価\D{0,10}[\d.]+\s*\(([\d,]+)\)", text_all)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def count_visual_chars(text):
    """絵文字の異体字セレクタなどを除去してから、見た目通りの文字数を数える"""
    if not text:
        return 0
    normalized = text.replace("\r\n", "\n")
    stripped = "".join(
        c for c in normalized
        if unicodedata.category(c) != "Mn" and ord(c) not in (0xFE0E, 0xFE0F)
    )
    return len(stripped)


def scrape_coconala_service(url):
    res = requests.get(url, headers=HEADERS, timeout=10)
    html = res.text
    soup = BeautifulSoup(html, "html.parser")

    data = {"URL": url}
    today = date.today()

    h1 = soup.find("h1")
    data["サービス名"] = h1.get_text(strip=True) if h1 else None
    h2 = soup.find("h2")
    data["サービス副題"] = h2.get_text(strip=True) if h2 else None

    seller_name_tag = soup.select_one("a.c-profile_nameLink span.c-profile_name")
    data["販売者名"] = seller_name_tag.get_text(strip=True) if seller_name_tag else None

    rank_img = soup.find("img", alt=re.compile(r"^出品者ランク："))
    if rank_img:
        m = re.search(r"出品者ランク：(.+)", rank_img["alt"])
        data["ランク"] = m.group(1) if m else None
    else:
        data["ランク"] = None

    price_tag = soup.select_one("span.c-spTabMainButtonsPrice_price")
    price_raw = price_tag.get_text(strip=True) if price_tag else None
    data["価格"] = int(price_raw.replace(",", "")) if price_raw else None

    sales_tag = soup.select_one("div.c-performance_sales div.c-performance_content strong")
    if sales_tag:
        m = re.search(r"[\d,]+", sales_tag.get_text(strip=True))
        data["販売実績"] = int(m.group(0).replace(",", "")) if m else None
    else:
        data["販売実績"] = None

    total_sales_tag = soup.select_one("a.c-profile_performance span.c-profile_performance-number")
    if total_sales_tag:
        m = re.search(r"[\d,]+", total_sales_tag.get_text(strip=True))
        data["総販売実績"] = int(m.group(0).replace(",", "")) if m else None
    else:
        data["総販売実績"] = None

    free_text_blocks = soup.select("div[class*='c-contentsFreeText_text']")
    texts = [b.get_text("\n", strip=True) for b in free_text_blocks]
    service_content = texts[0] if len(texts) >= 1 else None
    purchase_note = texts[1] if len(texts) >= 2 else None
    data["サービス内容"] = service_content
    data["サービス内容文字数"] = count_visual_chars(service_content)
    data["購入にあたってのお願い"] = purchase_note

    data["スケジュール"] = extract_full_schedule(html)

    image_alts = soup.find_all("img", alt=re.compile(r"イメージ\d+"))
    first_img = None
    for img in image_alts:
        m = re.search(r"イメージ(\d+)", img["alt"])
        if m and m.group(1) == "1" and img.get("src", "").startswith("http") and first_img is None:
            first_img = img["src"]
    data["サービス1枚目画像"] = first_img

    faq_count = len(re.findall(r"回答を見る", soup.get_text()))
    data["よくある質問数"] = faq_count

    text_all = soup.get_text()
    data["評価総数"] = extract_review_total(text_all)

    reviews = []
    recent_count = 0
    cutoff = today - timedelta(days=30)
    for item in soup.select("li.c-ratingCommentsList_item"):
        date_tag = item.select_one("[class*='c-buyerCommentRow_date']")
        star_tag = item.select_one("[class*='c-ratingStars']")
        comment_tag = item.select_one("[class*='c-contentsRatingComment']")
        raw_date = date_tag.get_text(strip=True) if date_tag else None
        parsed_date = parse_review_date(raw_date, today) if raw_date else None
        if parsed_date and parsed_date >= cutoff:
            recent_count += 1
        reviews.append({
            "日付": raw_date,
            "評価スコア": star_tag.get("data-score") if star_tag else None,
            "コメント": comment_tag.get_text(strip=True) if comment_tag else None,
        })
    data["レビュー一覧"] = reviews
    data["レビュー件数(取得分)"] = len(reviews)
    data["直近1ヶ月の評価件数"] = recent_count

    return data


# =====================================================================
# Streamlit UI
# =====================================================================

st.set_page_config(page_title="ココナラ競合分析ツール", layout="wide")
st.title("🔮 ココナラ競合分析ツール")

# ---- セッション状態の初期化 ----
if "urls" not in st.session_state:
    st.session_state.urls = []
if "results" not in st.session_state:
    st.session_state.results = []
if "scrape_index" not in st.session_state:
    st.session_state.scrape_index = 0
if "running" not in st.session_state:
    st.session_state.running = False
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False
if "phase" not in st.session_state:
    st.session_state.phase = "idle"  # idle -> collecting_urls -> scraping -> done

# ---- サイドバー: 検索条件 ----
with st.sidebar:
    st.header("検索条件")

    category_name = st.selectbox("カテゴリ", list(CATEGORY_OPTIONS.keys()), index=0)
    category_id = CATEGORY_OPTIONS[category_name]

    technique_names = st.multiselect("占術で絞り込み（任意）", list(TECHNIQUE_OPTIONS.keys()))
    technique_ids = [TECHNIQUE_OPTIONS[n] for n in technique_names]

    service_kind_name = st.selectbox("提供方法", list(SERVICE_KIND_OPTIONS.keys()))
    service_kind = SERVICE_KIND_OPTIONS[service_kind_name]

    st.divider()
    st.header("取得設定")
    max_count = st.slider("取得件数", min_value=5, max_value=300, value=30, step=5)
    sleep_sec = st.slider("アクセス間隔（秒）", min_value=0.5, max_value=5.0, value=1.5, step=0.5)

    st.divider()
    start_clicked = st.button("🚀 分析開始", type="primary", use_container_width=True,
                               disabled=st.session_state.running)
    stop_clicked = st.button("⏹ 停止", use_container_width=True,
                              disabled=not st.session_state.running)

# ---- ボタン処理 ----
if start_clicked and not st.session_state.running:
    category_url = build_category_url(category_id, technique_ids, service_kind)
    st.session_state.category_url = category_url
    st.session_state.results = []
    st.session_state.scrape_index = 0
    st.session_state.running = True
    st.session_state.stop_requested = False
    st.session_state.phase = "collecting_urls"
    st.rerun()

if stop_clicked:
    st.session_state.stop_requested = True

# ---- URL収集フェーズ ----
if st.session_state.phase == "collecting_urls":
    with st.spinner("商品URLを収集中..."):
        urls = collect_service_urls(
            st.session_state.category_url,
            max_count,
            sleep_sec,
            stop_check=lambda: st.session_state.stop_requested,
        )
    st.session_state.urls = urls
    if st.session_state.stop_requested or not urls:
        st.session_state.running = False
        st.session_state.phase = "done"
    else:
        st.session_state.phase = "scraping"
    st.rerun()

# ---- 詳細スクレイピングフェーズ（1件ずつ処理してrerunする） ----
if st.session_state.phase == "scraping":
    total = len(st.session_state.urls)
    idx = st.session_state.scrape_index

    progress_area = st.empty()
    with progress_area.container():
        st.progress(idx / total if total else 0, text=f"{idx}/{total} 件取得中...")

    if st.session_state.stop_requested or idx >= total:
        st.session_state.running = False
        st.session_state.phase = "done"
        st.rerun()
    else:
        url = st.session_state.urls[idx]
        try:
            data = scrape_coconala_service(url)
            st.session_state.results.append(data)
        except Exception as e:
            st.warning(f"取得失敗: {url}（{e}）")
        st.session_state.scrape_index += 1
        time.sleep(sleep_sec)
        st.rerun()

# ---- 完了メッセージ ----
if st.session_state.phase == "done" and not st.session_state.running:
    if st.session_state.stop_requested:
        st.info(f"停止しました。{len(st.session_state.results)}件取得済みです。")
    elif st.session_state.results:
        st.success(f"完了しました。{len(st.session_state.results)}件取得しました。")

# =====================================================================
# 結果表示
# =====================================================================

results = st.session_state.results

if results:
    main_rows = [{k: v for k, v in r.items() if k != "レビュー一覧"} for r in results]
    df_main = pd.DataFrame(main_rows)

    review_rows = []
    for r in results:
        for review in r["レビュー一覧"]:
            review_rows.append({"URL": r["URL"], "サービス名": r["サービス名"], **review})
    df_reviews = pd.DataFrame(review_rows)

    st.subheader("📋 サービス一覧")
    st.dataframe(df_main, use_container_width=True)

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("💰 価格帯の分布")
        if df_main["価格"].notna().any():
            st.bar_chart(df_main["価格"].dropna())

    with col2:
        st.subheader("🏅 ランク別 件数")
        if "ランク" in df_main.columns:
            rank_counts = df_main["ランク"].value_counts()
            st.bar_chart(rank_counts)

    col3, col4 = st.columns(2)

    with col3:
        st.subheader("👤 出品者別 出品数（上位10件）")
        if "販売者名" in df_main.columns:
            seller_counts = df_main["販売者名"].value_counts().head(10)
            st.bar_chart(seller_counts)

    with col4:
        st.subheader("⭐ 評価総数 上位10件")
        if "評価総数" in df_main.columns:
            top_rated = df_main.nlargest(10, "評価総数")[["サービス名", "評価総数"]].set_index("サービス名")
            st.bar_chart(top_rated)

    st.subheader("⬇️ ダウンロード")
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        st.download_button(
            "サービス一覧をCSVでダウンロード",
            df_main.to_csv(index=False).encode("utf-8-sig"),
            file_name="coconala_services.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with dcol2:
        st.download_button(
            "レビュー詳細をCSVでダウンロード",
            df_reviews.to_csv(index=False).encode("utf-8-sig"),
            file_name="coconala_reviews.csv",
            mime="text/csv",
            use_container_width=True,
        )
else:
    st.info("左のサイドバーで条件を設定し、「分析開始」を押してください。")
