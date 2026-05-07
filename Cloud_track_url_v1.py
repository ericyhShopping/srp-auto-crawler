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
    """ 檢查頁面中是否含有錯誤關鍵字，若有回傳 '404'，否則回傳空值 """
    try:
        # 取得標題與內容並轉為小寫
        content = page.content().lower()
        title = page.title().lower()
        
        error_keywords = [
            "page not found", 
            "404", 
            "dead end", 
            "page cannot be found", 
            "403"
        ]
        
        if any(kw in title for kw in error_keywords) or any(kw in content for kw in error_keywords):
            return "404"
        return "" # 正常則顯示空值
    except:
        return "Error"

def is_intermediate_domain(url):
    """ 判定是否為中間跳轉或無效網域 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "us.search.yahoo.com", "yahoo.com/rdlw", "r.search.yahoo.com",
        "affinity.net", "bizrate.com", "shophermedia.net", "provenpixel.com",
        "socialiqredir.com", "discounthero.org", "magik.ly", "netsourceio.com",
        "clickroll.net", "shopping123.com", "top-best.com", "v2i8b.com", 
        "beyondcheap.com", "intentxredir.com", "peakoptions.site"
    ]
    return any(k in url_lower for k in blacklist)

def wait_for_redirect_smart(page, initial_url):
    """ 智能等待跳轉直到脫離黑名單網域 """
    try:
        page.goto(initial_url, wait_until="commit", timeout=60000)
        for _ in range(8):
            if not is_intermediate_domain(page.url): return True
            page.wait_for_timeout(3000)
        return True
    except:
        return False

# --- 3. 核心抓取流程 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    
    # 初始化資料
    data = {col: "" for col in column_order}
    data.update({
        "Retailer": retailer, 
        "SRP": srp_url, 
        "Update Date": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    })
    
    try:
        page.goto(srp_url, wait_until="load", timeout=60000)
        time.sleep(2)
        tree = html.fromstring(page.content())
        
        # 抓取 DD Name
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
        
        if data["DD Name"] != "DD cannot be found":
            # 1. Landing Page 處理
            raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
            if raw_link:
                wait_for_redirect_smart(page, raw_link[0])
                data["Landing page URL"] = page.url
                data["Link works"] = check_status_text(page)
            
            # 2. 4 個 Categories 處理
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                if page.url != srp_url: 
                    page.goto(srp_url, wait_until="domcontentloaded")
                
                c_tree = html.fromstring(page.content())
                c_name = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
                c_link = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
                
                data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
                link_val = c_link[0] if c_link else ""
                data[f"{cat_key} Link URL"] = link_val
                
                if link_val and link_val not in ["#", "N/A"]:
                    wait_for_redirect_smart(page, link_val)
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = check_status_text(page)

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
    # 已刪除所有 Check 相關欄位
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
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for index, row in df_input.iterrows():
            retailer = row.get('Retailer', f'Row_{index}')
            print(f"🚀 處理中 ({index+1}/{len(df_input)}): {retailer}")
            results_dict[retailer] = run_retailer_capture(page, row, column_order)
        
        print("📤 上傳結果...")
        upload_to_google(results_dict, GAS_OUTPUT_URL)
        browser.close()

if __name__ == "__main__":
    main()