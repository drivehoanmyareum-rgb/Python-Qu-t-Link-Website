#!/usr/bin/env python3
# smart_form_scanner.py
"""
Quét thông minh để tìm 'link tới trang submit' từ nhiều loại phần tử:
- <a>, <button>, <span>, <div role="button">, phần tử có onclick/data-href hoặc nằm trong <a>.
- Text khớp các từ khoá: 'submit', 'add site', 'suggest', 'list your site', 'đăng ký', 'gửi', 'thêm', v.v.

Với mỗi candidate:
- Resolve URL (urljoin khi relative). Nếu là javascript/onclick -> thử click().
- Mở trang (depth=1) và kiểm tra có <form>.
- Nếu có -> lưu link form + field meta vào folder riêng cho website.

Usage:
  python smart_form_scanner.py urls.txt --out scan_outputs [--headful] [--timeout 20]
"""

import os, re, sys, json, time, argparse
from urllib.parse import urljoin, urlparse
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

# ==================== CONFIG ====================
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_CANDIDATES = 0  # 0 = không giới hạn, >0 = giới hạn số candidate theo site


TEXT_KEYWORDS = [
    "submit","add site","add url","suggest","list your site",
    "đăng ký","đăng","gửi","thêm","gửi liên hệ","gửi thông tin","đề xuất","đưa trang lên",
    "submit.php","add","suggest-site"
]
HREF_KEYWORDS = [
    "submit","add","suggest","add-site","suggest-site","submit.php","contact","signup","register"
]
CAPTCHA_PAT = re.compile(r"captcha|recaptcha|g-recaptcha", re.I)

# ==================== HELPERS ====================
def sanitize_folder_name(url: str) -> str:
    p = urlparse(url)
    host = p.netloc or p.path
    host = host.replace(":", "_")
    return re.sub(r"[^A-Za-z0-9\-_\.]", "_", host)[:200]

def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def save_text(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")

def take_snapshot(driver, folder: str, prefix="snapshot"):
    ts = int(time.time())
    shot = os.path.join(folder, f"{prefix}_{ts}.png")
    try:
        driver.save_screenshot(shot)
    except Exception:
        shot = None
    html = os.path.join(folder, f"{prefix}_{ts}.html")
    try:
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(driver.page_source[:200000])
    except Exception:
        html = None
    return {"screenshot": shot, "html": html}

def wait_ready(driver, timeout: int):
    WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")

def open_url(driver, url: str, timeout: int) -> bool:
    try:
        driver.get(url)
        wait_ready(driver, min(10, timeout))
        time.sleep(0.2)
        return True
    except Exception:
        if not url.startswith("http"):
            try:
                driver.get("http://" + url)
                wait_ready(driver, min(10, timeout))
                time.sleep(0.2)
                return True
            except Exception:
                return False
        return False

def is_form_present(driver) -> bool:
    try:
        forms = driver.find_elements(By.TAG_NAME, "form")
        return len(forms) > 0
    except Exception:
        return False

def extract_forms_meta(driver):
    forms_meta = []
    forms = driver.find_elements(By.TAG_NAME, "form")
    for idx, form in enumerate(forms):
        try:
            action = form.get_attribute("action") or ""
            method = (form.get_attribute("method") or "GET").upper()
            fields = []
            elems = form.find_elements(By.XPATH, ".//input|.//textarea|.//select")
            for el in elems:
                try:
                    tag = el.tag_name.lower()
                    itype = el.get_attribute("type") or tag
                    name = el.get_attribute("name") or ""
                    elid = el.get_attribute("id") or ""
                    placeholder = el.get_attribute("placeholder") or ""
                    required = bool(el.get_attribute("required"))
                    label = ""
                    if elid:
                        labs = driver.find_elements(By.XPATH, f"//label[@for='{elid}']")
                        if labs:
                            label = labs[0].text
                    if not label:
                        try:
                            # label tổ tiên gần nhất
                            anc_lab = el.find_element(By.XPATH, "./ancestor::label[1]")
                            label = anc_lab.text
                        except Exception:
                            pass
                    entry = {
                        "tag": tag, "type": itype, "name": name, "id": elid,
                        "placeholder": placeholder, "label": (label or "").strip()[:200],
                        "required": required
                    }
                    if tag == "select":
                        try:
                            select = Select(el)
                            entry["options"] = [{"value": o.get_attribute("value"), "text": o.text} for o in select.options]
                        except Exception:
                            entry["options"] = []
                    fields.append(entry)
                except Exception:
                    continue
            forms_meta.append({"form_index": idx, "action": action, "method": method, "fields": fields})
        except Exception:
            continue
    return forms_meta

def match_keyword(txt: str, patterns) -> bool:
    t = (txt or "").strip().lower()
    if not t:
        return False
    return any(k in t for k in patterns)

def get_clickable_parent_link(elem):
    """Nếu elem nằm trong <a> thì lấy <a> đó."""
    try:
        a = elem.find_element(By.XPATH, "./ancestor::a[1]")
        return a
    except Exception:
        return None

# ==================== SMART CANDIDATE FINDER ====================
def collect_submit_candidates(driver):
    """
    Thu thập 'điểm submit' từ nhiều phần tử:
    - <a> (href chứa keyword, text chứa keyword)
    - <button> (text), <span>/<div> có role=button hoặc onclick, hoặc text khớp và có <a> tổ tiên
    Trả về list dict: { 'how':'href|click', 'text':..., 'abs_url':..., 'element': WebElement or None }
    """
    results = []
    current = driver.current_url

    # 1) Anchors trực tiếp
    anchors = driver.find_elements(By.TAG_NAME, "a")
    for a in anchors:
        try:
            txt = (a.text or "").strip()
            href = (a.get_attribute("href") or "").strip()
            if match_keyword(txt, TEXT_KEYWORDS) or match_keyword(href, HREF_KEYWORDS):
                abs_url = href and (None if href.lower().startswith("javascript:") else urljoin(current, href))
                results.append({"how": "href" if abs_url else "click", "text": txt, "abs_url": abs_url, "element": a})
        except Exception:
            continue

    # 2) Buttons
    buttons = driver.find_elements(By.TAG_NAME, "button")
    for b in buttons:
        try:
            txt = (b.text or "").strip()
            if match_keyword(txt, TEXT_KEYWORDS):
                # nếu button có form submit trực tiếp, ta vẫn coi là candidate 'click'
                results.append({"how": "click", "text": txt, "abs_url": None, "element": b})
        except Exception:
            continue

    # 3) Spans / Div role=button hoặc có onclick, hoặc text khớp + có thẻ a tổ tiên
    spans = driver.find_elements(By.TAG_NAME, "span")
    divs  = driver.find_elements(By.TAG_NAME, "div")
    for elems in (spans, divs):
        for e in elems:
            try:
                txt = (e.text or "").strip()
                role = (e.get_attribute("role") or "").lower()
                onclick = (e.get_attribute("onclick") or "") or ""
                data_href = (e.get_attribute("data-href") or "") or ""
                parent_a = get_clickable_parent_link(e)

                candidate = None
                # Ưu tiên có data-href
                if data_href:
                    abs_url = urljoin(current, data_href)
                    candidate = {"how": "href", "text": txt, "abs_url": abs_url, "element": e}
                # span/div có role=button hoặc onclick
                elif role == "button" or onclick:
                    candidate = {"how": "click", "text": txt, "abs_url": None, "element": e}
                # text khớp keyword + có <a> tổ tiên
                if not candidate and match_keyword(txt, TEXT_KEYWORDS) and parent_a is not None:
                    href = (parent_a.get_attribute("href") or "").strip()
                    abs_url = href and (None if href.lower().startswith("javascript:") else urljoin(current, href))
                    candidate = {"how": "href" if abs_url else "click", "text": txt, "abs_url": abs_url, "element": parent_a}

                # thêm nếu có ý nghĩa (text/onclick/href)
                if candidate and (candidate["abs_url"] or candidate["element"] is not None or candidate["text"]):
                    results.append(candidate)
            except Exception:
                continue

    # 4) Loại bỏ trùng lặp theo (abs_url, how, text)
    dedup = []
    seen = set()
    for c in results:
        key = (c["abs_url"] or "CLICK@" + (c["text"] or "")) + "|" + c["how"]
        if key not in seen:
            seen.add(key)
            dedup.append(c)
    return dedup

# ==================== SCANNER CORE ====================
class SmartScanner:
    def __init__(self, headful=False, timeout=DEFAULT_TIMEOUT, max_candidates=DEFAULT_MAX_CANDIDATES, status_cb=None):
        self.headful = headful
        self.timeout = timeout
        self.max_candidates = int(max_candidates) if max_candidates else 0
        self.log = status_cb or (lambda s: print("[STATUS]", s))
        self.driver = self._init_driver()

    def _init_driver(self):
        options = webdriver.ChromeOptions()
        if not self.headful:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36")
        try:
            drv = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
            # Hide webdriver flag (best effort)
            try:
                drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                })
            except Exception:
                pass
            return drv
        except WebDriverException as e:
            raise RuntimeError("Không khởi tạo được ChromeDriver: " + str(e))

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    def scan_website(self, url: str, out_root: str):
        folder = ensure_dir(os.path.join(out_root, sanitize_folder_name(url)))
        meta = {"url": url, "found_forms": [], "notes": [], "candidates_followed": 0}
        form_links = []
        form_meta_map = {}

        # 1) mở trang chính
        self.log(f"[{url}] Mở trang chính...")
        if not open_url(self.driver, url, self.timeout):
            meta["notes"].append("cannot_open_main_page")
            save_json(os.path.join(folder, "meta.json"), meta)
            save_text(os.path.join(folder, "note.txt"), "Không mở được trang chính")
            return meta

        # captcha trên root?
        if CAPTCHA_PAT.search(self.driver.page_source or ""):
            meta["notes"].append("captcha_on_root")
            take_snapshot(self.driver, folder, "captcha_root")
            save_json(os.path.join(folder, "meta.json"), meta)
            return meta

        # 2) nếu trang chính đã có form → lưu ngay
        if is_form_present(self.driver):
            self.log(f"[{url}] Tìm thấy form trên trang chính.")
            forms = extract_forms_meta(self.driver)
            form_url = self.driver.current_url
            form_links.append(form_url)
            form_meta_map[form_url] = forms

            snap = take_snapshot(self.driver, folder, "form_root")
            # Lưu bản đầu tiên ra file riêng
            if snap.get("html"):
                os.replace(snap["html"], os.path.join(folder, "page.html"))
            if snap.get("screenshot"):
                os.replace(snap["screenshot"], os.path.join(folder, "screenshot.png"))

        # 3) thu thập các candidate submit từ nhiều phần tử
        candidates = collect_submit_candidates(self.driver)
        orig_count = len(candidates)
        if self.max_candidates and orig_count > self.max_candidates:
            candidates = candidates[:self.max_candidates]
            self.log(f"[{url}] Tìm thấy {orig_count} candidate submit — giới hạn xuống {len(candidates)} theo cấu hình.")
        else:
            self.log(f"[{url}] Tìm thấy {orig_count} candidate submit.")

        # lưu lại thống kê
        meta["candidate_count_found"] = orig_count
        meta["candidate_count_follow_limit"] = len(candidates)


        # 4) lần lượt follow candidate (depth=1)
        for i, cand in enumerate(candidates, start=1):
            # bỏ qua nếu đã có đủ form? (ở đây vẫn thử hết để thu nhiều link)
            try:
                meta["candidates_followed"] += 1
                if cand["abs_url"]:
                    # Truy cập thẳng
                    self.log(f"[{url}] [{i}/{len(candidates)}] GET: {cand['abs_url']}")
                    if not open_url(self.driver, cand["abs_url"], self.timeout):
                        meta["notes"].append(f"cannot_open_candidate:{cand['abs_url']}")
                        continue
                else:
                    # Click phần tử (javascript/onclick/role=button)
                    self.log(f"[{url}] [{i}/{len(candidates)}] CLICK candidate element (text='{cand.get('text','')[:60]}')")
                    try:
                        cand["element"].click()
                        wait_ready(self.driver, min(10, self.timeout))
                        time.sleep(0.2)
                    except Exception:
                        meta["notes"].append("click_failed_candidate")
                        continue

                # Captcha trên candidate?
                if CAPTCHA_PAT.search(self.driver.page_source or ""):
                    meta["notes"].append("captcha_on_candidate")
                    take_snapshot(self.driver, folder, f"captcha_candidate_{i}")
                    continue

                # Check form
                if is_form_present(self.driver):
                    form_url = self.driver.current_url
                    if form_url not in form_links:
                        self.log(f"[{url}]   → Có form tại: {form_url}")
                        forms = extract_forms_meta(self.driver)
                        form_links.append(form_url)
                        form_meta_map[form_url] = forms
                        # Lưu snapshot lần đầu
                        snap = take_snapshot(self.driver, folder, f"form_candidate_{i}")
                        # nếu là form đầu tiên của site chưa có page.html thì ghi ra
                        if not os.path.exists(os.path.join(folder, "page.html")) and snap.get("html"):
                            os.replace(snap["html"], os.path.join(folder, "page.html"))
                        if not os.path.exists(os.path.join(folder, "screenshot.png")) and snap.get("screenshot"):
                            os.replace(snap["screenshot"], os.path.join(folder, "screenshot.png"))
                else:
                    # heuristic fallback: thử /submit.php, /add, /suggest từ current
                    current = self.driver.current_url
                    for suffix in ["/submit.php", "/add", "/suggest"]:
                        fb = urljoin(current, suffix)
                        self.log(f"[{url}]    Thử fallback: {fb}")
                        if open_url(self.driver, fb, self.timeout) and is_form_present(self.driver):
                            form_url = self.driver.current_url
                            if form_url not in form_links:
                                self.log(f"[{url}]   → Có form (fallback) tại: {form_url}")
                                forms = extract_forms_meta(self.driver)
                                form_links.append(form_url)
                                form_meta_map[form_url] = forms
                                snap = take_snapshot(self.driver, folder, f"form_fallback_{i}")
                                if not os.path.exists(os.path.join(folder, "page.html")) and snap.get("html"):
                                    os.replace(snap["html"], os.path.join(folder, "page.html"))
                                if not os.path.exists(os.path.join(folder, "screenshot.png")) and snap.get("screenshot"):
                                    os.replace(snap["screenshot"], os.path.join(folder, "screenshot.png"))
                            break
            except Exception as e:
                meta["notes"].append(f"candidate_error:{str(e)[:120]}")
                continue

        # 5) lưu kết quả
        if form_links:
            meta["found_forms"] = form_links
            save_json(os.path.join(folder, "form_links.json"), form_links)
            # gộp form_meta_map thành list để dễ đọc
            form_meta_list = [{"url": u, "forms": form_meta_map[u]} for u in form_links]
            save_json(os.path.join(folder, "form_meta.json"), form_meta_list)
        else:
            # không tìm thấy form
            take_snapshot(self.driver, folder, "no_form")
            meta["notes"].append("no_form_found")

        save_json(os.path.join(folder, "meta.json"), meta)
        return meta

# ==================== CLI ====================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="File chứa danh sách URL (mỗi dòng 1 URL) hoặc 1 URL đơn lẻ")
    ap.add_argument("--out", default="scan_outputs", help="Thư mục gốc lưu kết quả")
    ap.add_argument("--headful", action="store_true", help="Mở Chrome có giao diện (mặc định headless)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout (giây)")
    ap.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
                help="Giới hạn số candidate submit theo dõi mỗi site (0 = không giới hạn).")
    
    args = ap.parse_args()

    # lấy danh sách URL
    urls = []
    p = Path(args.input)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                urls.append(line)
    else:
        urls = [args.input]

    out_root = ensure_dir(args.out)

    scanner = SmartScanner(headful=args.headful, timeout=args.timeout, max_candidates=args.max_candidates)

    try:
        all_meta = []
        for u in urls:
            print("="*70)
            print("Scanning:", u)
            m = scanner.scan_website(u, out_root)
            all_meta.append(m)
        save_json(os.path.join(out_root, "all_meta.json"), all_meta)
        print("\n✅ Done. Xem kết quả tại:", out_root)
    finally:
        scanner.close()

if __name__ == "__main__":
    main()

# python bulk_form_scanner.py directory_submission_unique_500.txt --out scan_outputs --headful --max-candidates 10