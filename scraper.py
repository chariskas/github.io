# -*- coding: utf-8 -*-
"""
scraper.py — Μηχανή δεδομένων για το site "Πλειστηριασμοί Κύπρου"
Τρέχει αυτόματα στο GitHub Actions κάθε πρωί. Δεν χρειάζεται να το αγγίξεις.

Τι κάνει:
  1. Τραβά όλους τους επερχόμενους πλειστηριασμούς από το eauction-cy.com
  2. Ενημερώνει το ιστορικό κάθε ακινήτου (data/history.json)
  3. Γράφει τα δεδομένα της σελίδας (data/auctions.json)
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

BASE = "https://www.eauction-cy.com"
LIST_URL = BASE + "/Home/HlektronikoiPleistiriasmoi"
SEARCH_URL = BASE + "/Home/HomeListAuctions"  # POST (DevTools capture 07/07/2026)

DATA_DIR = "data"
AUCTIONS_PATH = os.path.join(DATA_DIR, "auctions.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

# SEO: συμπλήρωσε το τελικό URL της σελίδας σου μόλις ενεργοποιηθεί το Pages,
# π.χ. "https://username.github.io/auctions-cy" (χωρίς / στο τέλος).
# Μέχρι τότε, sitemap και hreflang απλώς παραλείπονται — όλα τα άλλα δουλεύουν.
SITE_URL = ""

MAX_PAGES = 100
DELAY_SEC = 1.5
TIMEOUT = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}


def build_payload(page=1, date_from=None):
    if date_from is None:
        date_from = datetime.now().strftime("%d/%m/%Y")
    return {
        "auctionCode": "", "auctionDateFrom": date_from, "auctionDateTo": "",
        "auctionCreationDateFrom": "", "auctionCreationDateTo": "",
        "AuctionStatusId": None, "AuctionSubTypeId": "",
        "extendedFilter1": "", "extendedFilter2": "", "hastenerName": "",
        "lang": "el-GR", "notApprovedForeignBidderId": "",
        "offerValueFrom": "", "offerValueTo": "",
        "pageNumber": str(page), "selectedCountryNumericCode": "0",
        "sortAscending": "true", "sortingFieldId": "1",
    }


def fetch_page(session, page=1):
    # Το eauction χρησιμοποιεί GET με παραμέτρους στο URL (επιβεβαιωμένο 16/07/2026)
    params = {"sortAsc": "True", "sortId": "1", "page": str(page)}
    headers = dict(HEADERS)
    headers["Referer"] = LIST_URL
    r = session.get(LIST_URL, params=params, headers=headers, timeout=TIMEOUT)
    # 428/403/503 = ο server θέλει session/cookies — δοκίμασε ξανά μετά από warm-up
    if r.status_code in (428, 403, 503):
        time.sleep(2)
        session.get(BASE + "/", headers=HEADERS, timeout=TIMEOUT)
        time.sleep(1)
        r = session.get(LIST_URL, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _clean(s):
    if not s:
        return None
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"\s+", " ", s).strip() or None


def parse_price(text):
    if not text:
        return None
    m = re.search(r"([\d\.\,]+)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_date(text):
    if not text:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_listings(html):
    soup = BeautifulSoup(html, "html.parser")
    # Κάθε ακίνητο = div.AList-BoxContainer (πραγματική δομή eauction, 16/07/2026)
    cards = soup.select("div.AList-BoxContainer")
    out, seen = [], set()
    for card in cards:
        text = _clean(card.get_text(" ", strip=True)) or ""
        mcode = re.search(r"Μοναδικός\s+Κωδικός:\s*([A-Z0-9]+(?:-\d+)?)", text)
        if not mcode or mcode.group(1) in seen:
            continue
        code = mcode.group(1)
        seen.add(code)

        # Βοηθητικό: βρίσκει το κείμενο δίπλα σε έναν τίτλο κελιού
        def cell(title_kw):
            for t in card.select("div.AList-BoxMainCellTitle"):
                if title_kw in (t.get_text() or ""):
                    # Το επόμενο αδερφάκι κρατά την τιμή (BlueBold ή απλό)
                    sib = t.find_next_sibling("div")
                    while sib is not None:
                        val = _clean(sib.get_text(" ", strip=True))
                        if val:
                            return val
                        sib = sib.find_next_sibling("div")
            return None

        # Regex fallback από όλο το κείμενο (αν αλλάξει η δομή)
        def field(label):
            m = re.search(label + r"\s*:?\s*(.+?)(?:$|"
                          r"Επιφυλασσ|Ημ/νία|Ημερομην|Επαρχ|Δήμος|"
                          r"Είδος|Ενυπόθηκ|Κατάστασ|Μοναδικ|"
                          r"Περισσότερα|Μέρος\s+του)", text)
            return _clean(m.group(1)) if m else None

        # Είδος: ψάξε το BlueBold ΜΕΣΑ στο κελί που έχει τίτλο «Είδος»
        typ = None
        for t in card.select("div.AList-BoxMainCellTitle"):
            if "Είδος" in (t.get_text() or ""):
                parent = t.parent
                if parent:
                    bb = parent.select_one("div.AList-BoxTextBlueBold")
                    if bb:
                        typ = _clean(bb.get_text())
                break
        typ = typ or cell("Είδος") or field(r"Είδος")

        # Διεύθυνση/Δήμος
        addr = None
        ad = card.select_one("div.AList-BoxTextAddress")
        if ad:
            addr = _clean(ad.get_text(" ", strip=True))

        district = field(r"Επαρχία")
        municipality = field(r"Δήμος\s*/\s*Ενορία\s*/\s*Κοινότητα") or addr

        link = card.find("a", href=True)
        url = None
        if link:
            url = BASE + link["href"] if link["href"].startswith("/") else link["href"]

        out.append({
            "code": code,
            "status": cell("Κατάσταση") or field(r"Κατάστασ(?:η|ης)"),
            "price": parse_price(cell("Επιφυλασσόμενη") or field(r"Επιφυλασσόμενη\s+Τιμή") or ""),
            "auction_date": parse_date(cell("Ημ/νία Διεξαγωγής") or field(r"Ημ/νία\s+Διεξαγωγής") or ""),
            "district": district,
            "municipality": municipality,
            "type": typ,
            "lender": cell("Ενυπόθηκος") or field(r"Ενυπόθηκος\s+Δανειστής"),
            "url": url or LIST_URL,
        })
    return out


def find_total_pages(html):
    # Δοκίμασε διάφορες μορφές που μπορεί να έχει το eauction
    for pat in (r"[Σσ]ελίδα\s+\d+\s+από\s+(\d+)",
                r"[Σσ]ελίδα\s+\d+\s*/\s*(\d+)",
                r"page=(\d+)['\"]?\s*>\s*[Ττ]ελευτα"):
        m = re.search(pat, html)
        if m:
            return int(m.group(1))
    # Αλλιώς υπολόγισε από το πλήθος αποτελεσμάτων (π.χ. "Βρέθηκαν 415")
    mt = re.search(r"Βρέθηκαν\s+(\d+)\s+πλειστηριασμ", html)
    if mt:
        import math
        return max(1, math.ceil(int(mt.group(1)) / 20))
    return 1


def scrape_all():
    session = requests.Session()
    session.headers.update(HEADERS)
    # Warm-up: επισκέψου πρώτα την αρχική για να πάρεις cookies/session
    try:
        session.get(BASE + "/", headers=HEADERS, timeout=TIMEOUT)
        time.sleep(1)
        session.get(LIST_URL, headers=HEADERS, timeout=TIMEOUT)
        time.sleep(1)
    except Exception:
        pass
    first = fetch_page(session, 1)
    pages = min(find_total_pages(first), MAX_PAGES)
    rows = parse_listings(first)
    print(f"Σελίδα 1/{pages}: {len(rows)} εγγραφές")
    for p in range(2, pages + 1):
        time.sleep(DELAY_SEC)
        try:
            batch = parse_listings(fetch_page(session, p))
            rows += batch
            print(f"Σελίδα {p}/{pages}: +{len(batch)}")
        except Exception as e:
            print(f"  ! Σελίδα {p}: {e}", file=sys.stderr)
    return rows


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")) if s else ""


def fmt_eur(n):
    if n is None:
        return "—"
    return "€" + f"{round(n):,}".replace(",", ".")


def fmt_dmy(iso):
    if not iso:
        return "—"
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def render_seo(rows, updated_iso, district_links=None, seo_urls=None):
    """Γράφει τους πλειστηριασμούς ΜΕΣΑ στο index.html (crawlable HTML),
    μαζί με JSON-LD, hreflang και sitemap/robots αν έχει οριστεί SITE_URL."""
    if not os.path.exists("index.html"):
        print("Δεν βρέθηκε index.html — παράλειψη SEO render.", file=sys.stderr)
        return

    with open("index.html", encoding="utf-8") as f:
        html = f.read()

    # Social πλατφόρμες απαιτούν ΑΠΟΛΥΤΟ URL για og:image
    if SITE_URL:
        html = html.replace('content="og-image.png"',
                             f'content="{SITE_URL}/og-image.png"')

    upcoming = sorted([r for r in rows if r.get("auction_date")],
                      key=lambda r: r["auction_date"])[:60]

    items_html = []
    for r in upcoming:
        loc = ", ".join(x for x in [r.get("municipality"), r.get("district")] if x)
        ai_sum = (r.get("ai") or {}).get("summary", {}).get("el", "")
        typ = r.get("type") or "Ακίνητο"
        init = esc(typ[:2].upper())
        per = (f'<span class="per">{fmt_eur(r["price"] / r["sqm"])}/m²</span>'
               if r.get("sqm") and r.get("price") else "")
        items_html.append(
            f'<a class="card" href="{esc(r["url"])}" target="_blank" rel="noopener">'
            f'<div class="banner"><span class="initials">{init}</span>'
            f'<span class="type">{esc(typ)}</span></div>'
            f'<div class="body"><div class="price-row">'
            f'<span class="price">{fmt_eur(r.get("price"))}</span>{per}</div>'
            f'<div class="loc">📍 {esc(loc)}</div>'
            f'<div class="meta"><span>{esc(r.get("lender") or "")}</span>'
            f'<span>{fmt_dmy(r.get("auction_date"))}</span></div>'
            + (f'<p class="ai-summary">{esc(ai_sum)}</p>' if ai_sum else "")
            + '</div></a>')
    ssr = ("\n".join(items_html)
           or '<div class="state"><h2>Φόρτωση…</h2></div>')

    html = re.sub(r"<!--SSR-->.*?<!--/SSR-->",
                  "<!--SSR-->" + ssr + "<!--/SSR-->", html, flags=re.S)

    # JSON-LD: ItemList με τους πλησιέστερους πλειστηριασμούς
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Organization",
                "@id": (SITE_URL + "/#org") if SITE_URL else "#org",
                "name": "Cyprus Auctions",
                "description": "Ανεξάρτητο ευρετήριο πλειστηριασμών ακινήτων στην Κύπρο "
                               "με AI εκτίμηση, ιστορικό τιμών και ειδοποιήσεις.",
                **({"url": SITE_URL + "/"} if SITE_URL else {}),
            },
            {
                "@type": "WebSite",
                "@id": (SITE_URL + "/#website") if SITE_URL else "#website",
                "name": "Cyprus Auctions",
                "inLanguage": ["el", "en", "ru", "he"],
                **({"url": SITE_URL + "/",
                    "publisher": {"@id": SITE_URL + "/#org"}} if SITE_URL else {}),
            },
            {
                "@type": "ItemList",
                "name": "Πλειστηριασμοί ακινήτων Κύπρου",
                "numberOfItems": len(rows),
                "itemListElement": [{
                    "@type": "ListItem", "position": i + 1,
                    "item": {
                        "@type": "Product",
                        "name": f'{r.get("type") or "Ακίνητο"} — '
                                f'{r.get("municipality") or ""} {r.get("district") or ""}'.strip(),
                        "url": r["url"],
                        **({"offers": {"@type": "Offer", "price": int(r["price"]),
                                       "priceCurrency": "EUR"}} if r.get("price") else {}),
                    }} for i, r in enumerate(upcoming[:25])],
            },
        ],
    }
    html = re.sub(r"<!--LD-->.*?<!--/LD-->",
                  "<!--LD--><script type=\"application/ld+json\">"
                  + json.dumps(ld, ensure_ascii=False)
                  + "</script><!--/LD-->", html, flags=re.S)

    # hreflang + canonical (μόνο αν ξέρουμε το URL)
    head = ""
    if SITE_URL:
        head = f'<link rel="canonical" href="{SITE_URL}/">' + "".join(
            f'<link rel="alternate" hreflang="{l}" href="{SITE_URL}/?lang={l}">'
            for l in ("el", "en", "ru", "he")) + \
            f'<link rel="alternate" hreflang="x-default" href="{SITE_URL}/">'
    html = re.sub(r"<!--HREFLANG-->.*?<!--/HREFLANG-->",
                  "<!--HREFLANG-->" + head + "<!--/HREFLANG-->", html, flags=re.S)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"SEO: {len(upcoming)} πλειστηριασμοί γράφτηκαν στο index.html")

    # Internal links προς τις σελίδες επαρχιών (crawlable, μέσα στο <!--DLINKS-->)
    if district_links:
        links = "".join(
            f'<a href="d/{slug}.html">Πλειστηριασμοί {gen} ({n})</a>'
            for slug, gen, n in district_links if n)
        block = f'<div class="wrap dlinks"><!--DL-->{links}<!--/DL--></div>' if links else ""
        with open("index.html", encoding="utf-8") as f:
            h2 = f.read()
        h2 = re.sub(r"<!--DLINKS-->.*?<!--/DLINKS-->",
                    "<!--DLINKS-->" + block + "<!--/DLINKS-->", h2, flags=re.S)
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(h2)

    if SITE_URL:
        today = updated_iso[:10]
        urls = ([f"{SITE_URL}/", f"{SITE_URL}/guide.html", f"{SITE_URL}/report.html", f"{SITE_URL}/partners.html", f"{SITE_URL}/legal.html"]
                + [f"{SITE_URL}/?lang={l}" for l in ("en", "ru", "he")]
                + [f"{SITE_URL}/guide.html?lang={l}" for l in ("en", "ru", "he")]
                + (seo_urls or []))
        sm = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
              + "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod>"
                        f"<changefreq>daily</changefreq></url>\n" for u in urls)
              + "</urlset>\n")
        with open("sitemap.xml", "w", encoding="utf-8") as f:
            f.write(sm)
        with open("robots.txt", "w", encoding="utf-8") as f:
            f.write(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
        print(f"SEO: sitemap.xml ({len(urls)} URLs) και robots.txt ενημερώθηκαν")
    else:
        print("SEO: όρισε το SITE_URL στο scraper.py για sitemap/hreflang")


# ==================== ΛΕΠΤΟΜΕΡΕΙΕΣ ΑΝΑ ΑΚΙΝΗΤΟ ====================
# Επισκέπτεται τη σελίδα «Περισσότερα» κάθε ΝΕΟΥ ακινήτου (μία φορά ανά κωδικό)
# και εξάγει τ.μ., αρ. εγγραφής, τεμάχιο, μερίδιο και περιγραφή.

DETAILS_PER_RUN = 25   # ευγενικό όριο ανά ημέρα· τα υπόλοιπα την επόμενη

SQM_RE = re.compile(
    r"(?:Έκταση|Εμβαδόν|Επιφάνεια|Εμβαδό)[^\d]{0,40}([\d\.,]+)\s*(?:τ\.?\s*μ|m2|m²)",
    re.IGNORECASE)

# Λέξεις-κλειδιά κατάστασης/χαρακτηριστικών (θετικά & αρνητικά) — accent-insensitive
CONDITION_NEG = [
    ("ημιτελ", "ημιτελές"), ("υπο ανεγερσ", "υπό ανέγερση"),
    ("υπο κατασκευ", "υπό κατασκευή"), ("ερειπ", "ερειπωμένο"),
    ("χρηζει ανακαιν", "χρήζει ανακαίνισης"), ("κατεδαφιστε", "προς κατεδάφιση"),
    ("εγκαταλελειμμ", "εγκαταλελειμμένο"),
]
CONDITION_POS = [
    ("ανακαινισμ", "ανακαινισμένο"), ("πληρως επιπλωμ", "πλήρως επιπλωμένο"),
    ("επιπλωμ", "επιπλωμένο"), ("καινουργ", "καινούργιο"), ("νεοδμητ", "νεόδμητο"),
    ("θεα θαλασσ", "θέα θάλασσα"), ("γωνιακ", "γωνιακό"),
    ("πισινα", "πισίνα"), ("κηπο", "κήπος"),
]
RISK_KEYWORDS = [
    ("επικαρπ", "επικαρπία"), ("δουλεια", "δουλεία"),
    ("ενοικι", "ενοικιασμένο"), ("μισθωμ", "μισθωμένο"), ("μισθωσ", "μισθωμένο"),
    ("κατεχ", "κατεχόμενο"), ("χωρις προσβαση", "χωρίς πρόσβαση"),
    ("εγκλωβισμ", "εγκλωβισμένο"), ("δεσμευσ", "δέσμευση"),
]


def _accent_strip(s):
    import unicodedata as _u
    return "".join(c for c in _u.normalize("NFD", (s or "").lower())
                   if not _u.combining(c))


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    text = _clean(soup.get_text(" ", strip=True)) or ""
    low = _accent_strip(text)
    d = {}

    m = SQM_RE.search(text)
    if m:
        try:
            d["sqm"] = float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            pass

    for key, pat in (("reg_no", r"Αρ\.?\s*[ΕE]?γγραφής\s*:?\s*([\d/]+[\w/−–-]*)"),
                     ("plot", r"Τεμάχιο\s*:?\s*(\d+)"),
                     ("sheet", r"Φύλλο[/\s]*Σχέδιο\s*:?\s*([\d/]+)"),
                     ("share", r"Μερίδιο\s*:?\s*([\d/]+)"),
                     ("block", r"Τμήμα\s*:?\s*(\d+)")):
        mm = re.search(pat, text)
        if mm:
            d[key] = mm.group(1)

    # Όροφος
    mfloor = re.search(r"(\d+)ος?\s+όροφος", text) or \
        re.search(r"όροφος\s*:?\s*(\d+)", text, re.IGNORECASE)
    if mfloor:
        d["floor"] = int(mfloor.group(1))
    elif "ισογει" in low:
        d["floor"] = 0

    # Υπνοδωμάτια
    mbed = re.search(r"(\d+)\s*(?:υπνοδωμ|υ/δ|δωματ)", text, re.IGNORECASE)
    if mbed:
        d["bedrooms"] = int(mbed.group(1))

    # Έτος κατασκευής
    myear = re.search(r"(?:έτος\s+κατασκευής|κατασκευάστηκε|ανεγέρθη)\D{0,10}(\d{4})",
                      text, re.IGNORECASE)
    if myear and 1950 <= int(myear.group(1)) <= 2030:
        d["year_built"] = int(myear.group(1))

    # Πολεοδομική ζώνη
    mzone = re.search(r"(?:πολεοδομική\s+)?ζώνη\s*:?\s*([Α-ΩA-Z][\dα-ω/]*)",
                      text, re.IGNORECASE)
    if mzone:
        d["zone"] = mzone.group(1)

    # Κατάσταση & χαρακτηριστικά
    neg = [label for kw, label in CONDITION_NEG if kw in low]
    pos = [label for kw, label in CONDITION_POS if kw in low]
    risks = [label for kw, label in RISK_KEYWORDS if kw in low]
    # μερίδιο < 1/1 = ρίσκο
    if d.get("share") and d["share"] not in ("1/1", "1"):
        risks.append(f"μερίδιο {d['share']}")
    if neg:
        d["condition_neg"] = neg
    if pos:
        d["features"] = pos
    if risks:
        d["risks"] = risks

    mdesc = re.search(
        r"ΠΕΡΙΓΡΑΦΗ\s+ΑΚΙΝΗΤ\w+\s+ΙΔΙΟΚΤΗΣΙΑΣ\s*:?\s*(.{0,600})", text)
    if mdesc:
        d["description"] = _clean(mdesc.group(1))
    d["raw"] = text[:3500]   # πρώτη ύλη για το AI στρώμα
    return d


def enrich_details(rows, history):
    session = requests.Session()
    todo = [r for r in rows
            if "detail" not in history.get(r["code"], {})
            and r.get("url") and r["url"] != LIST_URL][:DETAILS_PER_RUN]
    done = 0
    for r in todo:
        try:
            resp = session.get(r["url"], headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            history[r["code"]]["detail"] = parse_detail(resp.text)
            done += 1
            time.sleep(DELAY_SEC)
        except Exception as e:
            print(f"  ! Λεπτομέρειες {r['code']}: {e}", file=sys.stderr)
    if done:
        print(f"Λεπτομέρειες: +{done} ακίνητα εμπλουτίστηκαν")
    # Πέρασμα των στοιχείων στα δεδομένα της σελίδας
    for r in rows:
        det = history.get(r["code"], {}).get("detail") or {}
        if det.get("sqm"):
            r["sqm"] = det["sqm"]
        if det.get("description"):
            r["description"] = det["description"]
        if det.get("reg_no"):
            r["reg_no"] = det["reg_no"]
        for k in ("floor", "bedrooms", "year_built", "zone",
                  "condition_neg", "features", "risks", "share"):
            if det.get(k):
                r[k] = det[k]


# ==================== AI ΑΝΑΛΥΣΗ ====================
# Τρέχει μόνο για νέα ακίνητα ή όταν αλλάξει η τιμή (cache στο history).
# Χωρίς ANTHROPIC_API_KEY απλώς παραλείπεται — η σελίδα δουλεύει κανονικά.
# Ρύθμιση: GitHub → Settings → Secrets and variables → Actions →
#          New repository secret → όνομα ANTHROPIC_API_KEY

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = "claude-haiku-4-5-20251001"
AI_PER_RUN = 20


def property_class(typ):
    """Κανονικοποιεί το είδος σε κατηγορία για σύγκριση ΟΜΟΕΙΔΩΝ ακινήτων.
    Επιστρέφει: apartment | house | land | commercial | other."""
    import unicodedata as _u
    t = "".join(c for c in _u.normalize("NFD", (typ or "").upper())
                if not _u.combining(c))
    if any(k in t for k in ("ΔΙΑΜΕΡ", "STUDIO", "ΓΚΑΡΣ", "ΡΕΤΙΡΕ", "ΟΡΟΦΟΔΙΑΜ")):
        return "apartment"
    if any(k in t for k in ("ΚΑΤΟΙΚ", "ΟΙΚΙΑ", "ΜΕΖΟΝ", "ΒΙΛΑ", "ΕΠΑΥΛ", "ΣΠΙΤ")):
        return "house"
    if any(k in t for k in ("ΟΙΚΟΠΕΔ", "ΧΩΡΑΦ", "ΑΓΡΟΤΕΜ", "ΤΕΜΑΧ", "ΓΗ", "ΚΤΗΜΑ")):
        return "land"
    if any(k in t for k in ("ΚΑΤΑΣΤ", "ΓΡΑΦΕΙ", "ΑΠΟΘΗΚ", "ΒΙΟΜΗΧ", "ΞΕΝΟΔΟΧ",
                            "ΕΠΑΓΓΕΛΜ", "ΕΜΠΟΡΙΚ")):
        return "commercial"
    return "other"


def district_medians(rows):
    """Διάμεση €/m² ανά επαρχία από τα ΔΙΚΑ ΜΑΣ δεδομένα — τίμια βάση σύγκρισης."""
    import statistics
    per = {}
    for r in rows:
        if r.get("sqm") and r.get("price") and r["sqm"] > 5:
            per.setdefault(r.get("district") or "?", []).append(r["price"] / r["sqm"])
    return {k: round(statistics.median(v)) for k, v in per.items() if len(v) >= 3}


def typed_medians(rows):
    """Διάμεση €/m² ανά (επαρχία, ΚΑΤΗΓΟΡΙΑ ακινήτου) — σύγκριση ομοειδών.
    Πολύ ακριβέστερη: διαμέρισμα με διαμερίσματα, όχι με χωράφια."""
    import statistics
    per = {}
    for r in rows:
        if r.get("sqm") and r.get("price") and r["sqm"] > 5:
            cls = property_class(r.get("type"))
            key = (r.get("district") or "?", cls)
            per.setdefault(key, []).append(r["price"] / r["sqm"])
    # Απαιτούμε 3+ ομοειδή για αξιόπιστη διάμεσο
    return {k: round(statistics.median(v)) for k, v in per.items() if len(v) >= 3}


# Ενδεικτική ακαθάριστη ετήσια απόδοση ενοικίου ανά επαρχία (μεικτή, συντηρητική).
# Χρησιμοποιείται ΜΟΝΟ ως τάξη μεγέθους στο AI, με ρητή επισήμανση ότι είναι εκτίμηση.
# Επίσημη ετήσια τάση τιμών κατοικιών ανά επαρχία (%), από δημόσιες πηγές:
# Κεντρική Τράπεζα Κύπρου (RPPI) & Στατιστική Υπηρεσία (CYSTAT HPI).
# ΕΝΗΜΕΡΩΣΗ: κάθε τρίμηνο βγαίνουν νέα στοιχεία. Άνοιξε:
#   https://www.centralbank.cy/en/publications/residential-property-price-indices
#   ή https://www.cystat.gov.cy (House Price Index) και ενημέρωσε τα νούμερα.
# Είναι λίγα και αλλάζουν σπάνια — 2 λεπτά χειροκίνητα, χωρίς scraping.
# Τελευταία ενημέρωση από χρήστη: Q4 2025 (ετήσια μεταβολή %).
OFFICIAL_TREND = {
    "ΛΕΥΚΩΣΙΑ": 4.5, "ΛΕΜΕΣΟΣ": 8.0, "ΛΑΡΝΑΚΑ": 7.5,
    "ΠΑΦΟΣ": 6.0, "ΑΜΜΟΧΩΣΤΟΣ": 5.5,
}
OFFICIAL_TREND_ASOF = "Q4 2025"


RENTAL_YIELD_HINT = {
    "ΛΕΥΚΩΣΙΑ": 5.0, "ΛΕΜΕΣΟΣ": 4.5, "ΛΑΡΝΑΚΑ": 5.5,
    "ΠΑΦΟΣ": 5.5, "ΑΜΜΟΧΩΣΤΟΣ": 6.0, "ΚΕΡΥΝΕΙΑ": 5.0,
}


def market_context(rows, history):
    """Χτίζει πλούσιο context αγοράς που ΔΥΝΑΜΩΝΕΙ όσο μεγαλώνει το ιστορικό:
    διάμεσες τιμές, τάση περιοχής (τελευταίες vs παλαιότερες), και
    πραγματικές κατακυρώσεις όπου τις έχουμε καταγράψει."""
    import statistics
    med = district_medians(rows)
    typed = typed_medians(rows)

    # Τάση: σύγκριση διάμεσης €/m² ακινήτων που πρωτοεμφανίστηκαν τον τελευταίο
    # μήνα vs παλαιότερα, ανά επαρχία. Θετικό = ανοδική αγορά.
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    recent, older = {}, {}
    for r in rows:
        if not (r.get("sqm") and r.get("price") and r["sqm"] > 5):
            continue
        d = r.get("district") or "?"
        (recent if (r.get("first_seen") or "") >= cutoff else older) \
            .setdefault(d, []).append(r["price"] / r["sqm"])
    trend = {}
    for d in med:
        if len(recent.get(d, [])) >= 2 and len(older.get(d, [])) >= 2:
            rm, om = statistics.median(recent[d]), statistics.median(older[d])
            if om:
                trend[d] = round((rm / om - 1) * 100)

    # Πραγματικές κατακυρώσεις: όσα ακίνητα «εξαφανίστηκαν» ενώ ο τελευταίος
    # γνωστός status τους ήταν ενεργός — proxy για πώληση. Καταγράφουμε €/m².
    sold = {}
    live_codes = {r["code"] for r in rows}
    for code, h in history.items():
        if code in live_codes:
            continue
        snaps = h.get("snaps", [])
        det = h.get("detail") or {}
        if snaps and det.get("sqm"):
            last = snaps[-1]
            if last.get("p") and det["sqm"] > 5 and \
                    (last.get("s") or "").find("Ματαιωμ") < 0:
                # χονδρική επαρχία από ιστορικό αν υπάρχει
                sold.setdefault(h.get("district") or det.get("district") or "?", []) \
                    .append(last["p"] / det["sqm"])
    sold_med = {k: round(statistics.median(v))
                for k, v in sold.items() if len(v) >= 3}

    # ---- ΡΕΥΣΤΟΤΗΤΑ ΠΕΡΙΟΧΗΣ (moat: χτίζεται μόνο με ιστορικό) ----
    # Μέσος χρόνος (ημέρες) από την πρώτη εμφάνιση ως την «εξαφάνιση» (πιθανή πώληση),
    # ανά επαρχία. Χαμηλός = ρευστή αγορά, γρήγορες πωλήσεις.
    from datetime import datetime as _dt
    liquidity = {}
    liq_raw = {}
    for code, h in history.items():
        if code in live_codes:
            continue
        fs, snaps = h.get("first_seen"), h.get("snaps", [])
        if fs and snaps:
            try:
                d0 = _dt.strptime(fs[:10], "%Y-%m-%d")
                d1 = _dt.strptime((snaps[-1].get("d") or fs)[:10], "%Y-%m-%d")
                days = (d1 - d0).days
                if 0 <= days <= 1500:
                    liq_raw.setdefault(h.get("district") or "?", []).append(days)
            except (ValueError, TypeError):
                pass
    liquidity = {k: round(statistics.median(v))
                 for k, v in liq_raw.items() if len(v) >= 3}

    # ---- ΤΥΠΙΚΗ ΑΠΟΚΛΙΣΗ €/m² ανά επαρχία (για ανίχνευση outlier) ----
    stdev = {}
    per = {}
    for r in rows:
        if r.get("sqm") and r.get("price") and r["sqm"] > 5:
            per.setdefault(r.get("district") or "?", []).append(r["price"] / r["sqm"])
    for d, vals in per.items():
        if len(vals) >= 5:
            try:
                stdev[d] = round(statistics.stdev(vals))
            except statistics.StatisticsError:
                pass

    return {"med": med, "typed": typed, "trend": trend, "sold_med": sold_med,
            "liquidity": liquidity, "stdev": stdev}


def history_signals(r, history):
    """Δείκτες βασισμένοι στο ΙΣΤΟΡΙΚΟ του συγκεκριμένου ακινήτου — το πιο
    μη-αντιγράψιμο κομμάτι, γιατί απαιτεί δεδομένα που μαζεύονται με τον χρόνο."""
    from datetime import datetime as _dt
    h = history.get(r["code"]) or {}
    snaps = h.get("snaps", [])
    sig = {}
    if len(snaps) >= 2:
        # Ταχύτητα πτώσης: % πτώση ανά μήνα (30% σε 2 μήνες ≠ 30% σε 2 χρόνια)
        first, last = snaps[0], snaps[-1]
        try:
            p0, p1 = first.get("p"), last.get("p")
            d0 = _dt.strptime((first.get("d") or "")[:10], "%Y-%m-%d")
            d1 = _dt.strptime((last.get("d") or "")[:10], "%Y-%m-%d")
            months = max((d1 - d0).days / 30.0, 0.5)
            if p0 and p1 and p1 < p0:
                total_drop = (1 - p1 / p0) * 100
                sig["συνολική_πτώση_%"] = round(total_drop)
                sig["ταχύτητα_πτώσης_%_ανά_μήνα"] = round(total_drop / months, 1)
                sig["μήνες_στην_αγορά"] = round(months)
        except (ValueError, TypeError):
            pass
    # Αριθμός μειώσεων τιμής (πόσες φορές άλλαξε προς τα κάτω)
    drops = sum(1 for i in range(1, len(snaps))
                if snaps[i].get("p") and snaps[i-1].get("p")
                and snaps[i]["p"] < snaps[i-1]["p"])
    if drops:
        sig["φορές_μείωσης_τιμής"] = drops
    return sig


def unsold_reasons(r, ctx):
    """Πιθανοί λόγοι που ένα ακίνητο δεν πουλήθηκε (μένει άγονο/επαναληπτικό).
    ΑΥΣΤΗΡΑ βάσει δεδομένων που έχουμε — υποθέσεις, όχι βεβαιότητες.
    Επιστρέφεται μόνο για ακίνητα με ένδειξη ότι δυσκολεύονται να πουληθούν."""
    relist = r.get("relistings") or 0
    # Μόνο αν υπάρχει ένδειξη δυσκολίας (επαναληπτικός ή μεγάλη παραμονή)
    if relist < 1:
        return []
    reasons = []
    med, typed = ctx.get("med", {}), ctx.get("typed", {})
    d = r.get("district")
    price, sqm = r.get("price"), r.get("sqm")

    # 1) Τιμή πάνω από την αγορά
    if price and sqm and sqm > 5:
        cls = property_class(r.get("type"))
        base = typed.get((d, cls)) or med.get(d)
        if base:
            dev = (price / sqm / base - 1) * 100
            if dev > 12:
                reasons.append(f"Η τιμή ήταν ~{round(dev)}% πάνω από τη μέση της περιοχής "
                               "για ανάλογα ακίνητα.")

    # 2) Σημαίες ρίσκου (μερίδιο, δικαιώματα τρίτων, πρόσβαση)
    risks = r.get("risks") or []
    if risks:
        reasons.append("Πιθανά νομικά/πρακτικά εμπόδια: " + ", ".join(risks[:3]) + ".")

    # 3) Χαμηλή ρευστότητα περιοχής
    liq = ctx.get("liquidity", {}).get(d)
    if liq and liq > 180:
        reasons.append(f"Η περιοχή έχει χαμηλή ρευστότητα (τα ακίνητα εδώ πωλούνται "
                       f"κατά μέσο όρο σε ~{liq} ημέρες).")

    # 4) Αρνητική κατάσταση ακινήτου
    cond = r.get("condition_neg") or []
    if cond:
        reasons.append("Η κατάσταση του ακινήτου (" + ", ".join(cond[:2]) +
                       ") απαιτεί επιπλέον κόστος/εργασίες.")

    # 5) Πολλοί επαναληπτικοί χωρίς πτώση τιμής
    if relist >= 2 and not (r.get("initial_price") and r.get("price")
                            and r["price"] < r["initial_price"]):
        reasons.append(f"Έχει βγει {relist+1} φορές χωρίς ουσιαστική μείωση τιμής — "
                       "ίσως η τιμή εκκίνησης είναι ψηλά για την αγορά.")

    # Fallback: αν δεν βρέθηκε συγκεκριμένος λόγος
    if not reasons and relist >= 1:
        reasons.append("Δεν εμφανίστηκε αγοραστής στους προηγούμενους γύρους — "
                       "συχνά ζήτημα τιμής, θέσης ή κατάστασης. Αξίζει έλεγχος πριν πλειοδοτήσεις.")
    return reasons


def valuation_metrics(r, ctx):
    """Υπολογίζει επαγγελματικούς δείκτες αποτίμησης ως ΓΕΓΟΝΟΤΑ (όχι εκτιμήσεις AI).
    Βασισμένο στις 3 αναγνωρισμένες μεθόδους των εκτιμητών + τον κανόνα 70% για
    distressed/πλειστηριασμούς. Επιστρέφει μόνο ό,τι μπορεί να υπολογιστεί με βεβαιότητα."""
    med, sold_med = ctx["med"], ctx["sold_med"]
    d = r.get("district")
    price, sqm = r.get("price"), r.get("sqm")
    m = {}
    if not (price and sqm and sqm > 5):
        return m

    ppsqm = price / sqm
    # Βάση αγοράς (Sales Comparison) — ΙΕΡΑΡΧΙΑ ΑΚΡΙΒΕΙΑΣ:
    # 1ο: διάμεση ΟΜΟΕΙΔΩΝ ακινήτων στην επαρχία (π.χ. διαμέρισμα με διαμερίσματα)
    # 2ο: πραγματικές κατακυρώσεις  3ο: γενική διάμεση επαρχίας
    typed = ctx.get("typed", {})
    cls = property_class(r.get("type"))
    typed_base = typed.get((d, cls))
    if typed_base:
        base_m2, base_src = typed_base, f"ομοειδή ({cls})"
    elif sold_med.get(d):
        base_m2, base_src = sold_med[d], "κατακυρώσεις"
    elif med.get(d):
        base_m2, base_src = med[d], "τρέχοντες (όλοι οι τύποι)"
    else:
        base_m2, base_src = None, None
    if base_m2:
        # 1) SALES COMPARISON: εκτιμώμενη αγοραία αξία & απόκλιση
        est_market = round(base_m2 * sqm)
        m["εκτιμώμενη_αγοραία_αξία_€_βάσει_comparables"] = est_market
        m["πηγή_βάσης"] = base_src
        m["απόκλιση_τιμής_από_αγορά_%"] = round((ppsqm / base_m2 - 1) * 100)

        # 2) ΚΑΝΟΝΑΣ 70% (distressed/flip): Μέγιστη Λογική Προσφορά.
        #    Cyprus-adjusted: χρησιμοποιούμε 72% (χαμηλότερα κόστη συναλλαγής vs ΗΠΑ,
        #    χωρίς agent commission στην αγορά από πλειστηριασμό). Χωρίς κόστος
        #    επισκευών (άγνωστο) — άρα MAO προ επισκευών.
        mao_70 = round(est_market * 0.72)
        m["μέγιστη_λογική_τιμή_κανόνας_72%_προ_επισκευών_€"] = mao_70
        m["περιθώριο_έναντι_κανόνα_72%_€"] = round(mao_70 - price)
        # Θετικό περιθώριο = αγοράζεις κάτω από το όριο του επενδυτή flip

        # 3) INCOME APPROACH (cap rate) — μόνο για κατοικήσιμα με ένδειξη ενοικίου
        yhint = RENTAL_YIELD_HINT.get(d)
        import unicodedata as _u
        typ = "".join(c for c in _u.normalize("NFD", (r.get("type") or "").upper())
                      if not _u.combining(c))
        residential = any(k in typ for k in ("ΔΙΑΜΕΡ", "ΚΑΤΟΙΚ", "ΟΙΚΙΑ", "ΜΕΖΟΝ", "STUDIO", "ΓΚΑΡΣ"))
        commercial = any(k in typ for k in ("ΚΑΤΑΣΤ", "ΓΡΑΦΕΙ", "ΑΠΟΘΗΚ", "ΒΙΟΜΗΧ", "ΞΕΝΟΔΟΧ"))
        if yhint and (residential or commercial):
            # Εκτιμώμενο ετήσιο ενοίκιο = αγοραία αξία × (yield-hint ως cap proxy)
            est_rent = est_market * (yhint / 100)
            m["εκτιμώμενο_ετήσιο_ενοίκιο_€_ενδεικτικό"] = round(est_rent)
            # Απόδοση στην ΤΙΜΗ ΠΛΕΙΣΤΗΡΙΑΣΜΟΥ (όχι στην αγοραία) — το πραγματικό yield αγοραστή
            m["απόδοση_στην_τιμή_πλειστηριασμού_%"] = round(est_rent / price * 100, 1)
            # GRM (Gross Rent Multiplier) στην τιμή πλειστηριασμού
            m["gross_rent_multiplier"] = round(price / est_rent, 1)
    return m


def build_facts(r, ctx, history=None):
    """Μόνο επαληθεύσιμα γεγονότα — το AI δεν επιτρέπεται να εφεύρει τίποτα."""
    med, trend, sold_med = ctx["med"], ctx["trend"], ctx["sold_med"]
    d = r.get("district")
    f = {
        "είδος": r.get("type"), "περιοχή": r.get("municipality"),
        "επαρχία": d, "επιφυλασσόμενη_τιμή_€": r.get("price"),
        "ημερομηνία_πλειστηριασμού": r.get("auction_date"),
    }
    if r.get("sqm"):
        f["εμβαδόν_m2"] = r["sqm"]
        if r.get("price"):
            f["τιμή_ανά_m2_€"] = round(r["price"] / r["sqm"])
    if med.get(d):
        f["διάμεση_τιμή_m2_επαρχίας_€_τρέχοντες"] = med[d]
    if sold_med.get(d):
        f["διάμεση_τιμή_m2_ΠΡΑΓΜΑΤΙΚΩΝ_κατακυρώσεων_€"] = sold_med[d]
    if d in trend:
        f["τάση_τιμών_επαρχίας_τελ_μήνα_%"] = trend[d]
    if OFFICIAL_TREND.get(d):
        f["επίσημη_ετήσια_τάση_τιμών_%_ΚΤΚ_CYSTAT"] = OFFICIAL_TREND[d]
    if RENTAL_YIELD_HINT.get(d):
        f["ενδεικτική_ακαθάριστη_απόδοση_ενοικίου_επαρχίας_%"] = RENTAL_YIELD_HINT[d]
    if r.get("initial_price") and r.get("price") and r["price"] < r["initial_price"]:
        f["πτώση_από_αρχική_%"] = round((1 - r["price"] / r["initial_price"]) * 100)
    if r.get("relistings"):
        f["επαναληπτικοί_πλειστηριασμοί"] = r["relistings"]
        ur = unsold_reasons(r, ctx)
        if ur:
            f["ΠΙΘΑΝΟΙ_ΛΟΓΟΙ_ΑΓΟΝΟΥ"] = ur
    # ---- Χαρακτηριστικά από το έγγραφο (εξαγωγή κειμένου) ----
    if r.get("floor") is not None:
        f["όροφος"] = r["floor"]
    if r.get("bedrooms"):
        f["υπνοδωμάτια"] = r["bedrooms"]
    if r.get("year_built"):
        f["έτος_κατασκευής"] = r["year_built"]
    if r.get("zone"):
        f["πολεοδομική_ζώνη"] = r["zone"]
    if r.get("condition_neg"):
        f["κατάσταση_αρνητικά"] = r["condition_neg"]
    if r.get("features"):
        f["θετικά_χαρακτηριστικά"] = r["features"]
    if r.get("risks"):
        f["ΠΙΘΑΝΑ_ΡΙΣΚΑ_ΑΠΟ_ΕΓΓΡΑΦΟ"] = r["risks"]
    # ---- Επαγγελματικοί δείκτες αποτίμησης (υπολογισμένοι, όχι εκτιμώμενοι) ----
    vm = valuation_metrics(r, ctx)
    if vm:
        f["ΔΕΙΚΤΕΣ_ΑΠΟΤΙΜΗΣΗΣ"] = vm
    # ---- Ρευστότητα περιοχής (από ιστορικό — moat) ----
    if ctx.get("liquidity", {}).get(d):
        f["μέση_ρευστότητα_περιοχής_ημέρες_ως_πώληση"] = ctx["liquidity"][d]
    # ---- Ιστορικοί δείκτες ΑΥΤΟΥ του ακινήτου (moat) ----
    if history is not None:
        hs = history_signals(r, history)
        if hs:
            f["ΙΣΤΟΡΙΚΟ_ΑΚΙΝΗΤΟΥ"] = hs
    if r.get("description"):
        f["περιγραφή"] = r["description"][:500]
    det = r.get("_detail_raw")
    if det:
        f["απόσπασμα_εγγράφου"] = det[:800]
    return f


AI_PROMPT = """Είσαι πιστοποιημένος εκτιμητής και έμπειρος επενδυτής σε πλειστηριασμούς ακινήτων στην Κύπρο.
Αναλύεις ΕΝΑ ακίνητο με τη μεθοδολογία των επαγγελματιών εκτιμητών. Χρησιμοποίησε ΜΟΝΟ τα παρακάτω δεδομένα — μην εφεύρεις μεγέθη, τιμές ή ενοίκια που δεν δίνονται.

ΔΕΔΟΜΕΝΑ:
{facts}

ΜΕΘΟΔΟΛΟΓΙΑ (οι 3 αναγνωρισμένες μέθοδοι εκτιμητών + κανόνας distressed):
1. ΣΥΓΚΡΙΤΙΚΗ (Sales Comparison): σύγκρινε την τιμή/m² με τη βάση αγοράς. Αρνητική «απόκλιση_τιμής_από_αγορά_%» = κάτω από την αγορά = καλό.
2. ΕΙΣΟΔΗΜΑΤΙΚΗ (Income/Cap Rate): για κατοικήσιμα/εμπορικά, η «απόδοση_στην_τιμή_πλειστηριασμού_%» δείχνει την ελκυστικότητα ως επένδυση ενοικίου. Πάνω από την ενδεικτική απόδοση περιοχής = ελκυστικό.
3. ΚΑΝΟΝΑΣ 72% (distressed/flip): θετικό «περιθώριο_έναντι_κανόνα_72%» σημαίνει ότι η τιμή είναι κάτω από το όριο που θα πλήρωνε επενδυτής ανακαίνισης — ισχυρό σήμα ευκαιρίας. Πρόσεξε: ο κανόνας ΔΕΝ αφαιρεί κόστος επισκευών (άγνωστο), άρα ανέφερέ το ως προ-επισκευών.

ΙΣΤΟΡΙΚΑ ΣΗΜΑΤΑ (αν υπάρχουν — δείχνουν δυναμική, όχι μόνο στιγμιότυπο):
- «επίσημη_ετήσια_τάση_τιμών_%_ΚΤΚ_CYSTAT»: επίσημος ρυθμός μεταβολής τιμών της επαρχίας από Κεντρική Τράπεζα/Στατιστική Υπηρεσία — αξιόπιστη εξωτερική αναφορά για την κατεύθυνση της αγοράς (θετικό = ανοδική).
- «ταχύτητα_πτώσης_%_ανά_μήνα»: γρήγορη πτώση (π.χ. >4%/μήνα) = πιεσμένη πώληση, ίσως ευκαιρία ΑΛΛΑ και σήμα προβλήματος — ανέφερέ το με προσοχή.
- «μέση_ρευστότητα_περιοχής_ημέρες_ως_πώληση»: χαμηλή = ρευστή περιοχή, ευκολότερη μεταπώληση (θετικό για flip). Υψηλή = δύσκολη έξοδος (ρίσκο).
- «φορές_μείωσης_τιμής» / «μήνες_στην_αγορά»: πολλές μειώσεις σε μεγάλο διάστημα = επίμονα αδιάθετο, ίσως κρυμμένο πρόβλημα → flag.

Σκέψου βήμα-βήμα εσωτερικά, αλλά επίστρεψε ΜΟΝΟ έγκυρο JSON (χωρίς markdown) με ΑΚΡΙΒΩΣ αυτή τη δομή:
{{"score": ακέραιος 0-100,
 "verdict": "deal" | "fair" | "caution",
 "confidence": "high" | "medium" | "low",
 "category": "flip" | "buy_to_let" | "land" | "commercial" | "unclear",
 "yield_est": αριθμός ή null (χρησιμοποίησε την «απόδοση_στην_τιμή_πλειστηριασμού_%» αν υπάρχει),
 "valuation": {{"market_value_est": αριθμός ή null (η «εκτιμώμενη_αγοραία_αξία»), "discount_pct": αριθμός ή null (πόσο % κάτω από την αγορά, θετικό=έκπτωση), "method": "comparable" | "income" | "mixed" | "insufficient"}},
 "reasons": [2-4 φράσεις στα ελληνικά, ΚΑΘΕΜΙΑ με συγκεκριμένο αριθμό από τους δείκτες (π.χ. «22% κάτω από την αγοραία αξία των €180.000»)],
 "flags": [προειδοποιήσεις στα ελληνικά ΜΟΝΟ από τα δεδομένα],
 "summary": {{"el":"...","en":"...","ru":"...","he":"..."}} 1-2 ουδέτερες προτάσεις ανά γλώσσα}}

ΚΑΝΟΝΕΣ:
- Score: συνδύασε τις 3 μεθόδους. Υψηλό (75+) μόνο όταν ΤΟΥΛΑΧΙΣΤΟΝ δύο μέθοδοι συμφωνούν (π.χ. κάτω από αγορά ΚΑΙ θετικό περιθώριο 72%) ΚΑΙ η κυριότητα είναι καθαρή.
- ΔΙΟΡΘΩΣΗ ΡΙΣΚΟΥ (υποχρεωτικό): αν confidence="low", ΜΕΙΩΣΕ το score κατά ~15-20 μονάδες — καλύτερα συντηρητικό παρά ψεύτικη σιγουριά. Αν βρεις σοβαρό flag (μερίδιο, δικαιώματα τρίτων, χωρίς πρόσβαση), το score ΔΕΝ ξεπερνά το 55 όσο καλή κι αν είναι η τιμή.
- Αν λείπουν οι ΔΕΙΚΤΕΣ_ΑΠΟΤΙΜΗΣΗΣ (χωρίς εμβαδόν ή βάση), score 40-60 και confidence "low".
- confidence: "high" μόνο με εμβαδόν + βάση σύγκρισης ΟΜΟΕΙΔΩΝ + καθαρό τίτλο· "medium" αν η βάση είναι «όλοι οι τύποι»· "low" αν λείπει βάση ή εμβαδόν.
- valuation.method: "comparable" αν στηρίχτηκες στη σύγκριση, "income" στην απόδοση, "mixed" και στα δύο, "insufficient" αν λείπουν δεδομένα.
- category: "land" για οικόπεδο/χωράφι, "commercial" για κατάστημα/γραφείο/αποθήκη/βιομηχανικό/ξενοδοχείο, "flip" για μεγάλη έκπτωση σε κατοικήσιμο, "buy_to_let" για καλή απόδοση ενοικίου.
- ΒΑΘΙΑ ΑΝΑΓΝΩΣΗ ΕΓΓΡΑΦΟΥ (περιγραφή + απόσπασμα): εντόπισε και ανέφερε ό,τι επηρεάζει αξία ή ρίσκο —
  · Κυριότητα: «Μερίδιο Χ/Υ» < 1/1 → flag «πωλείται μερίδιο Χ/Υ, όχι ολόκληρο»
  · Δικαιώματα τρίτων: επικαρπία, δουλεία, ενοικιαστής, εμπράγματα βάρη → flag
  · Πρόσβαση: «χωρίς οδική πρόσβαση», «εγκλωβισμένο» → flag
  · Κατάσταση: «ημιτελές», «υπό ανέγερση», «ερειπωμένο», «χρήζει ανακαίνισης» → επηρεάζει κόστος, ανέφερέ το στα reasons
  · Θετικά: «ανακαινισμένο», «θέα θάλασσα», «γωνιακό», «τελευταίος όροφος» → ανέφερέ τα αν υπάρχουν
- ΠΟΤΕ μην υπόσχεσαι κέρδος. Οι δείκτες είναι εκτιμήσεις, όχι εγγύηση — η αποτίμηση είναι τέχνη, όχι επιστήμη."""


def ai_analyze(r, ctx, history=None):
    facts = build_facts(r, ctx, history)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": AI_MODEL, "max_tokens": 1000,
              "messages": [{"role": "user",
                            "content": AI_PROMPT.format(
                                facts=json.dumps(facts, ensure_ascii=False))}]},
        timeout=90)
    resp.raise_for_status()
    txt = "".join(b.get("text", "") for b in resp.json().get("content", []))
    txt = re.sub(r"```json|```", "", txt).strip()
    out = json.loads(txt)
    # Επικύρωση δομής + λογικά όρια
    assert isinstance(out.get("score"), (int, float)) and 0 <= out["score"] <= 100
    assert out.get("verdict") in ("deal", "fair", "caution")
    assert isinstance(out.get("summary"), dict)
    out.setdefault("confidence", "medium")
    if out["confidence"] not in ("high", "medium", "low"):
        out["confidence"] = "medium"
    out.setdefault("category", "unclear")
    if out.get("yield_est") is not None:
        try:
            out["yield_est"] = round(float(out["yield_est"]), 1)
            if not (0 < out["yield_est"] < 25):   # φίλτρο παράλογων τιμών
                out["yield_est"] = None
        except (ValueError, TypeError):
            out["yield_est"] = None
    # Επικύρωση valuation
    val = out.get("valuation")
    if isinstance(val, dict):
        mv = val.get("market_value_est")
        try:
            val["market_value_est"] = int(mv) if mv else None
        except (ValueError, TypeError):
            val["market_value_est"] = None
        dp = val.get("discount_pct")
        try:
            val["discount_pct"] = round(float(dp)) if dp is not None else None
            # λογικά όρια: έκπτωση/υπερτίμηση εντός -60%..+80%
            if val["discount_pct"] is not None and not (-80 < val["discount_pct"] < 80):
                val["discount_pct"] = None
        except (ValueError, TypeError):
            val["discount_pct"] = None
        if val.get("method") not in ("comparable", "income", "mixed", "insufficient"):
            val["method"] = "insufficient"
    else:
        out["valuation"] = None

    # ---- ΔΙΧΤΥ ΑΣΦΑΛΕΙΑΣ ΡΙΣΚΟΥ (κώδικας, ανεξάρτητο από το AI) ----
    flags = out.get("flags") or []
    flags_txt = " ".join(flags)
    # Σοβαρά flags (μερίδιο, δικαιώματα, πρόσβαση) → το σκορ δεν ξεπερνά το 55
    serious = any(k in flags_txt for k in
                  ("μερίδ", "μερίδιο", "τρίτ", "επικαρπ", "δουλεία",
                   "χωρίς πρόσβαση", "εγκλωβ", "βάρη"))
    if serious and out["score"] > 55:
        out["score"] = 55
        if out["verdict"] == "deal":
            out["verdict"] = "caution"
    # Χαμηλή αξιοπιστία → συντηρητικό ceiling
    if out.get("confidence") == "low" and out["score"] > 65:
        out["score"] = 65
    return out


def enrich_ai(rows, history):
    ctx = market_context(rows, history)
    if ctx["med"]:
        print("Διάμεση €/m² (τρέχοντες):", ctx["med"])
    if ctx["sold_med"]:
        print("Διάμεση €/m² (κατακυρώσεις):", ctx["sold_med"])
    if ctx["trend"]:
        print("Τάση επαρχιών %:", ctx["trend"])
    count = 0
    for r in rows:
        h = history.setdefault(r["code"], {"first_seen": "", "snaps": []})
        det = (h.get("detail") or {}).get("raw")
        if det:
            r["_detail_raw"] = det
        # Ιστορικά σήματα: υπολογίζονται πάντα (δεν κοστίζουν API), για εμφάνιση στο UI
        hsig = history_signals(r, history)
        ureasons = unsold_reasons(r, ctx)
        cached = h.get("ai")
        fresh = cached and cached.get("analyzed_price") == r.get("price")
        if fresh or not ANTHROPIC_API_KEY or count >= AI_PER_RUN:
            if cached:
                r["ai"] = {k: v for k, v in cached.items() if k != "analyzed_price"}
                if hsig:
                    r["ai"]["signals"] = hsig
                if ureasons:
                    r["ai"]["unsold"] = ureasons
            r.pop("_detail_raw", None)
            continue
        try:
            res = ai_analyze(r, ctx, history)
            res["analyzed_price"] = r.get("price")
            h["ai"] = res
            r["ai"] = {k: v for k, v in res.items() if k != "analyzed_price"}
            if hsig:
                r["ai"]["signals"] = hsig
            if ureasons:
                r["ai"]["unsold"] = ureasons
            count += 1
            time.sleep(1)
        except Exception as e:
            print(f"  ! AI {r['code']}: {e}", file=sys.stderr)
            if cached:
                r["ai"] = {k: v for k, v in cached.items() if k != "analyzed_price"}
                if hsig:
                    r["ai"]["signals"] = hsig
                if ureasons:
                    r["ai"]["unsold"] = ureasons
        r.pop("_detail_raw", None)
    if not ANTHROPIC_API_KEY:
        print("AI: χωρίς ANTHROPIC_API_KEY — παράλειψη νέων αναλύσεων")
    elif count:
        print(f"AI: {count} νέες αναλύσεις (μοντέλο {AI_MODEL})")


DISTRICT_SLUG = {
    "ΛΕΥΚΩΣΙΑ": ("nicosia", "Λευκωσίας", "Nicosia"),
    "ΛΕΜΕΣΟΣ": ("limassol", "Λεμεσού", "Limassol"),
    "ΛΑΡΝΑΚΑ": ("larnaca", "Λάρνακας", "Larnaca"),
    "ΠΑΦΟΣ": ("paphos", "Πάφου", "Paphos"),
    "ΑΜΜΟΧΩΣΤΟΣ": ("famagusta", "Αμμοχώστου", "Famagusta"),
    "ΚΕΡΥΝΕΙΑ": ("kyrenia", "Κερύνειας", "Kyrenia"),
}


def _slugify(code):
    return re.sub(r"[^A-Za-z0-9-]", "", (code or "").replace("/", "-"))


def _page_shell(title, desc, canonical, body, updated, extra_head=""):
    """Κοινό, ελαφρύ, αυτόνομο κέλυφος σελίδας SEO (ίδια αισθητική με το site)."""
    hreflang = ""
    if SITE_URL and canonical:
        hreflang = f'<link rel="canonical" href="{canonical}">'
        for l in ("el", "en", "ru", "he"):
            hreflang += f'<link rel="alternate" hreflang="{l}" href="{canonical}?lang={l}">'
        hreflang += f'<link rel="alternate" hreflang="x-default" href="{canonical}">'
    return f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="UTF-8">
<meta name="color-scheme" content="light only">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<meta name="robots" content="index,follow">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="website">
{hreflang}{extra_head}
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--navy:#123B5D;--blue:#2D7FF9;--emerald:#18B67A;--bg:#F8FAFC;--card:#fff;
--ink:#111827;--muted:#6B7280;--line:#E8ECF1}}
*{{box-sizing:border-box;margin:0;padding:0}}html{{color-scheme:light only}}
body{{background:var(--bg)!important;color:var(--ink)!important;
font-family:'Inter',system-ui,sans-serif;font-size:15.5px;line-height:1.62}}
.wrap{{max-width:1000px;margin:0 auto;padding:0 22px}}
.nav{{background:#fff;border-bottom:1px solid var(--line)}}
.nav .row{{display:flex;align-items:center;gap:14px;height:58px}}
.brand{{font-weight:800;color:var(--navy);text-decoration:none;font-size:16px}}
.nav a.l{{margin-inline-start:auto;font-size:14px;font-weight:600;color:var(--blue);text-decoration:none}}
header.h{{background:linear-gradient(135deg,#0E3350,#1B5286);color:#fff;padding:44px 0 40px}}
header.h h1{{font-size:clamp(25px,4vw,36px);font-weight:800;letter-spacing:-.02em;max-width:20ch}}
header.h p{{color:#C4D6EC;margin-top:10px;max-width:60ch}}
.stats{{display:flex;gap:22px;flex-wrap:wrap;margin-top:20px}}
.stats b{{font-family:'IBM Plex Mono',monospace;font-size:22px;font-weight:800;display:block}}
.stats span{{font-size:12.5px;color:#B9CFE8}}
main{{padding:30px 0 50px}}
.intro{{font-size:15.5px;color:#374151;max-width:70ch;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 18px;
text-decoration:none;color:inherit;display:block;box-shadow:0 1px 3px rgba(17,24,39,.05);
transition:transform .15s,box-shadow .15s}}
.card:hover{{transform:translateY(-2px);box-shadow:0 10px 22px -6px rgba(17,24,39,.12)}}
.card .t{{font-weight:800;font-size:16px;color:var(--navy)}}
.card .loc{{font-size:13px;color:var(--muted);margin:3px 0 10px}}
.card .p{{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums}}
.card .m{{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--muted);margin-top:6px}}
.card .ai{{margin-top:10px;font-size:12.5px;color:var(--emerald);font-weight:700}}
.links{{display:flex;flex-wrap:wrap;gap:10px;margin:26px 0}}
.links a{{font-size:13.5px;font-weight:600;color:var(--navy);background:#fff;
border:1px solid var(--line);border-radius:99px;padding:8px 15px;text-decoration:none}}
.det{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:18px}}
.det h2{{font-size:18px;margin-bottom:12px}}
.det table{{width:100%;border-collapse:collapse;font-size:14.5px}}
.det td{{padding:9px 0;border-bottom:1px solid var(--line)}}
.det td:last-child{{text-align:end;font-family:'IBM Plex Mono',monospace}}
.cta{{display:inline-block;background:var(--navy);color:#fff;text-decoration:none;
font-weight:600;border-radius:12px;padding:12px 22px;margin-top:8px}}
.note{{font-size:12px;color:var(--muted);border-top:1px solid var(--line);margin-top:30px;padding-top:16px}}
footer{{border-top:1px solid var(--line);background:#fff;padding:26px 0;font-size:12.5px;color:var(--muted)}}
</style></head><body>
<nav class="nav"><div class="wrap row">
<a class="brand" href="index.html">Cyprus Auctions</a>
<a class="l" href="index.html">← Όλοι οι πλειστηριασμοί</a></div></nav>
{body}
<footer><div class="wrap">Cyprus Auctions · Ανεξάρτητη υπηρεσία πληροφόρησης.
Δεδομένα από δημόσιες αναρτήσεις· ενδέχεται να έχουν αλλάξει. Επιβεβαίωση στο
<a href="https://www.eauction-cy.com">eauction-cy.com</a>. Όχι επενδυτική συμβουλή.
Τελευταία ενημέρωση: {updated[:10]}.</div></footer>
</body></html>"""


def build_seo_pages(rows, updated_iso):
    """Παράγει: (1) μία σελίδα ανά επαρχία, (2) μία σελίδα ανά ακίνητο.
    Επιστρέφει λίστα σχετικών URLs για το sitemap."""
    os.makedirs("d", exist_ok=True)   # district pages
    os.makedirs("p", exist_ok=True)   # property pages
    sitemap_urls = []
    base = SITE_URL.rstrip("/") if SITE_URL else ""

    # ---------- Σελίδες ανά επαρχία ----------
    by_d = {}
    for r in rows:
        by_d.setdefault(r.get("district"), []).append(r)

    district_links = []
    for d, (slug, gen, en) in DISTRICT_SLUG.items():
        items = sorted(by_d.get(d, []),
                       key=lambda r: r.get("auction_date") or "9999")
        fname = f"d/{slug}.html"
        district_links.append((slug, gen, len(items)))
        if not items:
            continue
        prices = [r["price"] for r in items if r.get("price")]
        import statistics
        medp = round(statistics.median(prices)) if prices else 0
        cards = "".join(
            f'<a class="card" href="../p/{_slugify(r["code"])}.html">'
            f'<div class="t">{esc(r.get("type") or "Ακίνητο")}</div>'
            f'<div class="loc">{esc(r.get("municipality") or "")}</div>'
            f'<div class="p">{fmt_eur(r.get("price"))}</div>'
            f'<div class="m">{fmt_dmy(r.get("auction_date"))}'
            + (f' · {round(r["sqm"])} m²' if r.get("sqm") else "") + '</div>'
            + (f'<div class="ai">AI {r["ai"]["score"]}/100</div>' if r.get("ai") else "")
            + '</a>'
            for r in items[:60])
        intro = (f"Δείτε όλους τους επερχόμενους πλειστηριασμούς ακινήτων στην επαρχία "
                 f"{gen} στην Κύπρο — {len(items)} ακίνητα αυτή τη στιγμή, με διάμεση "
                 f"επιφυλασσόμενη τιμή {fmt_eur(medp)}. Περιλαμβάνονται κατοικίες, "
                 f"διαμερίσματα, οικόπεδα και επαγγελματικά ακίνητα από τράπεζες και "
                 f"διαχειριστές δανείων, με ημερομηνίες, τιμές και εκτίμηση AoI ανά ακίνητο. "
                 f"Τα στοιχεία ενημερώνονται καθημερινά.")
        canonical = f"{base}/d/{slug}.html" if base else ""
        body = (f'<header class="h"><div class="wrap">'
                f'<h1>Πλειστηριασμοί ακινήτων {esc(gen)}</h1>'
                f'<p>Επερχόμενοι ηλεκτρονικοί πλειστηριασμοί ενυπόθηκων ακινήτων '
                f'στην επαρχία {esc(gen)}. Property auctions in {esc(en)}, Cyprus.</p>'
                f'<div class="stats"><div><b>{len(items)}</b><span>ακίνητα</span></div>'
                f'<div><b>{fmt_eur(medp)}</b><span>διάμεση τιμή</span></div></div>'
                f'</div></header><main><div class="wrap">'
                f'<p class="intro">{intro}</p>'
                f'<div class="grid">{cards}</div></div></main>')
        with open(fname, "w", encoding="utf-8") as f:
            f.write(_page_shell(
                f"Πλειστηριασμοί ακινήτων {gen} — τιμές & ημερομηνίες | Cyprus Auctions",
                f"Όλοι οι επερχόμενοι πλειστηριασμοί ακινήτων στην επαρχία {gen}: "
                f"{len(items)} ακίνητα, τιμές, ημερομηνίες, AI εκτίμηση. Ενημέρωση καθημερινά.",
                canonical, body, updated_iso))
        if base:
            sitemap_urls.append(f"{base}/d/{slug}.html")

    # ---------- Σελίδες ανά πόλη/δήμο (city-level SEO) ----------
    # Καθαρίζει το «Δ. ΠΑΦΟΥ - ΑΓΙΟΣ ΘΕΟΔΩΡΟΣ» σε «ΠΑΦΟΥ» κ.λπ. και ομαδοποιεί.
    import statistics
    import unicodedata as _ud

    def town_key(muni):
        if not muni:
            return None
        # κράτα το κύριο όνομα: πριν από « - » και χωρίς πρόθεμα «Δ./Κ.»
        base_name = re.split(r"\s[-–]\s", muni)[0]
        base_name = re.sub(r"^\s*[ΔΚ]\.\s*", "", base_name).strip()
        return base_name or None

    def town_slug(name):
        # ελληνικά → λατινικά για καθαρό URL
        m = {"Α":"a","Β":"v","Γ":"g","Δ":"d","Ε":"e","Ζ":"z","Η":"i","Θ":"th",
             "Ι":"i","Κ":"k","Λ":"l","Μ":"m","Ν":"n","Ξ":"x","Ο":"o","Π":"p",
             "Ρ":"r","Σ":"s","Τ":"t","Υ":"y","Φ":"f","Χ":"ch","Ψ":"ps","Ω":"o"}
        s = _ud.normalize("NFD", name.upper())
        s = "".join(c for c in s if not _ud.combining(c))
        out = "".join(m.get(c, c if c.isalnum() else "-") for c in s)
        return re.sub(r"-+", "-", out).strip("-").lower()

    by_town = {}
    for r in rows:
        tk = town_key(r.get("municipality"))
        if tk:
            by_town.setdefault((r.get("district"), tk), []).append(r)

    town_count = 0
    for (dist, town), items in by_town.items():
        if len(items) < 2:          # σελίδα μόνο αν αξίζει (2+ ακίνητα)
            continue
        tslug = town_slug(town)
        if not tslug:
            continue
        items = sorted(items, key=lambda r: r.get("auction_date") or "9999")
        prices = [r["price"] for r in items if r.get("price")]
        medp = round(statistics.median(prices)) if prices else 0
        dgen = DISTRICT_SLUG.get(dist, ("", dist, dist))[1]
        cards = "".join(
            f'<a class="card" href="../p/{_slugify(r["code"])}.html">'
            f'<div class="t">{esc(r.get("type") or "Ακίνητο")}</div>'
            f'<div class="loc">{esc(r.get("municipality") or "")}</div>'
            f'<div class="p">{fmt_eur(r.get("price"))}</div>'
            f'<div class="m">{fmt_dmy(r.get("auction_date"))}'
            + (f' · {round(r["sqm"])} m²' if r.get("sqm") else "") + '</div>'
            + (f'<div class="ai">AI {r["ai"]["score"]}/100</div>' if r.get("ai") else "")
            + '</a>'
            for r in items[:60])
        intro = (f"Πλειστηριασμοί ακινήτων στην περιοχή {town} ({dgen}) — "
                 f"{len(items)} ακίνητα με διάμεση επιφυλασσόμενη τιμή {fmt_eur(medp)}. "
                 f"Κατοικίες, διαμερίσματα, οικόπεδα και επαγγελματικά ακίνητα σε "
                 f"πλειστηριασμό, με τιμές, ημερομηνίες και AI εκτίμηση. Καθημερινή ενημέρωση.")
        canonical = f"{base}/t/{tslug}.html" if base else ""
        body = (f'<header class="h"><div class="wrap">'
                f'<h1>Πλειστηριασμοί ακινήτων {esc(town)}</h1>'
                f'<p>Επερχόμενοι πλειστηριασμοί στην περιοχή {esc(town)}, επαρχία {esc(dgen)}.</p>'
                f'<div class="stats"><div><b>{len(items)}</b><span>ακίνητα</span></div>'
                f'<div><b>{fmt_eur(medp)}</b><span>διάμεση τιμή</span></div></div>'
                f'</div></header><main><div class="wrap">'
                f'<p class="intro">{intro}</p>'
                f'<div class="links"><a href="../d/'
                f'{DISTRICT_SLUG.get(dist, ("cyprus",))[0]}.html">← Όλη η επαρχία {esc(dgen)}</a></div>'
                f'<div class="grid">{cards}</div></div></main>')
        os.makedirs("t", exist_ok=True)
        with open(f"t/{tslug}.html", "w", encoding="utf-8") as f:
            f.write(_page_shell(
                f"Πλειστηριασμοί ακινήτων {town} — τιμές & ημερομηνίες | Cyprus Auctions",
                f"Πλειστηριασμοί ακινήτων στην περιοχή {town}: {len(items)} ακίνητα, "
                f"τιμές, ημερομηνίες, AI εκτίμηση. Ενημέρωση καθημερινά.",
                canonical, body, updated_iso))
        town_count += 1
        if base:
            sitemap_urls.append(f"{base}/t/{tslug}.html")
    written = 0
    for r in rows:
        slug = _slugify(r["code"])
        if not slug:
            continue
        loc = ", ".join(x for x in [r.get("municipality"), r.get("district")] if x)
        typ = r.get("type") or "Ακίνητο"
        canonical = f"{base}/p/{slug}.html" if base else ""

        detail_rows = [("Είδος", typ), ("Τοποθεσία", loc),
                       ("Επιφυλασσόμενη τιμή", fmt_eur(r.get("price"))),
                       ("Ημερομηνία πλειστηριασμού", fmt_dmy(r.get("auction_date")))]
        if r.get("sqm"):
            detail_rows.append(("Εμβαδόν", f'{round(r["sqm"])} m²'))
            if r.get("price"):
                detail_rows.append(("Τιμή ανά m²", fmt_eur(r["price"] / r["sqm"])))
        if r.get("lender"):
            detail_rows.append(("Ενυπόθηκος δανειστής", r["lender"]))
        if r.get("reg_no"):
            detail_rows.append(("Αρ. εγγραφής", r["reg_no"]))
        tbl = "".join(f"<tr><td>{esc(k)}</td><td>{esc(str(v))}</td></tr>"
                      for k, v in detail_rows)

        ai = r.get("ai") or {}
        ai_html = ""
        if ai:
            reasons = "".join(f"<li>{esc(x)}</li>" for x in ai.get("reasons", []))
            flags = "".join(f'<li style="color:#8A3B32">⚠ {esc(x)}</li>'
                            for x in ai.get("flags", []))
            summ = ai.get("summary", {}).get("el", "")
            ai_html = (f'<div class="det"><h2>Ανάλυση AI — {ai.get("score")}/100</h2>'
                       f'<p style="color:#374151;margin-bottom:10px">{esc(summ)}</p>'
                       f'<ul style="padding-inline-start:18px;line-height:1.7">{reasons}{flags}</ul>'
                       f'<p class="note" style="margin-top:14px;border:none;padding:0">'
                       f'Εκτίμηση AI βάσει δημόσιων δεδομένων — δεν αποτελεί συμβουλή.</p></div>')

        # JSON-LD ανά ακίνητο
        ld = {"@context": "https://schema.org", "@type": "Product",
              "name": f"{typ} — {loc}",
              "category": typ,
              **({"offers": {"@type": "Offer", "price": int(r["price"]),
                             "priceCurrency": "EUR",
                             "availabilityStarts": r.get("auction_date") or ""}}
                 if r.get("price") else {})}
        crumb = {"@context": "https://schema.org", "@type": "BreadcrumbList",
                 "itemListElement": [
                     {"@type": "ListItem", "position": 1, "name": "Πλειστηριασμοί",
                      **({"item": f"{base}/"} if base else {})},
                     {"@type": "ListItem", "position": 2, "name": r.get("district") or "Κύπρος",
                      **({"item": f"{base}/d/{DISTRICT_SLUG.get(r.get('district'), ('cyprus',))[0]}.html"}
                         if base and r.get("district") in DISTRICT_SLUG else {})},
                     {"@type": "ListItem", "position": 3, "name": f"{typ} — {loc}"}]}
        ld_head = ('<script type="application/ld+json">'
                   + json.dumps(ld, ensure_ascii=False) + '</script>'
                   + '<script type="application/ld+json">'
                   + json.dumps(crumb, ensure_ascii=False) + '</script>')

        desc = (f"{typ} σε πλειστηριασμό στην περιοχή {loc}. "
                f"Επιφυλασσόμενη τιμή {fmt_eur(r.get('price'))}, "
                f"ημερομηνία {fmt_dmy(r.get('auction_date'))}. "
                f"Στοιχεία, AI εκτίμηση και οδηγός συμμετοχής.")
        body = (f'<header class="h"><div class="wrap">'
                f'<h1>{esc(typ)} — {esc(loc)}</h1>'
                f'<p>Πλειστηριασμός ακινήτου · Κωδικός {esc(r["code"])}</p>'
                f'</div></header><main><div class="wrap">'
                f'<div class="det"><h2>Στοιχεία ακινήτου</h2><table>{tbl}</table></div>'
                f'{ai_html}'
                f'<a class="cta" href="{esc(r.get("url") or LIST_URL)}" '
                f'target="_blank" rel="noopener">Επίσημη σελίδα & συμμετοχή →</a>'
                f'<p style="margin-top:16px"><a href="../guide.html">Οδηγός: πώς '
                f'συμμετέχω σε πλειστηριασμό</a></p></div></main>')
        with open(f"p/{slug}.html", "w", encoding="utf-8") as f:
            f.write(_page_shell(
                f"{typ} — {loc} | Πλειστηριασμός {fmt_eur(r.get('price'))} | Cyprus Auctions",
                desc, canonical, body, updated_iso, extra_head=ld_head))
        written += 1
        if base:
            sitemap_urls.append(f"{base}/p/{slug}.html")

    print(f"SEO σελίδες: {sum(1 for _,_,n in district_links if n)} επαρχιών + "
          f"{town_count} πόλεων + {written} ακινήτων")
    return sitemap_urls, district_links


def build_report(rows, history, updated_iso):
    """Παράγει το report.html — αυτόματη εικόνα αγοράς από τα δικά μας δεδομένα.
    Ανανεώνεται σε κάθε τρέξιμο· έτοιμη για κοινοποίηση σε LinkedIn/Facebook."""
    import statistics
    from datetime import timedelta

    today = datetime.now(timezone.utc)
    week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    in7 = (today + timedelta(days=7)).strftime("%Y-%m-%d")
    tstr = today.strftime("%Y-%m-%d")

    by_d = {}
    for r in rows:
        d = r.get("district") or "—"
        g = by_d.setdefault(d, {"n": 0, "prices": [], "m2": []})
        g["n"] += 1
        if r.get("price"):
            g["prices"].append(r["price"])
            if r.get("sqm") and r["sqm"] > 5:
                g["m2"].append(r["price"] / r["sqm"])

    idx = {r["code"]: r for r in rows}
    drops = []
    for code, h in history.items():
        snaps = h.get("snaps", [])
        if len(snaps) >= 2 and snaps[-1].get("d", "") >= week_ago:
            prev, last = snaps[-2].get("p"), snaps[-1].get("p")
            if prev and last and last < prev:
                r = idx.get(code, {})
                drops.append({
                    "code": code, "old": prev, "new": last,
                    "pct": round((1 - last / prev) * 100),
                    "type": r.get("type") or "Ακίνητο",
                    "loc": ", ".join(x for x in [r.get("municipality"),
                                                 r.get("district")] if x)})
    drops.sort(key=lambda x: -x["pct"])

    new_week = sum(1 for r in rows if (r.get("first_seen") or "") >= week_ago)
    next_week = sum(1 for r in rows
                    if tstr <= (r.get("auction_date") or "") <= in7)
    all_prices = [r["price"] for r in rows if r.get("price")]
    med_all = round(statistics.median(all_prices)) if all_prices else 0

    dist_rows = "".join(
        f"<tr><td>{esc(d)}</td><td>{g['n']}</td>"
        f"<td>{fmt_eur(statistics.median(g['prices'])) if g['prices'] else '—'}</td>"
        f"<td>{fmt_eur(statistics.median(g['m2'])) + '/m²' if g['m2'] else '—'}</td></tr>"
        for d, g in sorted(by_d.items(), key=lambda x: -x[1]["n"]))

    drop_rows = "".join(
        f"<tr><td>{esc(x['type'])} — {esc(x['loc'])}</td>"
        f"<td>{fmt_eur(x['old'])} → <b>{fmt_eur(x['new'])}</b></td>"
        f"<td class='pct'>−{x['pct']}%</td></tr>"
        for x in drops[:12]) or \
        "<tr><td colspan='3'>Καμία μείωση τις τελευταίες 7 ημέρες</td></tr>"

    page = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="UTF-8">
<meta name="color-scheme" content="light only">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Αναφορά αγοράς πλειστηριασμών Κύπρου — {tstr}</title>
<meta name="description" content="Εβδομαδιαία εικόνα των πλειστηριασμών ακινήτων στην Κύπρο: πλήθος ανά επαρχία, διάμεσες τιμές και οι μεγαλύτερες μειώσεις της εβδομάδας.">
<link href="https://fonts.googleapis.com/css2?family=Literata:opsz,wght@7..72,700&family=Inter:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--paper:#F7F6F1;--pine:#17453F;--copper-ink:#8A4A17;--line:#D6D3C8;--muted:#565F5C}}
*{{box-sizing:border-box;margin:0;padding:0}}html{{color-scheme:light only}}
body{{background:var(--paper)!important;color:#1B2624!important;
 font-family:'Inter',system-ui,sans-serif;font-size:15px;line-height:1.6}}
.wrap{{max-width:720px;margin:0 auto;padding:26px 18px 46px}}
.eyebrow{{font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.14em;
 text-transform:uppercase;color:var(--copper-ink)}}
h1{{font-family:'Literata',serif;font-size:clamp(23px,4vw,32px);margin:8px 0 2px;color:#131D1B}}
.sub{{color:var(--muted);font-size:13.5px;margin-bottom:22px}}
.stats{{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:26px}}
.stat b{{font-family:'IBM Plex Mono',monospace;font-size:22px;display:block;color:#131D1B}}
.stat span{{font-size:12.5px;color:var(--muted)}}
h2{{font-family:'Literata',serif;font-size:19px;margin:26px 0 10px;color:#131D1B}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
 border-radius:10px;overflow:hidden;font-size:14px}}
td,th{{padding:9px 12px;border-bottom:1px solid var(--line);text-align:start}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
td:last-child,th:last-child{{text-align:end;font-family:'IBM Plex Mono',monospace}}
.pct{{color:var(--copper-ink);font-weight:700}}
a{{color:var(--pine)}}
.note{{font-size:12px;color:var(--muted);margin-top:26px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<div class="eyebrow">Cyprus Auctions · Αναφορά αγοράς</div>
<h1>Πλειστηριασμοί ακινήτων Κύπρου</h1>
<div class="sub">Ενημέρωση {tstr} · Weekly market snapshot (EN summary: {len(rows)} upcoming auctions, median reserve {fmt_eur(med_all)})</div>
<div class="stats">
<div class="stat"><b>{len(rows)}</b><span>επερχόμενοι πλειστηριασμοί</span></div>
<div class="stat"><b>{new_week}</b><span>νέοι τις τελευταίες 7 ημέρες</span></div>
<div class="stat"><b>{next_week}</b><span>διεξάγονται τις επόμενες 7</span></div>
<div class="stat"><b>{fmt_eur(med_all)}</b><span>διάμεση επιφυλασσόμενη τιμή</span></div>
</div>
<h2>Ανά επαρχία</h2>
<table><tr><th>Επαρχία</th><th>Πλήθος</th><th>Διάμεση τιμή</th><th>Διάμεση €/m²</th></tr>{dist_rows}</table>
<h2>Μεγαλύτερες μειώσεις τιμής (7 ημέρες)</h2>
<table><tr><th>Ακίνητο</th><th>Τιμή</th><th>Μεταβολή</th></tr>{drop_rows}</table>
<p style="margin-top:18px"><a href="index.html">→ Όλοι οι πλειστηριασμοί με φίλτρα, AI σκορ και ειδοποιήσεις</a></p>
<p class="note">Πηγή: δημόσιες αναρτήσεις ηλεκτρονικών πλειστηριασμών· επεξεργασία Cyprus Auctions.
Τα στοιχεία είναι ενδεικτικά και δεν αποτελούν επενδυτική συμβουλή.
Ελεύθερη αναδημοσίευση με αναφορά στην πηγή.</p>
</div></body></html>"""
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Αναφορά: report.html ({len(rows)} εγγραφές, {len(drops)} μειώσεις 7ημέρου)")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = scrape_all()
    print(f"Σύνολο: {len(rows)} πλειστηριασμοί")
    if not rows:
        print("Καμία εγγραφή — δεν αγγίζω τα υπάρχοντα δεδομένα.", file=sys.stderr)
        sys.exit(1)

    history = load_json(HISTORY_PATH, {})

    for r in rows:
        h = history.setdefault(r["code"], {"first_seen": today, "snaps": []})
        snaps = h["snaps"]
        last = snaps[-1] if snaps else None
        # Νέο snapshot μόνο όταν αλλάζει κάτι ουσιαστικό (κρατά το αρχείο μικρό)
        if (not last or last.get("p") != r["price"]
                or last.get("ad") != r["auction_date"]
                or last.get("s") != r["status"]):
            snaps.append({"d": today, "p": r["price"],
                          "ad": r["auction_date"], "s": r["status"]})

        # Εμπλουτισμός για τη σελίδα
        r["first_seen"] = h["first_seen"]
        prices = [s["p"] for s in snaps if s.get("p")]
        r["initial_price"] = prices[0] if prices else r["price"]
        r["relistings"] = len({s.get("ad") for s in snaps if s.get("ad")}) - 1

    enrich_details(rows, history)
    enrich_ai(rows, history)

    # Επισύναψη συνοπτικού ιστορικού τιμών για το γράφημα στο UI
    for r in rows:
        snaps = (history.get(r["code"]) or {}).get("snaps", [])
        # κράτα μόνο σημεία με τιμή, ελαφριά μορφή {d,p}
        pts = [{"d": s.get("d"), "p": s.get("p")} for s in snaps if s.get("p")]
        if len(pts) >= 2:
            r["history"] = pts

    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(rows),
        "auctions": rows,
    }
    with open(AUCTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))

    new_today = sum(1 for r in rows if r["first_seen"] == today)
    drops = sum(1 for r in rows if r.get("initial_price")
                and r.get("price") and r["price"] < r["initial_price"])
    print(f"Νέα σήμερα: {new_today} | Με μειωμένη τιμή: {drops}")
    print("Τα δεδομένα γράφτηκαν στο data/")

    seo_urls, district_links = build_seo_pages(rows, out["updated"])
    render_seo(rows, out["updated"], district_links, seo_urls)
    build_report(rows, history, out["updated"])


if __name__ == "__main__":
    main()
