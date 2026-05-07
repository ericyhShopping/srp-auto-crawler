import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import os
import requests
import json

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
# 這裡已更新為你提供的新 GAS 網址
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyLvET6p-l_zxQaoJqk-XLg6PEd--bBu__lqqRcc_LF70cgHmijRRpXSr2Kf50NHlLoiA/exec"

# --- 2. 工具函式 ---

def is_intermediate_domain(url):
    """ 判定是否為 Yahoo 或中間跳轉網域 """
    if not url: return True
    url_lower = url.lower()
    blacklist = ["yahoo.com", "search.yahoo.com", "shopping.yahoo.com", "affinity.net", "bizrate.com", "ebay.com/rover", "rover.ebay.com", "peakoptions.site"]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """ 檢查 404 關鍵字與網頁存取狀態 """
    try:
        current_url = page.url
        page_title = page.title().lower()
        
        # Ebay 特別快速通關
        if "ebay.com" in current_url and not is_intermediate_domain(current_url):
            return "Yes"
            
        if is_intermediate_domain(current_url): return "Error"
        if not response: return "No"

        page_content = page.content().lower()
        not_found_keywords = ["page not found", "404", "dead end", "page cannot be found"]
        if any(k in page_title for k in not_found_keywords) or any(k in page_content for k in not_found_keywords):
            return "404"

        status = response.status
        if status == 403 or "access denied" in page_title: return "Yes"
        return "Yes" if 200 <= status < 300 else "No"
    except: return "No"

def wait_for_redirect_smart(page, initial_url):
    """ 轉址追蹤（加速版） """
    try:
        resp = page.goto(initial_url, wait_until="commit", timeout=35000)
        for _ in range(5): 
            if not is_intermediate_domain(page.url):
                page.wait_for_timeout(1500) 
                return resp
            page.wait_for_timeout(2500) 
        return resp
    except: return None

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    
    # 徹底移除 Link Check，僅初始化現有 22 個欄位
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    try:
        page.goto(srp_url, wait_until="load", timeout=30000)
        time.sleep(2)
        tree = html.fromstring(page.content())
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
        
        if dd_label:
            # Landing Page 抓取
            raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
            if raw_link:
                resp = wait_for_redirect_smart(page, raw_link[0])
                data["Link works"] = get_link_status(page, resp)
                data["Landing page URL"] = page.url

            # Cat 1-4 抓取
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                if "yahoo.com" not in page.url:
                    page.goto(srp_url, wait_until="domcontentloaded", timeout=20000)
                
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
    except:
        pass
    return data

# --- 4. 主執行流程 ---

def main():
    print("🚀 啟動任務：Link Check 已移除，使用新 GAS URL")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except Exception as e:
        print(f"❌ 來源讀取失敗: {e}"); return

    # 徹底移除所有 Link Check 相關欄位 (總共 22 個欄位)
    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = context.new_page()

        for index, row in df_input.iterrows():
            retailer_name = row['Retailer']
            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            
            result_data = run_retailer_capture(page, row, column_order)
            
            try:
                # 確保回傳正確的欄位
                requests.post(GAS_OUTPUT_URL, json=result_data, timeout=30)
                print(f"✅ {retailer_name} 資料已傳回")
            except:
                print(f"❌ {retailer_name} 網路回傳失敗")

        browser.close()
    print("🎉 任務完成！")

if __name__ == "__main__":
    main()