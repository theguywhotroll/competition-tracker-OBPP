import re
import struct
import subprocess
import sys
import time
from datetime import datetime

import pytz
import requests
from dateutil.relativedelta import relativedelta

_playwright_browser_ready = False


def _ensure_playwright_chromium():
    """Streamlit Community Cloud has no post-install hook to run
    `playwright install chromium` after deploy, so we lazily install it here
    on first use. This adds a one-time delay (~30-60s) the first time a
    Playwright-based fetcher runs after a fresh container boot; subsequent
    calls in the same running container are unaffected."""
    global _playwright_browser_ready
    if _playwright_browser_ready:
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        _playwright_browser_ready = True
    except Exception:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        _playwright_browser_ready = True  # let the real launch below surface any remaining error

COLUMNS = [
    "OBPP",
    "ISIN",
    "Issuer",
    "YTM (%)",
    "Rating",
    "Tenure (Months)",
    "Face Value",
    "Minimum Investment Amount",
]


# ---------------------------------------------------------------------------
# Aspero
# ---------------------------------------------------------------------------
def fetch_aspero():
    url = "https://retail-api.aspero.in/bff/api/v1/bond-listing"
    headers = {
        "Accept": "application/json",
        "channel": "invest",
        "Device-Id": "unknown",
        "x-product-id": "YUBIFIN",
    }
    rows = []
    try:
        resp = requests.get(url, headers=headers, params={"page": 1, "items": 200}, timeout=30)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        now = datetime.now()
        for item in items:
            tenure_months = ""
            maturity_date = item.get("maturity_date")
            if maturity_date:
                m = datetime.strptime(maturity_date, "%Y-%m-%d")
                diff = relativedelta(m, now)
                tenure_months = diff.years * 12 + diff.months
            rows.append({
                "OBPP": "Aspero",
                "ISIN": item.get("isin"),
                "Issuer": item.get("name"),
                "YTM (%)": item.get("listed_yield"),
                "Rating": item.get("credit_rating"),
                "Tenure (Months)": tenure_months,
                "Face Value": item.get("face_value"),
                "Minimum Investment Amount": item.get("min_investment"),
            })
        print(f"Aspero: {len(rows)} rows")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Aspero data: {e}")
    return rows


# ---------------------------------------------------------------------------
# Smest
# ---------------------------------------------------------------------------
RATING_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B|C|D)(?![A-Za-z0-9])"
)


def clean_smest_issuer(security_name):
    if not security_name:
        return ""
    name = re.sub(r"^\d+(\.\d+)?%\s*", "", security_name)
    name = re.sub(r"\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s*$", "", name)
    return name.strip()


def parse_smest_rating(ratings_org_name):
    if not ratings_org_name:
        return ""
    for entry in ratings_org_name:
        for key in entry.keys():
            m = RATING_TOKEN_RE.search(key)
            if m:
                return m.group(1).upper()
    return ""


def fetch_smest():
    url = "https://admin-api.smestbonds.com/admin/quotes/filter/v2/"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    rows = []
    now = datetime.now()

    def months_to_maturity(date_str):
        try:
            maturity = datetime.strptime(date_str, "%d/%m/%Y")
            diff = relativedelta(maturity, now)
            return diff.years * 12 + diff.months
        except ValueError:
            return None

    try:
        resp = requests.post(url, headers=headers, timeout=30)
        resp.raise_for_status()
        for item in resp.json():
            maturity_date_str = item.get("maturity_or_call_date")
            if not maturity_date_str:
                continue
            tenure_months = months_to_maturity(maturity_date_str)
            if tenure_months is None or tenure_months > 36:
                continue
            rows.append({
                "OBPP": "Smest",
                "ISIN": item.get("isin"),
                "Issuer": clean_smest_issuer(item.get("security_name")),
                "YTM (%)": item.get("sell_yield"),
                "Rating": parse_smest_rating(item.get("ratings_org_name")),
                "Tenure (Months)": tenure_months,
                "Face Value": "",
                "Minimum Investment Amount": item.get("minimum_quantity"),
            })
        print(f"Smest: {len(rows)} rows")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Smest data: {e}")
    return rows


# ---------------------------------------------------------------------------
# Wintwealth
# ---------------------------------------------------------------------------
def fetch_wintwealth():
    url = "https://api.wintwealth.com/products/all"
    rows = []
    now = datetime.now()
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        for item in resp.json().get("CURRENT", []):
            tenure_months = ""
            maturity_date = item.get("maturityDate")
            if maturity_date:
                m = datetime.strptime(maturity_date, "%Y-%m-%d")
                diff = relativedelta(m, now)
                tenure_months = max(diff.years * 12 + diff.months, 0)
            min_units = item.get("minUnitsForBuyTransaction") or 1
            current_price = item.get("currentPrice")
            min_investment = current_price * min_units if current_price is not None else ""
            rows.append({
                "OBPP": "Wintwealth",
                "ISIN": item.get("isin"),
                "Issuer": item.get("entityDisplayName") or item.get("productName", ""),
                "YTM (%)": item.get("targetXirr") or item.get("interestRate"),
                "Rating": item.get("bondRating", ""),
                "Tenure (Months)": tenure_months,
                "Face Value": item.get("issuePrice"),
                "Minimum Investment Amount": min_investment,
            })
        print(f"Wintwealth: {len(rows)} rows")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Wintwealth data: {e}")
    return rows


# ---------------------------------------------------------------------------
# Altifi
# ---------------------------------------------------------------------------
def fetch_altifi():
    url = "https://invest.altifi.ai/wm/api/signUp/instrument/opportunity?isCorporate=true&getSdi=false"
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    rows = []

    def months_left(maturity_date_str):
        now = datetime.now(pytz.utc)
        maturity = datetime.fromisoformat(maturity_date_str.replace("Z", "+00:00"))
        diff = relativedelta(maturity, now)
        return max(diff.years * 12 + diff.months, 0) if maturity > now else 0

    try:
        resp = requests.put(url, headers=headers, json={}, timeout=30)
        resp.raise_for_status()
        for item in resp.json().get("content", []):
            maturity_date_str = item.get("maturityDate", "")
            tenure_months = months_left(maturity_date_str) if maturity_date_str else ""
            rows.append({
                "OBPP": "Altifi",
                "ISIN": item.get("isinId", ""),
                "Issuer": item.get("instrumentName", ""),
                "YTM (%)": item.get("yield", ""),
                "Rating": item.get("rating", ""),
                "Tenure (Months)": tenure_months,
                "Face Value": "",
                "Minimum Investment Amount": item.get("investmentAmount", ""),
            })
        print(f"Altifi: {len(rows)} rows")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Altifi data: {e}")
    return rows


# ---------------------------------------------------------------------------
# Stable (proprietary protobuf feed - reconstructed via reverse engineering)
# ---------------------------------------------------------------------------
ISIN_RE_TEXT = re.compile(r"\bIN[A-Z0-9]{10}\b")
ISIN_RE_BYTES = re.compile(rb"IN[A-Z0-9]{10}")

RATING_AGENCIES = [
    "Crisil", "CRISIL", "ICRA", "CARE", "Care", "India Ratings", "India-Rating",
    "IND-RA", "IND Ra", "Brickwork", "BWR", "Acuite", "ACUITE", "IVR",
    "Infomerics", "Fitch", "SMERA", "Onicra",
]
AGENCY_PATTERN = "|".join(re.escape(a) for a in RATING_AGENCIES)

FACE_VALUE_RE = re.compile(r"B\s*[A-Za-z][A-Za-z\-\s]{2,20}?J\s*(\d+(?:\.\d+)?)R")
TENURE_RE = re.compile(r"j\s*((?:\d+\s*Y)?\s*(?:\d+\s*M)?\s*(?:\d+\s*D))\s*p", re.I)

ISSUER_SUFFIX_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.,'()\- ]+?(?:Limited|Ltd\.|Private Limited|Pvt\. Ltd\.|"
    r"Finance Limited|Financial Services Limited|Capital Limited|Corporation Limited))"
)


def safe_decode(content: bytes) -> str:
    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode("latin1", errors="ignore")


def clean_text(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = cleaned.replace("\xa0", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_tenure_to_months(text: str):
    m = re.search(r"(?:(\d+)\s*Y)?\s*(?:(\d+)\s*M)?\s*(?:(\d+)\s*D)?", text, re.I)
    if not m:
        return ""
    years = int(m.group(1)) if m.group(1) else 0
    months = int(m.group(2)) if m.group(2) else 0
    return years * 12 + months


def extract_clean_issuer(window: str):
    candidates = []
    for m in ISSUER_SUFFIX_RE.finditer(window):
        val = m.group(1).strip()
        if 5 <= len(val) <= 120:
            candidates.append(val)
    if not candidates:
        return ""
    return sorted(set(candidates), key=len)[0]


def extract_rating(window: str):
    am = re.search(AGENCY_PATTERN, window, re.I)
    if not am:
        return ""
    tail = window[am.end():am.end() + 80]
    m = RATING_TOKEN_RE.search(tail)
    return m.group(1).upper() if m else ""


def extract_face_value(window: str):
    m = FACE_VALUE_RE.search(window)
    return m.group(1) if m else ""


def extract_tenure_months(window: str):
    m = TENURE_RE.search(window)
    return parse_tenure_to_months(m.group(1)) if m else ""


def extract_ytm_and_min_investment(block: bytes):
    """YTM (field 4, double) sits right after the bond's title string.
    Minimum investment / current price (field 23, double) sits right after the ISIN.
    Both are located by scanning for their protobuf tag byte and validating the
    decoded double falls in a plausible range."""
    ytm = ""
    idx = 0
    while True:
        pos = block.find(b"!", idx)  # tag byte 0x21 = field 4, wire type 1 (double)
        if pos == -1 or pos + 9 > len(block):
            break
        val = struct.unpack("<d", block[pos + 1:pos + 9])[0]
        if 0.5 <= val <= 40:
            ytm = round(val, 2)
            break
        idx = pos + 1
    return ytm


def extract_min_investment_after_isin(raw: bytes, isin_end: int):
    def read_varint(buf, pos):
        result, shift = 0, 0
        while pos < len(buf):
            b = buf[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    pos = isin_end
    try:
        tag, pos = read_varint(raw, pos)
        field, wire = tag >> 3, tag & 7
        if field == 23 and wire == 1:
            return round(struct.unpack("<d", raw[pos:pos + 8])[0], 2)
    except (IndexError, struct.error):
        pass
    return ""


def fetch_indiabonds():
    list_url = "https://prod-api.indiabonds.com/api/v3/web/bond-list/"
    detail_url_tmpl = "https://prod-api.indiabonds.com/api/v3/web/bond-details/{isin}/"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    rows = []
    now = datetime.now()
    try:
        resp = requests.get(
            list_url,
            params={"page_no": 1, "page_size": 200, "sort_by": "yield_high_to_low", "tag_name": "All Bonds"},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        bond_list = resp.json().get("bond_list", [])

        session = requests.Session()
        session.headers.update(headers)

        for item in bond_list:
            isin = item.get("isin", "")
            tenure_months = ""
            maturity_date = item.get("maturity_date")
            if maturity_date:
                m = datetime.strptime(maturity_date, "%d %b %Y")
                diff = relativedelta(m, now)
                tenure_months = max(diff.years * 12 + diff.months, 0)

            face_value = ""
            if isin:
                try:
                    d_resp = session.get(detail_url_tmpl.format(isin=isin), timeout=15)
                    d_resp.raise_for_status()
                    fv_str = d_resp.json().get("pricing_details_t0", {}).get("face_value", "")
                    fv_digits = re.sub(r"[^\d.]", "", fv_str or "")
                    face_value = float(fv_digits) if fv_digits else ""
                except (requests.exceptions.RequestException, ValueError):
                    face_value = ""

            yield_str = item.get("yield_value", "") or ""
            rows.append({
                "OBPP": "IndiaBonds",
                "ISIN": isin,
                "Issuer": item.get("issuer_name", ""),
                "YTM (%)": yield_str.replace("%", "").strip(),
                "Rating": item.get("rating", ""),
                "Tenure (Months)": tenure_months,
                "Face Value": face_value,
                "Minimum Investment Amount": item.get("price", ""),
            })
        print(f"IndiaBonds: {len(rows)} rows")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching IndiaBonds data: {e}")
    return rows


# ---------------------------------------------------------------------------
# BondsIndia
# ---------------------------------------------------------------------------
def fetch_bondsindia():
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    rows = []
    try:
        page_resp = requests.get("https://www.bondsindia.com/portfolio", headers=headers, timeout=30)
        page_resp.raise_for_status()
        build_id_m = re.search(r'"buildId":"([^"]+)"', page_resp.text)
        if not build_id_m:
            raise ValueError("Could not locate Next.js buildId on bondsindia.com/portfolio")
        build_id = build_id_m.group(1)

        data_url = f"https://www.bondsindia.com/_next/data/{build_id}/portfolio.json"
        resp = requests.get(
            data_url, params={"orderBy": "DESC", "orderByType": "YIELD", "page": 1}, headers=headers, timeout=30
        )
        resp.raise_for_status()
        page_props = resp.json()["pageProps"]
        total_records = page_props.get("totalrecord", 0)
        bond_list = page_props.get("Response1", [])

        if total_records and len(bond_list) < total_records:
            resp = requests.get(
                data_url,
                params={"orderBy": "DESC", "orderByType": "YIELD", "page": max(total_records, 3)},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            bond_list = resp.json()["pageProps"].get("Response1", [])

        for item in bond_list:
            rows.append({
                "OBPP": "BondsIndia",
                "ISIN": item.get("Security_Id", ""),
                "Issuer": item.get("Issuer_Name", ""),
                "YTM (%)": item.get("YTM", ""),
                "Rating": item.get("Rating", ""),
                "Tenure (Months)": item.get("Maturity_IN_MONTH", ""),
                "Face Value": item.get("Face_Value", ""),
                "Minimum Investment Amount": item.get("t0_min_investment", ""),
            })
        print(f"BondsIndia: {len(rows)} rows")
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        print(f"Error fetching BondsIndia data: {e}")
    return rows


# ---------------------------------------------------------------------------
# Jiraaf
# ---------------------------------------------------------------------------
def fetch_jiraaf():
    url = "https://www.jiraaf.com/api/opportunities-and-bonds/filters"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    rows = []
    try:
        page = 1
        limit = 19  # API rejects limit >= 20
        total = None
        while total is None or len(rows) < total:
            resp = requests.get(
                url,
                params={"status": "open", "type": "bond", "page": page, "limit": limit},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            entity = resp.json().get("entity", {})
            items = entity.get("rows", [])
            if total is None:
                total = entity.get("count", len(items))
            if not items:
                break

            for item in items:
                total_days = item.get("total_days")
                tenure_months = round(total_days / 30.44) if total_days else ""
                irr = item.get("displayIRR")
                try:
                    irr = float(irr) if irr not in (None, "") else ""
                except (TypeError, ValueError):
                    pass
                rows.append({
                    "OBPP": "Jiraaf",
                    "ISIN": item.get("isin", ""),
                    "Issuer": item.get("title", ""),
                    "YTM (%)": irr,
                    "Rating": item.get("riskRating", ""),
                    "Tenure (Months)": tenure_months,
                    "Face Value": "",
                    "Minimum Investment Amount": item.get("minInvestmentAmount", ""),
                })
            page += 1
        print(f"Jiraaf: {len(rows)} rows")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Jiraaf data: {e}")
    return rows


# ---------------------------------------------------------------------------
# GoldenPi (API requires an x-gpi-client-token that's generated client-side
# and validated server-side by means we couldn't replicate directly, so we
# load the site in a real browser and let its own JS mint a valid token)
# ---------------------------------------------------------------------------
GPI_FETCH_JS = """
async () => {
    const token = JSON.parse(localStorage.getItem('guestToken'));
    const all = [];
    let offset = 1;
    while (true) {
        const body = {
            bondsListFilters: {
                filters: {listed: [1], assetClass: ["NCD", "NCD-IPO"]},
                limit: 10,
                offset: offset,
                includeExtraParamsInResult: ["isin", "issuerId", "lockinMonths", "discountPrice", "ipoOpenDate", "sgbStatus", "minLotSize"],
                sortBy: {key: "ytmc", type: "desc"}
            },
            includeOfferDetail: true
        };
        const res = await fetch('https://api.goldenpi.com/v0/bonds/list', {
            method: 'POST',
            headers: {'x-gpi-client-token': token, 'Accept': 'application/json', 'Content-Type': 'application/json', 'gpi-platform': 'desktop_web'},
            body: JSON.stringify(body)
        });
        if (res.status !== 200) {
            return {status: res.status, instList: all};
        }
        const j = await res.json().catch(() => ({}));
        const items = (j.data && j.data.instList) || [];
        if (items.length === 0) break;
        all.push(...items);
        offset += 1;
        if (offset > 30) break;  // safety cap against a runaway loop
        await new Promise(r => setTimeout(r, 350));
    }
    return {status: 200, instList: all};
}
"""


def extract_goldenpi_rating(sorted_credit_rating):
    if not sorted_credit_rating:
        return ""
    for entry in sorted_credit_rating:
        for value in entry.values():
            m = RATING_TOKEN_RE.search(str(value))
            if m:
                return m.group(1).upper()
    return ""


def fetch_goldenpi():
    rows = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Skipping GoldenPi: install with `pip install playwright` and `playwright install chromium`")
        return rows

    _ensure_playwright_chromium()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            page.goto("https://goldenpi.com/", wait_until="networkidle", timeout=60000)
            result = page.evaluate(GPI_FETCH_JS)
            browser.close()

        if result.get("status") != 200:
            print(f"Error fetching GoldenPi data: HTTP {result.get('status')}")
            return rows

        seen = set()
        for item in result.get("instList", []):
            key = item.get("gpiId") or item.get("isin")
            if key in seen:
                continue
            seen.add(key)

            ytm = item.get("ytm")
            if ytm is None:
                ytm = item.get("ytmc")
            rows.append({
                "OBPP": "GoldenPi",
                "ISIN": item.get("isin") or "",
                "Issuer": item.get("name", ""),
                "YTM (%)": ytm,
                "Rating": extract_goldenpi_rating(item.get("sortedCreditRating")),
                "Tenure (Months)": item.get("tenureMonth", ""),
                "Face Value": item.get("stagFaceValue", ""),
                "Minimum Investment Amount": item.get("settlementAmount", ""),
            })
        print(f"GoldenPi: {len(rows)} rows")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error fetching GoldenPi data: {e}")
    return rows


# ---------------------------------------------------------------------------
# TheFixedIncome (site is behind AWS WAF bot-challenge; needs a real browser
# to pass the challenge, then uses the page's own fetch() for the rest)
# ---------------------------------------------------------------------------
TFI_BASE = "https://www.thefixedincome.com"
TFI_LIST_JS = """
async (url) => {
    const res = await fetch(url, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
    return await res.text();
}
"""
TFI_FETCH_MANY_JS = """
async (urls) => {
    const results = await Promise.all(urls.map(async (u) => {
        try {
            const res = await fetch(u);
            return {url: u, text: await res.text()};
        } catch (e) {
            return {url: u, text: ''};
        }
    }));
    return results;
}
"""


def parse_tfi_ymd_tenure(text):
    m = re.search(r"(?:(\d+)\s*y)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*d)?", text, re.I)
    if not m:
        return ""
    years = int(m.group(1)) if m.group(1) else 0
    months = int(m.group(2)) if m.group(2) else 0
    return years * 12 + months


def parse_tfi_listing_html(html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for li in soup.select("ul.offer-grad > li"):
        a = li.select_one("h3 a")
        if not a:
            continue
        href = a.get("href", "")
        title = a.get("title") or a.get_text(strip=True)

        rating = ""
        strong = li.select_one(".form-group strong")
        if strong:
            rating = strong.get_text(strip=True)

        fields = {}
        for div in li.select(".offer-grad-row > div"):
            b = div.find("b")
            if not b:
                continue
            label = b.get_text(strip=True)
            full = div.get_text(" ", strip=True)
            value = full[len(label):].strip() if full.startswith(label) else full.replace(label, "").strip()
            fields[label] = value

        yield_val = re.sub(r"[^\d.]", "", fields.get("Yield (%)", ""))
        min_investment = re.sub(r"[^\d.]", "", fields.get("Min. Investment", ""))
        tenure_months = parse_tfi_ymd_tenure(fields.get("Tenure", ""))

        cards.append({
            "href": href,
            "title": title,
            "rating": rating,
            "ytm": yield_val,
            "tenure_months": tenure_months,
            "min_investment": min_investment,
        })
    return cards


def clean_tfi_listing_title(title):
    """Strip the leading coupon rate and trailing maturity date from a listing
    card title, e.g. '10.75% NAVI FINSERV LIMITED 31/Dec/2027' -> 'NAVI FINSERV LIMITED'."""
    name = re.sub(r"^\d+(\.\d+)?%\s*", "", title or "")
    name = re.sub(r"\s+(?:\d{1,2}/)?(?:[A-Za-z]{3}/)?\d{4}\s*$", "", name)
    return name.strip()


def parse_tfi_detail_html(html, fallback_title=""):
    from bs4 import BeautifulSoup

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    isin_m = re.search(r"\bIN[A-Z0-9]{10}\b", text)
    issuer_m = re.search(r"Issuer\s*:\s*(.+?)\s*\([^)]*\)\s*Issuer's Type", text)
    fv_m = re.search(r"Face Value\s+([\d,]+\.\d+)", text)

    issuer = issuer_m.group(1).strip() if issuer_m else ""
    if issuer.endswith("..."):
        # The site itself truncates some long issuer names on the detail page;
        # the listing card title is usually untruncated, so prefer it here.
        clean_fallback = clean_tfi_listing_title(fallback_title)
        if clean_fallback and not clean_fallback.endswith("..."):
            issuer = clean_fallback

    return (
        isin_m.group(0) if isin_m else "",
        issuer,
        fv_m.group(1).replace(",", "") if fv_m else "",
    )


def fetch_thefixedincome():
    rows = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Skipping TheFixedIncome: install with `pip install playwright beautifulsoup4` "
              "and `playwright install chromium`")
        return rows

    _ensure_playwright_chromium()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
            page.goto(f"{TFI_BASE}/products", wait_until="networkidle", timeout=60000)

            list_url_tmpl = (
                f"{TFI_BASE}/productlistajax?list_type=grad&product_type=0&page={{page}}"
                "&search=&yieldSort=&url_filter=&maturityYearmax=49"
            )
            first_html = page.evaluate(TFI_LIST_JS, list_url_tmpl.format(page=1))
            if "403 Forbidden" in first_html or len(first_html) < 500:
                print("TheFixedIncome: listing endpoint returned an error (likely rate-limited), retrying once after a pause...")
                time.sleep(30)
                first_html = page.evaluate(TFI_LIST_JS, list_url_tmpl.format(page=1))
                if "403 Forbidden" in first_html or len(first_html) < 500:
                    print("TheFixedIncome: still blocked after retry, skipping this run")
                    browser.close()
                    return rows

            total_m = re.search(r"Total Result:.*?(\d+)", first_html, re.S)
            total = int(total_m.group(1)) if total_m else 0

            cards = parse_tfi_listing_html(first_html)
            per_page = len(cards) or 20
            total_pages = -(-total // per_page) if total else 1

            for pg in range(2, total_pages + 1):
                time.sleep(1.5)
                html = page.evaluate(TFI_LIST_JS, list_url_tmpl.format(page=pg))
                cards += parse_tfi_listing_html(html)

            hrefs = [c["href"] for c in cards if c["href"]]
            detail_map = {}
            batch_size = 8
            for i in range(0, len(hrefs), batch_size):
                batch = hrefs[i:i + batch_size]
                results = page.evaluate(TFI_FETCH_MANY_JS, batch)
                for r in results:
                    detail_map[r["url"]] = r["text"]
                time.sleep(1.5)

            for c in cards:
                isin, issuer, face_value = "", "", ""
                detail_html = detail_map.get(c["href"], "")
                if detail_html:
                    isin, issuer, face_value = parse_tfi_detail_html(detail_html, fallback_title=c["title"])
                clean_fallback = clean_tfi_listing_title(c["title"])
                rows.append({
                    "OBPP": "TheFixedIncome",
                    "ISIN": isin,
                    "Issuer": issuer or clean_fallback,
                    "YTM (%)": c["ytm"],
                    "Rating": c["rating"],
                    "Tenure (Months)": c["tenure_months"],
                    "Face Value": face_value,
                    "Minimum Investment Amount": c["min_investment"],
                })
            browser.close()
        print(f"TheFixedIncome: {len(rows)} rows")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error fetching TheFixedIncome data: {e}")
    return rows


def fetch_stable():
    url = "https://broking-api.stablebonds.in/v1/collection/bonds_all_bonds_new_user"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Origin": "https://stablebonds.in",
        "Referer": "https://stablebonds.in/",
    }
    rows = []
    try:
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        raw = resp.content
        text = clean_text(safe_decode(raw))

        text_matches = list(ISIN_RE_TEXT.finditer(text))
        byte_matches = list(ISIN_RE_BYTES.finditer(raw))

        seen = set()
        for i, tm in enumerate(text_matches):
            isin = tm.group(0)
            if isin in seen:
                continue
            seen.add(isin)

            prev_text_end = text_matches[i - 1].end() if i > 0 else 0
            big_window = text[prev_text_end:tm.end() + 50]
            small_window = text[max(prev_text_end, tm.start() - 400):tm.end() + 20]

            ytm = ""
            min_investment = ""
            if i < len(byte_matches):
                bm = byte_matches[i]
                prev_byte_end = byte_matches[i - 1].end() if i > 0 else 0
                byte_block = raw[prev_byte_end:bm.end()]
                ytm = extract_ytm_and_min_investment(byte_block)
                min_investment = extract_min_investment_after_isin(raw, bm.end())

            rows.append({
                "OBPP": "Stable",
                "ISIN": isin,
                "Issuer": extract_clean_issuer(big_window),
                "YTM (%)": ytm,
                "Rating": extract_rating(big_window),
                "Tenure (Months)": extract_tenure_months(small_window),
                "Face Value": extract_face_value(small_window),
                "Minimum Investment Amount": min_investment,
            })
        print(f"Stable: {len(rows)} rows")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Stable data: {e}")
    return rows


# ---------------------------------------------------------------------------
# Registry consumed by the Streamlit app: platform name -> fetch function.
# Order here determines the order platforms are fetched / shown as options.
# ---------------------------------------------------------------------------
FETCHERS = {
    "Aspero": fetch_aspero,
    "Smest": fetch_smest,
    "Wintwealth": fetch_wintwealth,
    "Altifi": fetch_altifi,
    "Stable": fetch_stable,
    "IndiaBonds": fetch_indiabonds,
    "BondsIndia": fetch_bondsindia,
    "Jiraaf": fetch_jiraaf,
    "GoldenPi": fetch_goldenpi,
    "TheFixedIncome": fetch_thefixedincome,
}

# ---------------------------------------------------------------------------
# How much to trust each platform's numbers. This reflects how the data is
# sourced, not how complete it is — a platform can be "High" confidence and
# still be missing a field (e.g. Face Value) because the source API simply
# doesn't expose it.
#   High     - clean, direct fields from an official-style JSON API.
#   Good     - mostly direct fields, but with a documented caveat (a value
#              that's inferred/parsed rather than a literal API field, or a
#              filter that silently narrows the result set).
#   Reverify - the source has no public API/schema (reverse-engineered
#              binary format, or an HTML scrape of a WAF-protected site).
#              Spot-checked accurate at build time, but fragile to upstream
#              changes and worth a manual check before relying on it.
# ---------------------------------------------------------------------------
CONFIDENCE = {
    "Aspero": ("High", "Official JSON API; every field (incl. Face Value, Min Investment) is a direct field."),
    "Wintwealth": ("High", "Official JSON API; every field is direct or a one-line computation from direct fields."),
    "Altifi": ("High", "Official JSON API; direct fields. Face Value isn't exposed by their API, so it's left blank rather than guessed."),
    "BondsIndia": ("High", "Official JSON API; every field, including Face Value and Min Investment, is a direct field."),
    "Jiraaf": ("High", "Public JSON API; every field is direct except Tenure (computed from their own total_days field). Face Value isn't exposed, left blank."),
    "GoldenPi": ("High", "Direct JSON fields via a browser-minted auth token. YTM falls back to their indicative ytmc field for a few not-yet-listed IPO tranches."),
    "IndiaBonds": ("Good", "Direct fields, but Face Value needs a second per-ISIN call, and Minimum Investment relies on their `price` field being verified equal to total settlement amount (checked on a few bonds, not all)."),
    "Smest": ("Good", "ISIN/YTM are direct; Issuer and Rating are parsed out of compound text fields. Minimum Investment uses their `minimum_quantity` field as-is, unverified. Bonds with tenure over 36 months are filtered out entirely."),
    "Stable": ("Reverify", "Their API returns undocumented raw binary with no public schema — every field is reverse-engineered from byte patterns. Spot-checked exact on 3 live bonds, but Rating/Tenure are blank on a handful of rows, and an upstream format change could silently break this."),
    "TheFixedIncome": ("Reverify", "Site blocks scripted access; data is scraped from rendered HTML via a headless browser. Accurate when it works, but fragile to markup changes and can be temporarily rate-limited."),
}
