import re
import time
import unicodedata
from datetime import date, timedelta

import requests
import pandas as pd
import streamlit as st
import altair as alt
from bs4 import BeautifulSoup

# =====================================================================
# 定数
# =====================================================================

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# アクセス間隔（秒）。サーバー負荷を抑えるための固定値。UIでは変更できません。
SLEEP_SEC = 2.0


# =====================================================================
# スクレイピング用の関数群
# =====================================================================

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


def extract_breadcrumbs(soup):
    """
    パンくずリスト（nav[aria-label="breadcrumbs"]）からカテゴリ階層を取得する。
    例: ホーム > 占い > 電話占い > 恋愛占い > 相手の気持ち占い
    「ホーム」は除外し、階層を " > " で連結した文字列と、
    大カテゴリ・末端カテゴリをそれぞれ返す。
    """
    nav = soup.find("nav", attrs={"aria-label": "breadcrumbs"})
    if not nav:
        return None, None, None

    items = [a.get_text(strip=True) for a in nav.select("li a")]
    items = [t for t in items if t and t != "ホーム"]

    if not items:
        return None, None, None

    breadcrumb_str = " > ".join(items)
    top_category = items[0]
    sub_category = items[-1]
    return breadcrumb_str, top_category, sub_category


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

    breadcrumb_str, top_category, sub_category = extract_breadcrumbs(soup)
    data["カテゴリ階層"] = breadcrumb_str
    data["大カテゴリ"] = top_category
    data["サブカテゴリ"] = sub_category

    rank_img = soup.find("img", alt=re.compile(r"^出品者ランク："))
    if rank_img:
        m = re.search(r"出品者ランク：(.+)", rank_img["alt"])
        data["ランク"] = m.group(1) if m else "なし"
    else:
        data["ランク"] = "なし"

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

# ---- 全体の文字サイズを小さくするカスタムCSS ----
st.markdown(
    """
    <style>
    html, body, [class*="css"] {
        font-size: 12px !important;
    }
    h1 { font-size: 22px !important; }
    h2 { font-size: 18px !important; }
    h3 { font-size: 15px !important; }
    [data-testid="stSidebar"] * {
        font-size: 12px !important;
    }
    [data-testid="stDataFrame"] * {
        font-size: 11px !important;
    }
    button {
        font-size: 12px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

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

    category_url_input = st.text_input(
        "カテゴリ一覧ページのURL",
        placeholder="https://coconala.com/categories/3?service_kind=0&technique_ids%5B%5D=17",
        help="ココナラのカテゴリ一覧ページ（絞り込み後）のURLをそのまま貼り付けてください。",
    )

    st.divider()
    st.header("取得設定")
    max_count = st.number_input("取得件数", min_value=1, max_value=1000, value=30, step=1)
    st.caption(f"アクセス間隔は {SLEEP_SEC} 秒に固定しています（サーバー負荷軽減のため）")

    st.divider()
    start_clicked = st.button(
        "🚀 分析開始", type="primary", use_container_width=True,
        disabled=st.session_state.running or not category_url_input,
    )
    stop_clicked = st.button("⏹ 停止", use_container_width=True,
                              disabled=not st.session_state.running)

    st.divider()
    st.header("フィルター条件")

    # フィルターの初期値は、これまでに取得済みのデータを元に計算する
    # （まだ何も取得していない場合は広めの既定値を使う）
    _prev_rows = [{k: v for k, v in r.items() if k != "レビュー一覧"} for r in st.session_state.results]
    _prev_df = pd.DataFrame(_prev_rows) if _prev_rows else pd.DataFrame()

    def _num_bounds(col, fallback_lo, fallback_hi):
        if col in _prev_df.columns:
            numeric = pd.to_numeric(_prev_df[col], errors="coerce")
            if not numeric.dropna().empty:
                return int(numeric.min()), int(numeric.max())
        return fallback_lo, fallback_hi

    rank_options = sorted(_prev_df["ランク"].dropna().unique().tolist()) if "ランク" in _prev_df.columns else []
    selected_ranks = st.multiselect("ランク", rank_options, default=rank_options)

    category_query = st.text_input("カテゴリ（部分一致で検索）", "")

    st.caption("価格（円）")
    _lo, _hi = _num_bounds("価格", 0, 100000)
    pc1, pc2 = st.columns(2)
    price_min_input = pc1.number_input("最小", value=_lo, step=100, key="price_min")
    price_max_input = pc2.number_input("最大", value=_hi, step=100, key="price_max")

    st.caption("販売実績（件）")
    _lo, _hi = _num_bounds("販売実績", 0, 10000)
    sc1, sc2 = st.columns(2)
    sales_min_input = sc1.number_input("最小", value=_lo, step=1, key="sales_min")
    sales_max_input = sc2.number_input("最大", value=_hi, step=1, key="sales_max")

    st.caption("総販売実績（件）")
    _lo, _hi = _num_bounds("総販売実績", 0, 100000)
    tc1, tc2 = st.columns(2)
    total_sales_min_input = tc1.number_input("最小", value=_lo, step=1, key="total_sales_min")
    total_sales_max_input = tc2.number_input("最大", value=_hi, step=1, key="total_sales_max")

    st.caption("直近1ヶ月の評価件数")
    _lo, _hi = _num_bounds("直近1ヶ月の評価件数", 0, 1000)
    rc1, rc2 = st.columns(2)
    recent_min_input = rc1.number_input("最小", value=_lo, step=1, key="recent_min")
    recent_max_input = rc2.number_input("最大", value=_hi, step=1, key="recent_max")

# ---- ボタン処理 ----
if start_clicked and not st.session_state.running:
    st.session_state.category_url = category_url_input
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
            SLEEP_SEC,
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
        time.sleep(SLEEP_SEC)
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

    # 列の並び順を「識別→出品者信頼度→比較用の数値→文章・詳細」の順に整える
    preferred_order = [
        "URL", "サービス1枚目画像", "サービス名", "サービス副題",
        "販売者名", "ランク", "総販売実績",
        "価格", "販売実績", "評価総数", "直近1ヶ月の評価件数", "よくある質問数",
        "サービス内容文字数", "スケジュール",
        "サービス内容", "購入にあたってのお願い",
        "カテゴリ階層", "大カテゴリ", "サブカテゴリ",
        "レビュー件数(取得分)",
    ]
    ordered_cols = [c for c in preferred_order if c in df_main.columns]
    remaining_cols = [c for c in df_main.columns if c not in ordered_cols]
    df_main = df_main[ordered_cols + remaining_cols]

    # =================================================================
    # フィルター適用（条件はサイドバーで指定済み）
    # =================================================================

    df_filtered = df_main.copy()

    if selected_ranks:
        df_filtered = df_filtered[df_filtered["ランク"].isin(selected_ranks) | df_filtered["ランク"].isna()]

    if category_query:
        search_cols = [c for c in ["大カテゴリ", "サブカテゴリ", "カテゴリ階層"] if c in df_filtered.columns]
        if search_cols:
            mask = pd.Series(False, index=df_filtered.index)
            for c in search_cols:
                mask = mask | df_filtered[c].astype(str).str.contains(category_query, case=False, na=False)
            df_filtered = df_filtered[mask]

    def apply_range_filter(df, col, lo, hi):
        if col not in df.columns:
            return df
        numeric = pd.to_numeric(df[col], errors="coerce")
        keep = numeric.isna() | numeric.between(lo, hi)
        return df[keep]

    df_filtered = apply_range_filter(df_filtered, "価格", price_min_input, price_max_input)
    df_filtered = apply_range_filter(df_filtered, "販売実績", sales_min_input, sales_max_input)
    df_filtered = apply_range_filter(df_filtered, "総販売実績", total_sales_min_input, total_sales_max_input)
    df_filtered = apply_range_filter(df_filtered, "直近1ヶ月の評価件数", recent_min_input, recent_max_input)

    st.caption(f"フィルター適用後: {len(df_filtered)} / {len(df_main)} 件")

    # =================================================================
    # 表示
    # =================================================================

    st.subheader("📋 サービス一覧")
    column_config = {
        "URL": st.column_config.LinkColumn("URL", display_text="開く", width="small"),
    }
    if "サービス1枚目画像" in df_filtered.columns:
        column_config["サービス1枚目画像"] = st.column_config.ImageColumn("画像", width="small")

    # URLと画像以外の列は、すべて幅を「small」に固定して表を狭くする
    for col in df_filtered.columns:
        if col not in column_config:
            column_config[col] = st.column_config.Column(width="small")

    st.dataframe(df_filtered, use_container_width=True, column_config=column_config)

    def flatten_for_csv(df):
        """
        セル内の改行(\\r\\n・\\n)をスペースに置き換え、
        Excel等で開いたときに1行1レコードになるようにする（ダウンロード用のみ）。
        """
        df = df.copy()
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].apply(
                    lambda v: v.replace("\r\n", " ").replace("\n", " ") if isinstance(v, str) else v
                )
        return df

    st.download_button(
        "⬇️ サービス一覧をCSVでダウンロード（フィルター適用後）",
        flatten_for_csv(df_filtered).to_csv(index=False).encode("utf-8-sig"),
        file_name="coconala_services.csv",
        mime="text/csv",
        use_container_width=True,
    )

    # =================================================================
    # グラフ: 「直近1ヶ月に評価が付いたか」の割合を軸別に見る
    # =================================================================

    def build_ratio_counts(df, group_col, top_n=None):
        """group_col ごとに「直近1ヶ月の評価件数が1件以上／0件」の件数を集計する"""
        tmp = df.dropna(subset=[group_col]).copy()
        if tmp.empty:
            return pd.DataFrame()

        tmp["区分"] = pd.to_numeric(tmp["直近1ヶ月の評価件数"], errors="coerce").fillna(0).apply(
            lambda x: "1件以上" if x >= 1 else "0件"
        )
        counts = tmp.groupby([group_col, "区分"]).size().unstack(fill_value=0)
        for c in ["0件", "1件以上"]:
            if c not in counts.columns:
                counts[c] = 0
        counts = counts[["0件", "1件以上"]]

        if top_n is not None:
            counts = counts.loc[counts.sum(axis=1).sort_values(ascending=False).head(top_n).index]

        return counts

    def render_ratio_chart(counts, group_col, order=None):
        """
        件数のDataFrame(counts)から、100%積み上げ棒グラフをAltairで描画する。
        - x軸の並び順を order で指定可能
        - ツールチップは「項目名 → 区分 → 割合(小数点1桁 + %)」の順で表示
        """
        if counts.empty:
            st.info("表示できるデータがありません。")
            return

        pct = counts.div(counts.sum(axis=1), axis=0) * 100
        if order:
            pct = pct.reindex([c for c in order if c in pct.index])
            counts = counts.reindex([c for c in order if c in counts.index])

        long_df = pct.reset_index().melt(id_vars=group_col, var_name="区分", value_name="割合")
        long_df["割合表示"] = long_df["割合"].map(lambda v: f"{v:.1f}%")

        x_sort = list(pct.index) if order is None else [c for c in order if c in pct.index]

        chart = (
            alt.Chart(long_df)
            .mark_bar()
            .encode(
                x=alt.X(f"{group_col}:N", sort=x_sort, title=group_col),
                y=alt.Y("割合:Q", title="割合（%）", stack="zero"),
                color=alt.Color(
                    "区分:N",
                    scale=alt.Scale(domain=["0件", "1件以上"], range=["#c6dbef", "#08519c"]),
                    legend=alt.Legend(title="区分"),
                ),
                order=alt.Order("区分:N", sort="descending"),
                tooltip=[
                    alt.Tooltip(f"{group_col}:N", title=group_col),
                    alt.Tooltip("区分:N", title="区分"),
                    alt.Tooltip("割合表示:N", title="割合"),
                ],
            )
            .properties(height=350)
        )
        st.altair_chart(chart, use_container_width=True)

        with st.expander(f"件数の内訳を見る（{group_col}別）"):
            st.dataframe(counts, use_container_width=True)

    st.subheader("📊 直近1ヶ月の評価有無（1件以上 / 0件）の割合")

    # ---- 価格帯別 ----
    price_bins = [-1, 499, 999, 1999, 2999, 4999, 6999, 9999, 19999, float("inf")]
    price_labels = [
        "1-499", "500-999", "1000-1999", "2000-2999", "3000-4999",
        "5000-6999", "7000-9999", "10000-19999", "20000以上",
    ]
    df_for_chart = df_filtered.copy()
    df_for_chart["価格帯"] = pd.cut(
        pd.to_numeric(df_for_chart["価格"], errors="coerce"),
        bins=price_bins, labels=price_labels,
    ).astype(str)

    st.markdown("**価格帯別**")
    counts_price = build_ratio_counts(df_for_chart, "価格帯")
    render_ratio_chart(counts_price, "価格帯", order=price_labels)

    # ---- ランク別 ----
    rank_order = ["なし", "レギュラー", "ブロンズ", "シルバー", "ゴールド", "プラチナ"]
    st.markdown("**ランク別**")
    counts_rank = build_ratio_counts(df_filtered, "ランク")
    render_ratio_chart(counts_rank, "ランク", order=rank_order)

    # ---- サブカテゴリ別 ----
    st.markdown("**サブカテゴリ別**（件数の多い上位15件のみ表示）")
    counts_sub = build_ratio_counts(df_filtered, "サブカテゴリ", top_n=15)
    counts_sub_sorted_order = counts_sub.sum(axis=1).sort_values(ascending=False).index.tolist() if not counts_sub.empty else None
    render_ratio_chart(counts_sub, "サブカテゴリ", order=counts_sub_sorted_order)
else:
    st.info("左のサイドバーで条件を設定し、「分析開始」を押してください。")
