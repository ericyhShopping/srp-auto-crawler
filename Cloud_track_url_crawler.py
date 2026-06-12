import os
import time
import pandas as pd
import requests
import random
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone

# =====================================================================
# 🛠️ 核心設定區：已完美保留妳的真實 Google 試算表與 GAS 網址
# =====================================================================
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=2006486009&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyHzJ9JEC-8AuEJmcpZpjXdpfVrKcMmO3pvstNvZQyv-_c0jlmoqBHt4jnW3IwrDiK0Hg/exec"
TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"
# =====================================================================

def is_intermediate_domain(url):
    if not url: 
        return True
    url_lower = str(url).lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com", 
        "chrome-error://", "chromewebdata", "access-denied", 
        "viglink.com", "sovrn.com", "linkbux.com", "financebuzz.com", 
        "validclick.net", "shophermedia.net", "rd.bizrate.com"
    ]
    return any(k in url_lower for k in blacklist)

def wait_for_redirect_smart(page, initial_url, retailer_name):
    try:
        is_slow = any(k in retailer_name.lower() for k in ["ebay", "booking", "statefarm", "kohls"])
        resp = page.goto(
            initial_url, 
            wait_until="domcontentloaded", 
            timeout=15000 if is_slow else 30000, 
            referer="https://search.yahoo.com/"
        )
        last_url = ""
        for _ in range(5):
            curr_url = page.url
            if is_slow and "ebay.com" in curr_url and "rover.ebay" not in curr_url: 
                return resp
            if curr_url == last_url and not is_intermediate_domain(curr_url): 
                return resp
            last_url = curr_url
            page.wait_for_timeout(1500)
        return resp
    except: 
        return None

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get("Retailer", "N/A"))
    srp_url = str(row.get("SRP", ""))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    titles_map = {}
    
    try:
        page.goto(srp_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        tree = html.fromstring(page.content())
        
        dd_label = tree.xpath("//div[contains(@class,\"TopNavCommerce\")]//h3/a/@aria-label")
        if not dd_label:
            data["Landing page URL"] = "DD cannot be found"
            data["Update Date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            return data
            
        data["DD Name"] = dd_label[0]
        landing_raw = tree.xpath("//div[contains(@class,\"compTitle\")]/h3/a/@href")
        
        if landing_raw:
            wait_for_redirect_smart(page, landing_raw[0], retailer)
            if not is_intermediate_domain(page.url):
                data["Landing page URL"] = page.url
                data["Link works"] = "Yes" if len(page.url) > 10 else "No"
                titles_map["Landing"] = page.title().strip().split("|")[0].split("-")[0].strip().lower()
            else:
                data["Landing page URL"] = "crawler failed"
                
        if data["Landing page URL"] == "crawler failed":
            data["Update Date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            return data
            
        has_cat_failed = False
        for i in range(1, 5):
            c_link = tree.xpath(f"//div[contains(@class,\"TopNavCommerce\")]/div[3]//li[{i}]//a/@href")
            c_name = tree.xpath(f"//div[contains(@class,\"TopNavCommerce\")]/div[3]//li[{i}]//a//img/@alt")
            cat_key = f"Cat{i}"
            
            data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A" 
            
            if c_link:
                data[f"{cat_key} Link URL"] = c_link[0]
                wait_for_redirect_smart(page, c_link[0], retailer)
                if not is_intermediate_domain(page.url):
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = "Yes"
                    curr_t = page.title().strip().split("|")[0].split("-")[0].strip().lower()
                    if curr_t == titles_map.get("Landing") and len(curr_t) > 3:
                        data[f"{cat_key} Link works"] = "same"
                        has_cat_failed = True
                        break
                else:
                    data[f"{cat_key} Link works"] = "Error"
                    has_cat_failed = True
            else:
                if data[f"{cat_key} Name"] != "N/A":
                    has_cat_failed = True
                    
            if c_link and (not data[f"{cat_key} page URL"] or data[f"{cat_key} page URL"] in ["", "nan"]):
                has_cat_failed = True
            time.sleep(1)
            
        if has_cat_failed:
            data["Landing page URL"] = "crawler failed"
            
        data["Update Date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return data
    except Exception as e:
        print(f"❌ 擷取期間發生異常: {e}")
        data["Landing page URL"] = "crawler failed"
        data["Update Date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return data

def main_process(wait_minutes):
    if GSHEET_INPUT_URL.startswith("http") is False:
        print("⚠️ 提示：請先在程式碼最上方填入妳的真實試算表與 GAS 網址！", flush=True)
        return

    # 🚀 100% 聽從來源清單！直接下載妳指定的 urls_crawler 清單工作表
    try:
        cb = random.randint(1000, 9999)
        fresh_source_url = f"{GSHEET_INPUT_URL}&t={int(time.time())}&cb={cb}"
        df_source = pd.read_csv(fresh_source_url, storage_options={"timeout": 15})
    except Exception as e:
        print(f"❌ 讀取來源表格 (urls_crawler) 失敗: {e}")
        return

    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", 
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works", 
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works", 
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works", 
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]

    # 💡 核心優化：直接將來源清單的每一筆，透過 .iloc[::-1] 由下往上全數排入排隊任務，完全不和別的表格比對！
    task_queue = []
    for _, source_row in df_source.iloc[::-1].iterrows():
        r_name = str(source_row.get("Retailer", "")).strip()
        if not r_name or r_name.lower() == "nan":
            continue
        task_queue.append((r_name, source_row))

    if not task_queue:
        print("⚠️ urls_crawler 清單內沒有任何有效的廠商資料。")
        return

    print(f"📋 任務排定！直接依據 urls_crawler 的排序（由下往上），將強行執行全部【{len(task_queue)}】筆任務。")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", 
            viewport={"width": 1280, "height": 720}
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        # 🚀 完全遵循 urls_crawler 的最後一筆往前強行推進
        for index, (target_retailer, target_row_data) in enumerate(task_queue, start=1):
            print(f"\n⚡ [{index}/{len(task_queue)}] 正在強行處理來源名單: 【{target_retailer}】...")
            
            result = run_retailer_capture(page, target_row_data, column_order)
            
            if result:
                result["mode"] = "update_by_retailer"
                try:
                    resp = requests.post(GAS_OUTPUT_URL, json=result, timeout=20)
                    print(f"✅ 處理完畢！【{target_retailer}】-> 網址已發射: {result['Landing page URL']}")
                    print(f"💬 GAS 回傳結果: {resp.text}")
                except Exception as e:
                    print(f"❌ 發射資料回 GAS 失敗: {e}")

            # ⏳ 手動等待時間控制
            if index < len(task_queue):
                if wait_minutes > 0:
                    print(f"😴 依手動設定，等待 {wait_minutes} 分鐘後再執行下一筆任務...")
                    time.sleep(wait_minutes * 60)
                else:
                    print("⚡ 設定為 0，不進行任何等待，直接全速衝向下一筆！")
                    time.sleep(1)

        browser.close()

if __name__ == "__main__":
    print("==============================================")
    print("🚀 Mac 本機強行救援監聽器（純來源清單版）已開工！")
    print("==============================================")
    
    try:
        user_input = input("💬 請輸入每筆資料處理完後的等待時間 (分鐘，輸入 0 代表直接衝下一筆): ")
        wait_minutes = float(user_input)
    except ValueError:
        print("⚠️ 輸入不正確，預設調整為不等待 (0 分鐘)。")
        wait_minutes = 0.0

    while True:
        main_process(wait_minutes)
        print("\n💤 清單內所有任務已悉數強制執行完畢。進入 10 分鐘冷卻狀態，稍後重新下載清單...")
        time.sleep(600)