import time
import pandas as pd
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

from lxml import html
from datetime import datetime, timezone, timedelta
import requests
import json
import random
from collections import Counter

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbwfA2vP2hdACwsEei73OSQUEojmXFRyyKqu_NcsDFi0Mp7oU2_fTUB1DCM2x4oFMWt7tA/exec"
TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 觸發爬蟲的核心判斷：由 Landing page URL 決定 """
    landing_status = str(row_data.get("Landing page URL", "")).strip()
    if landing_status.lower() in ["", "nan", "crawler failed", "dd cannot be found"]:
        return False
    return True

def is_intermediate_domain(url):
    """ 黑名單：排除廣告跳轉網域 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "chrome-error://", "viglink.com", 
        "sovrn.com", "linkbux.com", "financebuzz.com", "validclick.net", 
        "shophermedia.net", "rover.ebay.com", "clickroll.net"
    ]
    return any(k in url_lower for k in blacklist)

def wait_for_redirect_smart(page, initial_url, retailer_name):
    """ 專對 eBay 優化的極速跳轉邏輯 """
    try:
        is_ebay = "ebay" in retailer_name.lower()
        # 💡 eBay 專用：只給 15 秒，不等 load，只要 commit 就好
        timeout = 15000 if is_ebay else 35000
        
        resp = page.goto(initial_url, wait_until="domcontentloaded", timeout=timeout, referer="https://search.yahoo.com/")
        
        last_url = ""
        # 💡 eBay 只要跳轉一次就檢查，不進入長時間循環
        max_loops = 3 if is_ebay else 6
        
        for _ in range(max_loops):
            curr_url = page.url
            # 💡 eBay 核心斷路器：只要進到 ebay.com 且脫離 rover 追蹤，立刻回傳
            if is_ebay and "ebay.com" in curr_url and "rover.ebay" not in curr_url:
                return resp
            
            if curr_url == last_url and not is_intermediate_domain(curr_url):
                return resp
            
            last_url = curr_url
            page.wait_for_timeout(1500)
        return resp
    except:
        return None

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
        tree = html.fromstring(page.content())
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        
        if not dd_label:
            data["Landing page URL"] = "DD cannot be found"
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data
        
        data["DD Name"] = dd_label[0]

        # Step 2: Landing Page
        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        if landing_raw:
            wait_for_redirect_smart(page, landing_raw[0], retailer)
            if not is_intermediate_domain(page.url):
                data["Landing page URL"] = page.url
                data["Link works"] = "Yes"
                # 標題淨化 (處理 State Farm 重複標題)
                t_raw = page.title().strip().split('|')[0].split('-')[0].strip().lower()
                titles_map["Landing"] = t_raw
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
                    
                    # 💡 標題斷路器：偵測到導回首頁立刻停止後續 Category
                    curr_t = page.title().strip().split('|')[0].split('-')[0].strip().lower()
                    if curr_t == titles_map.get("Landing") and len(curr_t) > 3:
                        data[f"{cat_key} Link works"] = "same"
                        break 
                else:
                    data[f"{cat_key} Link works"] = "Error"
            # 💡 縮減 Category 間的等待
            page.wait_for_timeout(1000)

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data
    except:
        data["Landing page URL"] = "crawler failed"
        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

# --- 4. 主流程 ---

def main():
    print(f"🚀 極速模式啟動 | 本月剩餘額度預估支援中")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except: return

    existing_records = {}
    try:
        cb = random.randint(1000, 9999)
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}&cb={cb}"
        df_existing = pd.read_csv(fresh_url)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records: existing_records[name] = r.to_dict()
    except: pass

    column_order = ["Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", 
                    "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
                    "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
                    "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
                    "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # 縮小 Viewport 減少渲染壓力
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1024, "height": 768}
        )
        page = context.new_page()
        if stealth_sync: stealth_sync(page)

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
                            should_crawl = False
                            continue
                    except: pass

            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            result = run_retailer_capture(page, row, column_order)
            if result:
                requests.post(GAS_OUTPUT_URL, json=result, timeout=20)
            
            # 💡 縮減 Retailer 之間的等待
            time.sleep(random.uniform(2, 4))
            
        browser.close()
    print("🎉 任務結束！")

if __name__ == "__main__":
    main()