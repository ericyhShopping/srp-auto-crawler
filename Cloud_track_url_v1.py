import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import os

# --- 1. 基礎工具函式 ---

def is_intermediate_domain(url):
    """ 
    判定是否為中間轉址網域。
    如果 URL 包含 Yahoo 或黑名單中的追蹤網域，則回傳 True。
    """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        # Yahoo 體系
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com",
        # 廣告與追蹤轉址
        "affinity.net", "bizrate.com", "shophermedia.net", "provenpixel.com",
        "socialiqredir.com", "discounthero.org", "magik.ly", "netsourceio.com",
        "clickroll.net", "shopping123.com", "top-best.com",
        "v2i8b.com", "beyondcheap.com", "intentxredir.com",
        "peakoptions.site"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """
    檢查網頁狀態與內容。
    """
    try:
        current_url = page.url
        # 如果最後還是停留在中間網域，標示為 Error
        if is_intermediate_domain(current_url): return "Error"
        if not response: return "No"

        page_title = page.title().lower()
        page_content = page.content().lower()
        
        not_found_keywords = [
            "page not found", "404", "dead end", "page cannot be found"
        ]
        
        if any(k in page_title for k in not_found_keywords) or \
           any(k in page_content for k in not_found_keywords):
            return "404"

        status = response.status
        if status == 403 or "access denied" in page_title: return "Yes"
        if status >= 400: return "No"
        
        return "Yes" if 200 <= status < 300 else "No"
    except:
        return "No"

def safe_goto(page, url, wait_until="load", timeout=60000):
    try:
        return page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception as e:
        if "interrupted" in str(e):
            time.sleep(2)
            return None
        return None

def wait_for_redirect_smart(page, initial_url):
    """
    改良版：強制等待直到 URL 脫離 Yahoo/黑名單網域。
    """
    try:
        resp = safe_goto(page, initial_url, wait_until="commit")
        
        # 最多等待 10 次循環 (每次 3-4 秒)，直到跳出中間網域
        for attempt in range(10):
            current_url = page.url
            if not is_intermediate_domain(current_url):
                # 已經抵達零售商網頁，再多等一下下確保網址穩定
                page.wait_for_timeout(2000)
                return resp
            
            # 如果還在 Yahoo，模擬滑鼠微動並等待
            print(f"      ...等待轉址中 (目前仍在中轉頁: {current_url[:40]}...)")
            page.mouse.move(random.randint(100, 300), random.randint(100, 300))
            page.wait_for_timeout(3500)
            
        return resp
    except:
        return None

# --- 2. 核心抓取流程 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    
    data = {col: ("N/A" if "Check" in col else "") for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    capture_status = {"Landing": False, "Cat1": False, "Cat2": False, "Cat3": False, "Cat4": False}

    for attempt in range(1, 4): # 減少嘗試次數但增加單次轉址等待時間
        needed = [k for k, v in capture_status.items() if v is False]
        if not needed: break
        
        try:
            safe_goto(page, srp_url, wait_until="load")
            time.sleep(3)
            tree = html.fromstring(page.content())
            
            dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
            data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
            if not dd_label: break 

            # Landing Page
            if not capture_status["Landing"]:
                raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
                if raw_link:
                    resp = wait_for_redirect_smart(page, raw_link[0])
                    data["Link works"] = get_link_status(page, resp)
                    data["Landing page URL"] = page.url
                    if not is_intermediate_domain(page.url):
                        capture_status["Landing"] = True

            # Categories
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                if not capture_status[cat_key]:
                    # 確保回到 SRP 頁面抓取分類連結
                    if "yahoo.com" not in page.url: 
                        safe_goto(page, srp_url, wait_until="domcontentloaded")
                    
                    c_tree = html.fromstring(page.content())
                    c_name = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
                    c_link = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
                    
                    data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
                    l_val = c_link[0] if c_link else ""
                    data[f"{cat_key} Link URL"] = l_val

                    if l_val and l_val not in ["", "#", "N/A"]:
                        c_resp = wait_for_redirect_smart(page, l_val)
                        data[f"{cat_key} page URL"] = page.url
                        data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
                        if not is_intermediate_domain(page.url) and data[f"{cat_key} Name"] != "N/A":
                            capture_status[cat_key] = True
                    else:
                        data[f"{cat_key} Link works"] = "No"
                        capture_status[cat_key] = True 
        except: pass
    return data

# --- 3. 主程式執行 ---

def process_srp():
    input_file = 'urls.csv'
    output_file = 'srp_final_results.csv'
    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", "Link Check",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works", "Cat1 Link Check",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works", "Cat2 Link Check",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works", "Cat3 Link Check",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works", "Cat4 Link Check"
    ]

    if not os.path.exists(input_file): return

    df_input = pd.read_csv(input_file)
    results_dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0 ...")
        page = context.new_page()

        for index, row in df_input.iterrows():
            print(f"🚀 {index+1}/{len(df_input)}: {row['Retailer']}")
            results_dict[row['Retailer']] = run_retailer_capture(page, row, column_order)
            pd.DataFrame(list(results_dict.values()))[column_order].to_csv(output_file, index=False, encoding='utf-8-sig')

        browser.close()
        print(f"\n✅ 任務完成。")

if __name__ == "__main__":
    process_srp()