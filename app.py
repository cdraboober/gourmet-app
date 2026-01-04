import streamlit as st
import requests
import pandas as pd
import googlemaps
import os
import folium
import random
import datetime
import re
import time
import google.generativeai as genai
from streamlit_folium import st_folium
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

# .envファイルを読み込む
load_dotenv()

# 環境変数
HOTPEPPER_API_KEY = os.getenv("HOTPEPPER_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

st.set_page_config(page_title="AI飲食店予約アシスタント", layout="wide")

# Gemini設定
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# --- 定数 ---
PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"
]
WEEKDAYS_JP = ["月", "火", "水", "木", "金", "土", "日"]

# --- 関数 ---

def fetch_hotpepper_single(api_key, keyword, budget_code, count, start):
    """単一の予算コードでホットペッパーAPIを叩く関数"""
    url = "http://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
    params = {
        "key": api_key, "keyword": keyword, "count": count, "format": "json",
        "internet": 1, "start": start
    }
    # 予算コードがある場合のみ追加
    if budget_code:
        params["budget"] = budget_code
        
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if data and "results" in data and "shop" in data["results"]:
            return data["results"]["shop"]
        return []
    except:
        return []

def get_hotpepper_data_multi_budget(api_key, keyword, budget_codes, count, start=1):
    """
    複数の予算コードに対応するため、並列でAPIを叩いて結果をマージする関数
    """
    # 予算指定がない場合は、budget=Noneで1回だけ叩く
    target_budgets = budget_codes if budget_codes else [None]
    
    all_shops = []
    
    # 予算コードごとに並列リクエスト
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(fetch_hotpepper_single, api_key, keyword, b_code, count, start)
            for b_code in target_budgets
        ]
        
        for future in as_completed(futures):
            shops = future.result()
            all_shops.extend(shops)
    
    # 重複排除 (店舗IDでユニークにする)
    unique_shops = {}
    for shop in all_shops:
        if shop["id"] not in unique_shops:
            unique_shops[shop["id"]] = shop
            
    return list(unique_shops.values())

def fetch_google_data(gmaps_client, shop):
    name = shop.get("name", "")
    address = shop.get("address", "")
    query = f"{name} {address}"
    try:
        place_result = gmaps_client.places(query=query)
        if place_result['results']:
            result = place_result['results'][0]
            shop["google_rating"] = result.get('rating', 0.0)
            shop["review_count"] = result.get('user_ratings_total', 0)
        else:
            shop["google_rating"] = 0.0
            shop["review_count"] = 0
    except:
        shop["google_rating"] = 0.0
        shop["review_count"] = 0
    return shop

def check_open_logic(shop, target_date, target_time, use_ai=False):
    shop_name = shop["name"]
    open_text = shop.get("open", "")
    close_text = shop.get("close", "")
    
    target_wday_idx = target_date.weekday()
    target_wday_str = WEEKDAYS_JP[target_wday_idx]
    
    # ルールベース判定
    if not close_text or "無休" in close_text:
        pass 
    else:
        clean_close_text = close_text.replace("祝日", "").replace("祝前日", "")
        if f"{target_wday_str}曜" in clean_close_text:
            return False, f"定休日 ({close_text})"
        tokens = re.split(r'[、,，\s/]+', clean_close_text)
        if target_wday_str in tokens:
            return False, f"定休日 ({close_text})"

    # AI判定
    if use_ai and GEMINI_API_KEY:
        target_str = f"{target_date.strftime('%Y/%m/%d')} ({target_wday_str}) {target_time.strftime('%H:%M')}"
        prompt = f"""
        店舗情報に基づき、指定日時が「営業中」か「休み」か判定してください。
        店舗: {shop_name}
        営業時間: {open_text}
        定休日: {close_text}
        希望日時: {target_str}
        回答は 'TRUE' (営業中) または 'FALSE' (休み) の文字列のみ。
        """
        try:
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            if "FALSE" in response.text.strip().upper():
                return False, "AI判定: 休み"
        except:
            pass 

    return True, "OK"

def create_numbered_icon(number, rating):
    if rating >= 4.0: color = "#2980b9"
    elif rating >= 3.0: color = "#27ae60"
    else: color = "#7f8c8d"
    return folium.DivIcon(
        icon_size=(30, 30), icon_anchor=(15, 30),
        html=f"""<div style="background-color: {color}; color: white; border-radius: 50%; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-family: Arial; border: 2px solid white; box-shadow: 2px 2px 5px rgba(0,0,0,0.3);">{number}</div>"""
    )

# --- サイドバー ---
st.sidebar.title("📅 AI予約アシスタント")
api_ok = True
if not HOTPEPPER_API_KEY or not GOOGLE_API_KEY:
    st.sidebar.error("⚠️ APIキー不足")
    api_ok = False

st.sidebar.markdown("---")
# 日時設定
target_date = st.sidebar.date_input("予約日", datetime.date.today() + datetime.timedelta(days=1))
target_time = st.sidebar.time_input("予約時間", datetime.time(19, 0))
target_wday_str = WEEKDAYS_JP[target_date.weekday()]
st.sidebar.info(f"設定: **{target_date.month}/{target_date.day} ({target_wday_str}) {target_time.strftime('%H:%M')}**")

use_ai = False
if GEMINI_API_KEY:
    use_ai = st.sidebar.checkbox("🤖 AI判定も併用する", value=True)

st.sidebar.markdown("---")
selected_pref = st.sidebar.selectbox("都道府県", PREFECTURES, index=12)
area_keyword = st.sidebar.text_input("エリア詳細", "大手町")
selected_genre = st.sidebar.selectbox("ジャンル", ["指定なし", "居酒屋", "焼肉", "焼き鳥", "イタリアン", "フレンチ", "寿司", "和食", "中華", "ラーメン", "カフェ", "韓国料理"], index=0)
people_count = st.sidebar.number_input("人数", 1, value=4)

# 【変更点1】予算を複数選択に変更
budget_options = {
    "〜2000円": "B001", "2001〜3000円": "B002", "3001〜4000円": "B003",
    "4001〜5000円": "B008", "5001〜7000円": "B004", "7001〜10000円": "B005", "10001円〜": "B006"
}
# デフォルトで "3001〜4000円" と "4001〜5000円" を選択状態にするなど
selected_budget_labels = st.sidebar.multiselect("予算 (複数選択可)", list(budget_options.keys()))
# 選択されたラベルからコードのリストを作成
selected_budget_codes = [budget_options[label] for label in selected_budget_labels]

st.sidebar.markdown("---")
use_random = st.sidebar.checkbox("開始位置ランダム", value=True)

if "search_params" not in st.session_state:
    st.session_state["search_params"] = {"start": 1}
if "shops_data" not in st.session_state: st.session_state["shops_data"] = None

col1, col2 = st.sidebar.columns(2)
if col1.button("検索", type="primary"):
    start_idx = random.randint(1, 50) if use_random else 1
    st.session_state["search_params"] = {"start": start_idx}
    st.session_state["trigger_search"] = True

if col2.button("次のリスト"):
    st.session_state["search_params"]["start"] += 20
    st.session_state["trigger_search"] = True

# --- 検索ロジック ---
if st.session_state.get("trigger_search", False) and api_ok:
    genre_str = selected_genre if selected_genre != "指定なし" else ""
    query_str = f"{selected_pref} {area_keyword} {genre_str}".strip()
    
    try:
        gmaps = googlemaps.Client(key=GOOGLE_API_KEY)
        
        with st.status("お店を探しています...", expanded=True) as status:
            valid_shops = []
            current_start = st.session_state["search_params"]["start"]
            
            # ループ処理 (20件集まるまで)
            max_loops = 5 
            for loop in range(max_loops):
                if len(valid_shops) >= 20:
                    break 

                status.write(f"🔍 ホットペッパー検索中... (確保数: {len(valid_shops)}/20) - ページ{loop+1}")
                
                # 【変更点】複数予算対応の関数を呼び出し
                # party_capacity のフィルタはAPI側でできない（キャパ指定パラメータはあるが、厳密ではないため）
                # ここでは APIの party_capacity パラメータを使って「この人数以上」でフィルタする
                
                # APIのparty_capacityは数値指定
                raw_shops = get_hotpepper_data_multi_budget(
                    HOTPEPPER_API_KEY, query_str, selected_budget_codes, 
                    count=20, start=current_start
                )
                
                if not raw_shops:
                    break 

                # キャパシティのクライアントサイドフィルタ & 営業チェック
                with ThreadPoolExecutor(max_workers=10) as executor:
                    # 判定関数
                    def check_shop(shop):
                        # 人数キャパチェック (APIで漏れる場合があるため念のため)
                        cap = shop.get("party_capacity", 0)
                        try:
                            cap = int(cap)
                        except:
                            cap = 0
                        if cap < people_count:
                            return None, None # キャパ不足
                        
                        return check_open_logic(shop, target_date, target_time, use_ai)

                    future_to_shop = {executor.submit(check_shop, shop): shop for shop in raw_shops}
                    
                    for future in as_completed(future_to_shop):
                        shop = future_to_shop[future]
                        res = future.result()
                        if res[0] is None: continue # キャパ不足など
                        
                        is_open, reason = res
                        if is_open:
                            valid_shops.append(shop)
                
                current_start += 20
                st.session_state["search_params"]["start"] = current_start 
            
            valid_shops = valid_shops[:20]
            
            if valid_shops:
                status.write(f"✅ {len(valid_shops)}件確保。Google評価取得中...")
                enriched_shops = []
                with ThreadPoolExecutor(max_workers=10) as executor:
                    future_to_shop = {executor.submit(fetch_google_data, gmaps, shop): shop for shop in valid_shops}
                    for future in as_completed(future_to_shop):
                        enriched_shops.append(future.result())
                
                sorted_shops = sorted(enriched_shops, key=lambda x: x["google_rating"], reverse=True)
                st.session_state["shops_data"] = sorted_shops
                status.update(label="完了！", state="complete", expanded=False)
            else:
                st.error("条件に合う営業中のお店が見つかりませんでした。")
                st.session_state["shops_data"] = None

    except Exception as e:
        st.error(f"システムエラー: {e}")
    st.session_state["trigger_search"] = False

# --- 表示 ---
st.title("🍽️ AI飲食店予約アシスタント")

st.markdown(f"""
<div style="background-color:#e8f4f8; padding:15px; border-radius:10px; border:1px solid #bce0fd; margin-bottom:20px;">
    📅 <b>希望日時: {target_date.month}月{target_date.day}日 ({target_wday_str}) {target_time.strftime('%H:%M')}</b> で検索中
</div>
""", unsafe_allow_html=True)

if st.session_state["shops_data"]:
    shops = st.session_state["shops_data"]
    
    if shops:
        m = folium.Map(location=[shops[0]["lat"], shops[0]["lng"]], zoom_start=15, tiles="CartoDB positron")
        for i, shop in enumerate(shops):
            n, lat, lon = i+1, shop["lat"], shop["lng"]
            folium.Marker([lat, lon], icon=create_numbered_icon(n, shop["google_rating"]), 
                          popup=shop["name"], tooltip=f"{n}. {shop['name']}").add_to(m)
        st_folium(m, width="100%", height=500)

    st.divider()
    st.markdown(f"### 📋 お店リスト ({len(shops)}件)")
    
    for i, shop in enumerate(shops):
        n = i + 1
        name = shop["name"]
        rating = shop["google_rating"]
        open_txt = shop.get("open", "情報なし")
        close_txt = shop.get("close", "情報なし")
        
        # 【変更点2】予算とキャパ情報の取得
        budget_name = shop.get("budget", {}).get("name", "情報なし")
        budget_avg = shop.get("budget", {}).get("average", "")
        capacity = shop.get("party_capacity", "不明")

        with st.container():
            c1, c2 = st.columns([1, 2])
            img = shop.get("photo", {}).get("pc", {}).get("l", "")
            if img: c1.image(img, use_column_width=True)
            
            with c2:
                bg = "#2980b9" if rating>=4 else "#27ae60" if rating>=3 else "#7f8c8d"
                st.markdown(f"""### <span style='background-color:{bg}; color:white; border-radius:50%; padding:4px 11px; font-size:0.8em;'>{n}</span> {name}""", unsafe_allow_html=True)
                st.markdown(f"<span style='color:#f39c12; font-size:18px;'>★{rating}</span> <span style='color:gray;'>({shop['review_count']}件)</span>", unsafe_allow_html=True)
                
                # 【変更点2】予算とキャパを見やすく表示
                st.write(f"📍 {shop.get('address','')}")
                st.write(f"💰 **予算**: {budget_name} {f'({budget_avg})' if budget_avg else ''}")
                st.write(f"🥂 **最大キャパ**: {capacity}名")
                
                # 曜日ハイライト
                if target_wday_str in close_txt:
                     close_display = f"<span style='color:red; font-weight:bold;'>⚠️ {close_txt}</span>"
                else:
                     close_display = close_txt

                st.markdown(f"""
                <div style="font-size:0.9em; color:#333; background-color:#f9f9f9; padding:10px; border-radius:5px; margin-top:5px;">
                    🕒 <b>営業時間</b>: {open_txt}<br>
                    🛑 <b>定休日</b>: {close_display}
                </div>
                """, unsafe_allow_html=True)
                st.link_button(f"👉 予約へ進む", shop["urls"]["pc"])
            st.divider()