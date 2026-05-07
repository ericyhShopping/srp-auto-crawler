import time
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import requests
import json
import os

# --- 1. 配置設定 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyHzJ9JEC-8AuEJmcpZpjXdpfVrKcMmO3pvstNvZQyv-_c0jlmoqBHt4jnW3IwrDiK0Hg/exec"

# --- 2. 工具函式 ---

def check_status_text(page):
    """ 檢查是否為 404，否則回傳 '-' """
    try:
        # 等待一點時間確保頁面內容加載
        page.wait_for_timeout(2000)
        
        # 排除瀏覽器內部錯誤網址
        if "chromewebdata" in page.url or "chrome-error" in page.url:
            return "Load Error"

        content = page.content().lower()
        title = page.title().lower()
        
        error_keywords = [
            "page not found", "404", "dead end", 
            "page cannot be found", "403", "access denied"
        ]
        
        if any(kw in title for kw in error_keywords) or any(kw in content for kw in error_keywords):
            return "404"
        return "-" # 正常則顯示 -
    except:
        return "Error"

def is_intermediate_domain(url):
    """ 判定是否為中間跳轉、黑名單或搜尋引擎頁面 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "us.search.yahoo.com", "yahoo.com/rdlw", "r.search.yahoo.com",
        "affinity.net", "bizrate.com", "shophermedia.net", "provenpixel.com",
        "socialiqredir.com", "discounthero.org", "magik.ly", "netsourceio.com",
        "clickroll.net", "shopping123.com", "top-best.com", "v2i8b.com", 
        "beyondcheap.com", "intentxredir.com", "peakoptions.site",
        "search.yahoo", "google.com/search"
    ]
    return any(k in url_lower for k in blacklist)

def wait_for_redirect_smart(page, initial_url):
    """ 智能等待跳轉直到脫離 Yahoo 或中間頁面 """
    try:
        # 增加初始等待時間
        page.goto(initial_url, wait_until="domcontentloaded", timeout=60000)
        
        # 最多循環檢查 15 次 (約 45-60 秒)，直到跳出黑名單
        for _ in range(15):
            current_url = page.url
            if not is_intermediate_domain(current_url) and "chromewebdata" not in current_url:
                # 脫離黑名單後，再多等一下確保官網加載完成
                page.wait_for_load_state("networkidle", timeout=5000)
                return True
            time.sleep(3)
        return True
    except:
        return False

# --- 3. 核心抓取流程 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    
    data = {col: "" for col in column_order}
    data.update({
        "Retailer": retailer, 
        "SRP": srp_url, 
        "Update Date": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    })
    
    try:
        # 1. 讀取 SRP 搜尋頁面
        page.goto(srp_url, wait_until="load", timeout=60000)
        time.sleep(3)
        tree = html.fromstring(page.content())
        
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
        
        if data["DD Name"] != "DD cannot be found":
            # --- 處理 Landing Page ---
            raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
            if raw_link:
                wait_for_redirect_smart(page, raw_link[0])
                data["Landing page URL"] = page.url
                data["Link works"] = check_status_text(page)
            
            # --- 處理 4 個 Categories ---
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                # 每次處理 Category 前回到 SRP 頁面
                page.goto(srp_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(1)
                
                c_tree = html.fromstring(page.content())
                c_name = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
                c_link = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
                
                data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
                link_val = c_link[0] if c_link else ""
                data[f"{cat_key} Link URL"] = link_val
                
                if link_val and link_val not in ["#", "N/A"]:
                    if wait_for_redirect_smart(page, link_val):
                        data[f"{cat_key} page URL"] = page.url
                        data[f"{cat_key} Link works"] = check_status_text(page)
                    else:
                        data[f"{cat_key} Link works"] = "Timeout"

    except Exception as e:
        print(f"⚠️ {retailer} 錯誤: {e}")
            
    return data

def upload_to_google(results_dict, gas_url):
    payload = list(results_dict.values())
    try:
        res = requests.post(gas_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=120)
        print(f"📡 GAS 回應: {res.text}")
    except Exception as e:
        print(f"❌ 上傳失敗: {e}")

# --- 4. 主執行程序 ---

def main():
    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]
    
    print("📥 讀取輸入清單...")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except Exception as e:
        print(f"❌ 讀取 CSV 失敗: {e}")
        return

    results_dict = {}

    with sync_playwright() as p:
        # 使用更擬真的瀏覽器設定
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale="en-US"
        )
        page = context.new_page()

        for index, row in df_input.iterrows():
            retailer = row.get('Retailer', f'Row_{index}')
            print(f"🚀 處理中 ({index+1}/{len(df_input)}): {retailer}")
            results_dict[retailer] = run_retailer_capture(page, row, column_order)
            # 每個商店處理完稍微停一下，降低被擋機率
            time.sleep(2)
        
        print("📤 上傳結果...")
        upload_to_google(results_dict, GAS_OUTPUT_URL)
        browser.close()

if __name__ == "__main__":
    main()