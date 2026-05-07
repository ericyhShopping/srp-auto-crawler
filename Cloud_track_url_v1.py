import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import os
import requests

# --- 1. 環境變數讀取 ---
# 這裡會讀取 YAML 中 env 區塊定義的變數
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyHzJ9JEC-8AuEJmcpZpjXdpfVrKcMmO3pvstNvZQyv-_c0jlmoqBHt4jnW3IwrDiK0Hg/exec"

def main():
    print(f"DEBUG: 正在使用硬編碼網址執行...")

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
    """ 核心判定邏輯：包含 404 關鍵字檢查。 """
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
    """ 強制追蹤轉址，直到脫離 Yahoo/中間網域。 """
    try:
        resp = page.goto(initial_url, wait_until="commit", timeout=60000)
        for _ in range(12): 
            if not is_intermediate_domain(page.url):
                page.wait_for_timeout(2000)
                return resp
            print(f"      ...等待跳轉中 (目前: {page.url[:50]}...)")
            page.mouse.move(random.randint(100, 300), random.randint(100, 300))
            page.wait_for_timeout(4000)
        return resp
    except:
        return None

def main():
    # 檢查變數是否成功讀取
    if not GSHEET_INPUT_URL or not GAS_OUTPUT_URL:
        print("❌ 錯誤：找不到 GSHEET_INPUT_URL 或 GAS_OUTPUT_URL 變數")
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
            print(f"🚀 Processing: {row['Retailer']}")
            # 這裡執行原本的抓取邏輯 (run_retailer_capture)
            # ... (抓取邏輯代碼) ...
            
            # 回傳 GAS
            # requests.post(GAS_OUTPUT_URL, json=result_data)

        browser.close()
    print("🎉 所有任務已完成！")

if __name__ == "__main__":
    main()