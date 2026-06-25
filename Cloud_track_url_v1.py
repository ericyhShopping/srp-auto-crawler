import os
import time
import pandas as pd
from playwright.sync_api import sync_playwright
# 容錯匯入 stealth：相容新版(2.x 的 Stealth)，套件缺失/版本不符時優雅略過，
# 絕不讓反偵測套件的問題拖垮整個爬蟲啟動。
try:
    from playwright_stealth import Stealth
    _STEALTH = Stealth()
except Exception:
    _STEALTH = None
from lxml import html
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import requests
import json
import random
from collections import Counter

# --- 1. 設定區域 ---
# 優先讀 GitHub Secrets 注入的環境變數；本機/未設定時退回寫死的預設值。
# 如此 workflow 裡映射的 Secrets 才會真正生效，網址也不必硬編在程式碼裡。
_DEFAULT_GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
_DEFAULT_GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyHzJ9JEC-8AuEJmcpZpjXdpfVrKcMmO3pvstNvZQyv-_c0jlmoqBHt4jnW3IwrDiK0Hg/exec"
_DEFAULT_TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"

GSHEET_INPUT_URL = os.environ.get("GSHEET_INPUT_URL") or _DEFAULT_GSHEET_INPUT_URL
GAS_OUTPUT_URL = os.environ.get("GAS_OUTPUT_URL") or _DEFAULT_GAS_OUTPUT_URL
TRACK_URL_DATA_CSV = os.environ.get("TRACK_URL_DATA_CSV") or _DEFAULT_TRACK_URL_DATA_CSV

# 中繼/失敗的「真實網域」黑名單：只比對 hostname，避免落地頁追蹤參數
# (force_referer=search.yahoo.com、pname-Yahoo 等) 把真正落地頁誤判成中繼。
INTERMEDIATE_DOMAIN_BLACKLIST = [
    "yahoo.com", "viglink.com", "sovrn.com", "linkbux.com",
    "financebuzz.com", "validclick.net", "shophermedia.net",
    "clickroll.net", "rover.ebay.com"
]
# 錯誤/非正常頁面標記：比對整條 URL（scheme 或錯誤字串）
INTERMEDIATE_URL_MARKERS = ["chrome-error://", "chromewebdata", "access-denied"]

DD_WAIT_TIMEOUT = 8000          # 等 DD 元素出現的毫秒上限
DD_DETECT_ATTEMPTS = 2          # DD 抓不到時的重載重試總次數
SRP_SETTLE_RANGE = (2.5, 5.0)   # 進 SRP 後等待頁面穩定的隨機秒數
LOAD_SETTLE_TIMEOUT = 8000      # 讀 title 前等待導航穩定的毫秒上限
GAS_POST_RETRIES = 3            # 發射回 GAS 的重試次數
LANDING_RETRY = 1               # 落地跳轉停在中繼網域時的重試次數（設 0 可關閉，只對失敗筆加時間）
LANDING_RETRY_PAUSE = 3         # 落地重試前的暫停秒數
RETAILER_GAP_RANGE = (5, 12)    # 廠商之間的隨機間隔秒數（限流綁 IP 與間隔無關，故用較短值省時間）
WARMUP_URL = "https://www.yahoo.com/"  # 開跑前暖機訪問，取得 cookie/consent 讓 session 更像真人

# 模擬美國使用者的瀏覽器地區指紋（IP 仍以實際執行環境為準，這層只統一語言/時區）
US_LOCALE = "en-US"
US_TIMEZONE = "America/New_York"
US_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 觸發爬蟲的核心判斷：由 Landing page URL 決定 """
    landing_status = str(row_data.get("Landing page URL", "")).strip()
    if landing_status.lower() in ["", "nan", "crawler failed", "dd cannot be found"]:
        return False
    return True

def is_intermediate_domain(url):
    """ 黑名單：排除廣告跳轉網域（只比對 hostname，避免落地頁追蹤參數誤殺） """
    if not url: return True
    url_lower = str(url).lower()
    if any(m in url_lower for m in INTERMEDIATE_URL_MARKERS):
        return True
    host = urlparse(url_lower).hostname or ""
    return any(host == d or host.endswith("." + d) for d in INTERMEDIATE_DOMAIN_BLACKLIST)

def _safe_title(page):
    """ 先等導航穩定再抓 title 並包 try，避免頁面跳轉中呼叫 title() 觸發
        'Execution context was destroyed' 而讓整筆失敗。 """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=LOAD_SETTLE_TIMEOUT)
    except Exception:
        pass
    try:
        return page.title().strip().split('|')[0].split('-')[0].strip().lower()
    except Exception:
        return ""

def wait_for_redirect_smart(page, initial_url, retailer_name):
    """ 專對 eBay 優化的極速跳轉邏輯 """
    is_ebay = "ebay" in retailer_name.lower()
    timeout = 15000 if is_ebay else 35000
    resp = None
    try:
        # goto 逾時也不放棄：跳轉鏈長的站即使沒在時限內完成，
        # 當下 page.url 通常已抵達落地頁，後續靠輪詢確認穩定即可。
        resp = page.goto(initial_url, wait_until="domcontentloaded", timeout=timeout, referer="https://search.yahoo.com/")
    except Exception:
        resp = None

    last_url = ""
    # 💡 eBay 只要跳轉一次就檢查，不進入長時間循環
    max_loops = 3 if is_ebay else 6
    for _ in range(max_loops):
        try:
            curr_url = page.url
        except Exception:
            break
        # 💡 eBay 核心斷路器：只要進到 ebay.com 且脫離 rover 追蹤，立刻回傳
        if is_ebay and "ebay.com" in curr_url and "rover.ebay" not in curr_url:
            return resp
        if curr_url == last_url and not is_intermediate_domain(curr_url):
            return resp
        last_url = curr_url
        page.wait_for_timeout(1500)
    return resp

def _url_key(url):
    """ 取 hostname + 路徑(去尾斜線、忽略 query) 當作頁面識別。
        用來判斷分類是否導到與落地相同的頁面——比標題可靠（標題常被品牌名/
        反爬頁如 'Bot or Not?' 汙染，使不同分類頁看起來相同）。 """
    try:
        u = urlparse(str(url).lower())
        return (u.hostname or "") + (u.path or "").rstrip("/")
    except Exception:
        return str(url).lower()

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    titles_map = {}

    try:
        # Step 1: Yahoo SRP
        page.goto(srp_url, wait_until="domcontentloaded", timeout=30000)

        # 🔁 等 DD 模組真的出現再抓；抓不到就重載重試，避免「零等待直接抓」抓到
        #    尚未載入完成的頁面而誤報 DD cannot be found。
        tree, dd_label = None, []
        for attempt in range(DD_DETECT_ATTEMPTS):
            try:
                page.wait_for_selector("div[class*='TopNavCommerce'] h3 a[aria-label]", timeout=DD_WAIT_TIMEOUT)
            except Exception:
                pass
            time.sleep(random.uniform(*SRP_SETTLE_RANGE))
            tree = html.fromstring(page.content())
            dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
            if dd_label:
                break
            if attempt < DD_DETECT_ATTEMPTS - 1:
                page.reload(wait_until="domcontentloaded", timeout=30000)

        if not dd_label:
            data["Landing page URL"] = "DD cannot be found"
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        data["DD Name"] = dd_label[0]

        # Step 2: Landing Page
        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        if landing_raw:
            # 落地停在中繼網域時重試跳轉：限流多為瞬時，單獨重跑常能成功。
            # 只重試「落地」這一跳，且只在失敗時才花時間，成功的廠商完全不受影響。
            for attempt in range(LANDING_RETRY + 1):
                wait_for_redirect_smart(page, landing_raw[0], retailer)
                if not is_intermediate_domain(page.url):
                    break
                if attempt < LANDING_RETRY:
                    print(f"   ↻ {retailer} 落地停在中繼網域，{LANDING_RETRY_PAUSE}s 後重試跳轉...")
                    time.sleep(LANDING_RETRY_PAUSE)

            if not is_intermediate_domain(page.url):
                data["Landing page URL"] = page.url
                data["Link works"] = "Yes"
                # 記錄落地頁的網址路徑，供分類斷路器比對（取代不可靠的標題比對）
                titles_map["Landing"] = _url_key(page.url)
            else:
                data["Landing page URL"] = "crawler failed"

        # 💡 額度斷路器：Landing 失敗則不跑 Categories，現省 4 次跳轉時間
        if data["Landing page URL"] == "crawler failed":
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        # Step 3: Categories
        for i in range(1, 5):
            c_link = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
            c_name = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
            cat_key = f"Cat{i}"
            data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
            
            if c_link:
                data[f"{cat_key} Link URL"] = c_link[0]
                wait_for_redirect_smart(page, c_link[0], retailer)
                
                if not is_intermediate_domain(page.url):
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = "Yes"
                    
                    # 💡 路徑斷路器：只有當分類「網址路徑」與落地相同(真的導回同一頁)才停止；
                    #    各分類有自己的目標頁(/Hotels、/Flights…)時路徑不同，會正常繼續往下抓。
                    curr_key = _url_key(page.url)
                    if curr_key and curr_key == titles_map.get("Landing"):
                        data[f"{cat_key} Link works"] = "same"
                        break
                else:
                    data[f"{cat_key} Link works"] = "Error"
            # 💡 縮減 Category 間的等待
            page.wait_for_timeout(1000)

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data
    except Exception as e:
        print(f"   ❌ 擷取異常: {e}")
        data["Landing page URL"] = "crawler failed"
        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

def post_to_gas(result):
    """ 發射回 GAS，加重試 + backoff，避免一次網路抖動就讓整個任務崩潰中止。 """
    for attempt in range(1, GAS_POST_RETRIES + 1):
        try:
            requests.post(GAS_OUTPUT_URL, json=result, timeout=20)
            return True
        except Exception as e:
            print(f"   ⚠️ GAS 發射失敗 ({attempt}/{GAS_POST_RETRIES}): {e}")
            if attempt < GAS_POST_RETRIES:
                time.sleep(2 * attempt)
    return False

# --- 4. 主流程 ---

def _print_geo():
    # 印出對外 IP 與國家，方便在 GitHub Actions log 確認是否從美國 IP 執行
    try:
        j = requests.get("https://ipinfo.io/json", timeout=10).json()
        print(f"🌐 對外 IP: {j.get('ip')} | 國家: {j.get('country')} | 地區: {j.get('region')}")
        if j.get("country") != "US":
            print("⚠️ 注意：目前非美國 IP，結果可能與美國使用者不同！")
    except Exception as e:
        print(f"🌐 IP 國家檢查略過: {e}")

def main():
    print(f"🚀 極速模式啟動 | 本月剩餘額度預估支援中")
    _print_geo()
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except Exception as e:
        print(f"❌ 讀取來源清單失敗: {e}")
        return

    existing_records = {}
    try:
        cb = random.randint(1000, 9999)
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}&cb={cb}"
        df_existing = pd.read_csv(fresh_url)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records: existing_records[name] = r.to_dict()
    except Exception:
        pass

    column_order = ["Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", 
                    "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
                    "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
                    "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
                    "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"]

    with sync_playwright() as p:
        # CI(GitHub Actions / ubuntu-latest)只裝了 chromium，不可加 channel="chrome"
        browser = p.chromium.launch(headless=True)
        try:
            # 縮小 Viewport 減少渲染壓力
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1024, "height": 768},
                locale=US_LOCALE,
                timezone_id=US_TIMEZONE,
                extra_http_headers={"Accept-Language": US_ACCEPT_LANGUAGE}
            )
            # 套用 stealth（新版 2.x API）；容錯：失敗不影響爬蟲
            if _STEALTH:
                try:
                    _STEALTH.apply_stealth_sync(context)
                except Exception:
                    pass
            page = context.new_page()

            # 🔥 暖機：開跑前先訪問 Yahoo 首頁，取得 cookie/consent，讓後續請求更像正常使用者
            try:
                page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(random.randint(2000, 4000))
                print("🔥 暖機完成，開始爬取")
            except Exception:
                pass

            for index, row in df_input.iterrows():
                retailer_name = str(row['Retailer']).strip()
                srp_url = str(row.get('SRP', ''))

                if retailer_name in existing_records:
                    old = existing_records[retailer_name]
                    if is_data_complete(old):
                        try:
                            old_date = datetime.strptime(str(old.get('Update Date', '')).replace(" UTC", ""), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                            if (datetime.now(timezone.utc) - old_date < timedelta(days=7)):
                                print(f"⏭️  Skip: {retailer_name}")
                                continue
                        except Exception:
                            pass

                print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
                result = run_retailer_capture(page, row, column_order)
                if result:
                    # 與 _CL 一致：帶 mode 讓 GAS 走「依 Retailer 就地覆蓋」分支
                    # （GAS 的無 mode else 分支是空的，不帶 mode 會寫不進去）
                    result["mode"] = "update_by_retailer"
                    print(f"   → 結果: {result.get('Landing page URL','')}")
                    post_to_gas(result)

                # 拉長 Retailer 之間的隨機間隔，大幅降低被限流的機率
                time.sleep(random.uniform(*RETAILER_GAP_RANGE))
        finally:
            browser.close()
    print("🎉 任務結束！")

if __name__ == "__main__":
    main()