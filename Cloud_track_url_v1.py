import time
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone, timedelta
import requests
import json
import random

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbwfA2vP2hdACwsEei73OSQUEojmXFRyyKqu_NcsDFi0Mp7oU2_fTUB1DCM2x4oFMWt7tA/exec"
TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    fields_to_check = [
        "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]
    for field in fields_to_check:
        val = str(row_data.get(field, "")).strip().lower()
        if val in ["", "nan", "undefined", "none", "null", "n/a"]:
            return False
    return True

def is_intermediate_domain(url):
    if not url: return True
    url_lower = url.lower()
    # 擴大黑名單，包含 ebay 的廣告追蹤網域
    blacklist = ["yahoo.com", "search.yahoo.com", "shopping.yahoo.com", "affinity.net", "bizrate.com", "ebay.com/rover", "rover.ebay.com", "peakoptions.site", "clickroll.net"]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    try:
        current_url = page.url
        page_title = page.title().lower()
        # Ebay 快速判斷：只要進到 ebay 網域就給 Yes，不檢查內容，防止被阻擋頁面卡住
        if "ebay.com" in current_url: return "Yes"
        if is_intermediate_domain(current_url): return "Error"
        if not response: return "No"
        not_found_keywords = ["page not found", "404", "dead end", "page cannot be found"]
        visible_text = page.inner_text("body").lower() if page.query_selector("body") else ""
        if any(k in page_title for k in not_found_keywords) or any(k in visible_text for k in not_found_keywords): return "404"
        status = response.status
        if status == 403 or "access denied" in page_title: return "Yes"
        return "Yes" if 200 <= status < 300 else "No"
    except: return "No"

def wait_for_redirect_smart(page, initial_url):
    """ 強化的 Ebay 防卡死跳轉邏輯 """
    try:
        # 對 Ebay 連結使用更短的逾時 (25秒)，並只要伺服器有回應 (commit) 就繼續
        resp = page.goto(initial_url, wait_until="commit", timeout=25000)
        
        for _ in range(6): 
            curr_url = page.url
            # 如果已經跳出 Yahoo 且進入 Ebay，直接提早回傳，不再等待渲染
            if "ebay.com" in curr_url and "rover" not in curr_url:
                return resp
            
            if not is_intermediate_domain(curr_url):
                page.wait_for_timeout(1000)
                return resp
            
            page.wait_for_timeout(2000) 
        return resp
    except:
        return None

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    
    try:
        # 載入 Yahoo SRP，若 30 秒載不進去直接跳下一筆
        page.goto(srp_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2) 
        tree = html.fromstring(page.content())
        
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        if not dd_label: return None
        data["DD Name"] = dd_label[0]

        if data["DD Name"].lower() == "dd cannot be found":
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        cat_links_cache = []
        for i in range(1, 5):
            c_name = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
            c_link = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
            cat_links_cache.append({"name": c_name[0] if c_name else "N/A", "link": c_link[0] if c_link else None})

        # 跳轉 Landing Page
        if landing_raw:
            resp = wait_for_redirect_smart(page, landing_raw[0])
            data["Link works"] = get_link_status(page, resp)
            data["Landing page URL"] = page.url

        # 跳轉 Cat 1-4
        for i, cat in enumerate(cat_links_cache):
            cat_key = f"Cat{i+1}"
            data[f"{cat_key} Name"] = cat["name"]
            if cat["link"]:
                data[f"{cat_key} Link URL"] = cat["link"] 
                c_resp = wait_for_redirect_smart(page, cat["link"])
                data[f"{cat_key} page URL"] = page.url
                data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
            else:
                data[f"{cat_key} Link URL"] = "N/A"

        if not data["Landing page URL"]: return None

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data
    except: return None

# --- 4. 主流程 ---

def main():
    print("🚀 啟動任務：Ebay 防卡死與環境強化版")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except: print("❌ 無法讀取來源名單"); return

    existing_records = {}
    try:
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}"
        df_existing = pd.read_csv(fresh_url)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records:
                existing_records[name] = r.to_dict()
    except: print("⚠️ 無歷史紀錄")

    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # 額外偽裝：設置更加真實的視窗大小
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US", 
            timezone_id="America/New_York",
            viewport={"width": 1920, "height": 1080}
        )
        
        # 破解 Webdriver 偵測的核心 Script
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        now = datetime.now(timezone.utc)

        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer']).strip()
            should_crawl = True
            
            if retailer_name in existing_records:
                old = existing_records[retailer_name]
                old_dd = str(old.get('DD Name', '')).strip().lower()
                old_date_str = str(old.get('Update Date', ''))
                
                is_nf = (old_dd == "dd cannot be found")
                within_7_days = False
                try:
                    clean_date = old_date_str.replace(" UTC", "")
                    old_date = datetime.strptime(clean_date, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                    if now - old_date < timedelta(days=7):
                        within_7_days = True
                except: pass
                
                if is_nf and within_7_days:
                    print(f"⏭️  Skip: {retailer_name}")
                    should_crawl = False
                elif old_dd != "" and within_7_days and is_data_complete(old):
                    print(f"⏭️  Skip: {retailer_name}")
                    should_crawl = False

            if not should_crawl: continue

            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            new_result = run_retailer_capture(page, row, column_order)
            
            if new_result:
                new_dd = str(new_result.get("DD Name")).lower()
                if new_dd == "dd cannot be found" or is_data_complete(new_result):
                    try:
                        requests.post(GAS_OUTPUT_URL, json=new_result, timeout=25)
                        print(f"   ✅ {retailer_name} 執行覆蓋更新")
                    except: print(f"   ❌ {retailer_name} 傳送失敗")
                else:
                    print(f"   ⚠️ {retailer_name} 抓取不全，保留舊資料")
            else:
                print(f"   ⚠️ {retailer_name} 抓取超時或失敗，跳過。")
            
            # 增加隨機休眠
            time.sleep(random.uniform(3, 6))

        browser.close()
    print("🎉 任務結束！")

if __name__ == "__main__":
    main()