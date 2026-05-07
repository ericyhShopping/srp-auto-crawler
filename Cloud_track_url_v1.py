import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import os
import requests

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyHzJ9JEC-8AuEJmcpZpjXdpfVrKcMmO3pvstNvZQyv-_c0jlmoqBHt4jnW3IwrDiK0Hg/exec"

# --- 2. 工具函式 ---

def is_intermediate_domain(url):
    """ 判定是否為中間轉址網域。 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com",
        "affinity.net", "bizrate.com", "shophermedia.net", "provenpixel.com",
        "socialiqredir.com", "discounthero.org", "magik.ly", "netsourceio.com",
        "clickroll.net", "shopping123.com", "top-best.com",
        "v2i8b.com", "beyondcheap.com", "intentxredir.com", "peakoptions.site"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """ 判定 404 關鍵字與連結效力。 """
    try:
        current_url = page.url
        if is_intermediate_domain(current_url): return "Error"
        if not response: return "No"

        page_title = page.title().lower()
        page_content = page.content().lower()
        not_found_keywords = ["page not found", "404", "dead end", "page cannot be found"]
        
        if any(k in page_title for k in not_found_keywords) or \
           any(k in page_content for k in not_found_keywords):
            return "404"

        status = response.status
        if status == 403 or "access denied" in page_title: return "Yes"
        if status >= 400: return "No"
        return "Yes" if 200 <= status < 300 else "No"
    except:
        return "No"

def wait_for_redirect_smart(page, initial_url):
    """ 追蹤轉址直到脫離 Yahoo。 """
    try:
        resp = page.goto(initial_url, wait_until="commit", timeout=60000)
        for _ in range(12): 
            if not is_intermediate_domain(page.url):
                page.wait_for_timeout(2000)
                return resp
            page.mouse.move(random.randint(100, 300), random.randint(100, 300))
            page.wait_for_timeout(4000)
        return resp
    except:
        return None

# --- 3. 核心抓取函式 (這部分你原本漏掉了) ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: ("N/A" if "Check" in col else "") for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    try:
        page.goto(srp_url, wait_until="load", timeout=60000)
        time.sleep(3)
        tree = html.fromstring(page.content())
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
        
        if dd_label:
            # 抓取 Landing Page
            raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
            if raw_link:
                resp = wait_for_redirect_smart(page, raw_link[0])
                data["Link works"] = get_link_status(page, resp)
                data["Landing page URL"] = page.url

            # 抓取 Cat1 - Cat4
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                if page.url != srp_url: page.goto(srp_url, wait_until="domcontentloaded")
                c_tree = html.fromstring(page.content())
                c_name = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
                c_link = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
                
                data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
                if c_link:
                    l_val = c_link[0]
                    data[f"{cat_key} Link URL"] = l_val
                    c_resp = wait_for_redirect_smart(page, l_val)
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
    except Exception as e:
        print(f"抓取 {retailer} 時出錯: {e}")
    return data

# --- 4. 主執行流程 ---

def main():
    print(f"🚀 啟動任務，使用硬編碼網址...")
    
    if not GSHEET_INPUT_URL or not GAS_OUTPUT_URL:
        print("❌ 錯誤：網址未設定")
        return

    # 讀取資料
    df_input = pd.read_csv(GSHEET_INPUT_URL)
    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", "Link Check",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works", "Cat1 Link Check",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works", "Cat2 Link Check",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works", "Cat3 Link Check",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works", "Cat4 Link Check"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = context.new_page()

        for index, row in df_input.iterrows():
            retailer_name = row['Retailer']
            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            
            # 執行抓取
            result_data = run_retailer_capture(page, row, column_order)
            
            # 即時傳回 GAS
            try:
                requests.post(GAS_OUTPUT_URL, json=result_data, timeout=30)
                print(f"✅ {retailer_name} 資料已成功回傳")
            except Exception as e:
                print(f"❌ {retailer_name} 回傳失敗: {e}")

        browser.close()
    print("🎉 所有任務已完成！")

if __name__ == "__main__":
    main()