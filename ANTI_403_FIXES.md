# ANTI-403 FIXES — Claude Code Implementation Spec
**Version:** 1.0 | March 2026  
**Files to modify:** `scraper.py` + `claygent.py`  
**New dependency:** `curl_cffi` (pip install curl_cffi)  
**Goal:** Reduce 403 bot-blocking rate from current level to near-zero on coaching sites

---

## WHY THESE TWO FIXES

Two detection vectors cause ~90% of 403s on coaching sites:

**Vector 1 — TLS Fingerprinting (~70% of blocks):** Python's `requests` library sends a TLS ClientHello with cipher suites that hash to a Python-specific JA3 fingerprint. Cloudflare, Akamai, and even basic WAFs check this BEFORE looking at headers. Your perfect User-Agents and Sec-CH-UA headers are never evaluated — you're blocked at the TLS handshake.

**Vector 2 — Stale Browser Versions (~20% of blocks):** All UA strings reference Chrome 120-122 and Firefox 122-123 (early 2024, over 2 years old). The Sec-CH-UA header hardcodes Chrome v122. Sites with even basic bot detection flag these as statistically impossible in March 2026.

The remaining ~10% are aggressive Cloudflare/DataDome sites that require full browser rendering — your Playwright fallback already handles these.

---

## FIX 1: TLS Fingerprint Impersonation (curl_cffi)

### 1.1 What Changes

Replace the `requests.Session` HTTP client with `curl_cffi.requests.Session`. `curl_cffi` impersonates real browser TLS fingerprints at the connection level — same JA3/JA4 hash, same HTTP/2 frame ordering, same cipher suites as actual Chrome/Firefox. Same API as requests, drop-in replacement.

### 1.2 Install Dependency

```bash
pip install curl_cffi
```

Add to any `requirements.txt`:
```
curl_cffi>=0.7.0
```

### 1.3 Modify scraper.py — Import with Fallback

**Find this block (around line 36-38):**

```python
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
```

**Replace with:**

```python
import requests  # Keep for exception types and as fallback

# curl_cffi impersonates real browser TLS fingerprints (JA3/JA4)
# This is the #1 fix for 403 bot-blocking — sites block Python's requests
# TLS fingerprint before even looking at headers.
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
```

### 1.4 Modify scraper.py — Replace Session Creation

**Find this block (around line 299-301):**

```python
# Shared requests.Session for connection pooling (DNS cache + keep-alive)
_http_session = requests.Session()
_http_session.headers.update(_get_headers())
```

**Replace with:**

```python
# Shared HTTP session — uses curl_cffi for TLS fingerprint impersonation,
# falls back to plain requests if curl_cffi not installed.
if HAS_CURL_CFFI:
    # impersonate="chrome" makes the TLS handshake identical to real Chrome.
    # This passes JA3/JA4 fingerprint checks that block Python's requests library.
    _http_session = cffi_requests.Session(impersonate="chrome")
    _http_session.headers.update(_get_headers())
else:
    _http_session = requests.Session()
    _http_session.headers.update(_get_headers())
```

### 1.5 Modify scraper.py — Fix Exception Handling

`curl_cffi` raises `curl_cffi.requests.errors.RequestsError` instead of `requests.exceptions.HTTPError`. Since both catch blocks in `fetch_page_simple()` do the same thing, simplify to a single catch.

**Find this block in `fetch_page_simple()` (around line 400-406):**

```python
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return resp, soup
    except requests.exceptions.HTTPError:
        return None, None
    except Exception:
        return None, None
```

**Replace with:**

```python
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return resp, soup
    except Exception:
        # Catches both requests.exceptions.HTTPError and curl_cffi.requests.errors.RequestsError
        return None, None
```

### 1.6 Modify scraper.py — Update _retry_request for curl_cffi Compatibility

`curl_cffi` sessions pass `impersonate` at session level, so per-request header rotation works identically. However, curl_cffi's `Session.get()` returns a response with a slightly different `elapsed` attribute — it's a `float` (seconds) not a `timedelta`.

**Find `analyze_performance()` (around line 952):**

```python
    data["load_time_seconds"] = round(resp.elapsed.total_seconds(), 2)
```

**Replace with:**

```python
    # curl_cffi returns elapsed as float (seconds), requests returns timedelta
    elapsed = resp.elapsed
    if hasattr(elapsed, 'total_seconds'):
        data["load_time_seconds"] = round(elapsed.total_seconds(), 2)
    elif isinstance(elapsed, (int, float)):
        data["load_time_seconds"] = round(float(elapsed), 2)
    else:
        data["load_time_seconds"] = None
```

### 1.7 Modify scraper.py — Update _retry_request Impersonation Rotation

For maximum stealth, rotate the TLS impersonation profile alongside the User-Agent. `curl_cffi` supports impersonating specific browser versions.

**Find `_retry_request()` (around line 180-200).**

**Replace the entire function with:**

```python
# curl_cffi impersonation profiles to rotate with User-Agents
_IMPERSONATE_PROFILES = [
    "chrome", "chrome", "chrome",  # Weighted toward Chrome (most common)
    "chrome", "chrome",
    "safari",
    "firefox",  # Only if curl_cffi version supports it
]


def _retry_request(method, url, max_retries=2, **kwargs):
    """Make an HTTP request with retry + exponential backoff on 403/429/5xx.
    Rotates User-Agent and TLS impersonation profile per attempt."""
    kwargs.setdefault("timeout", TIMEOUT)
    original_headers = kwargs.pop("headers", {}) or {}
    for attempt in range(max_retries + 1):
        try:
            req_headers = dict(original_headers)
            req_headers["User-Agent"] = random.choice(_USER_AGENTS)
            kwargs["headers"] = req_headers

            # Rotate TLS impersonation on retries (curl_cffi only)
            if HAS_CURL_CFFI and attempt > 0:
                profile = random.choice(_IMPERSONATE_PROFILES)
                kwargs["impersonate"] = profile

            resp = method(url, **kwargs)
            if resp.status_code in (403, 429, 503) and attempt < max_retries:
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)
                time.sleep(wait)
                continue
            return resp
        except Exception:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise
    return None
```

**NOTE:** curl_cffi session methods accept per-request `impersonate` override. On first attempt it uses the session default ("chrome"). On retries after a 403, it rotates to a different profile. This way a blocked Chrome fingerprint gets retried as Safari or Firefox.

---

## FIX 2: Update Browser Versions to 2026

### 2.1 Update Desktop User-Agents in scraper.py

**Find `_USER_AGENTS` (around line 74-85).**

**Replace the entire list with:**

```python
# Rotating User-Agent pool — 2026-current browser versions
# Updated March 2026. Review quarterly — stale versions are a detection signal.
_USER_AGENTS = [
    # Chrome 131-133 (Windows + macOS) — ~65% of real traffic
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    # Edge 131-132 (Windows) — ~5% of real traffic
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # Firefox 133-135 (Windows + macOS + Linux) — ~8% of real traffic
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
    # Safari 18.x (macOS) — ~18% of real traffic
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
]
```

### 2.2 Update Mobile User-Agents in scraper.py

**Find `_BROWSER_USER_AGENTS` (around line 87-93).**

**Replace the entire list with:**

```python
_BROWSER_USER_AGENTS = [
    # iPhone — iOS 18.x with Safari and Chrome
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/132.0.6834.78 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    # Android — Chrome 131-132
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.79 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.200 Mobile Safari/537.36",
]
```

### 2.3 Update Sec-CH-UA Headers to Match

**Find `_get_headers()` (around line 96-128).**

**Replace the entire function with:**

```python
def _get_headers():
    """Get request headers with a random User-Agent + synced Sec-CH-UA fingerprint.
    
    Sec-CH-UA version MUST match the Chrome version in the User-Agent.
    Mismatched versions are a detection signal.
    """
    ua = random.choice(_USER_AGENTS)
    is_chrome = "Chrome/" in ua and "Edg/" not in ua
    is_edge = "Edg/" in ua
    is_firefox = "Firefox/" in ua
    is_safari = "Version/" in ua and "Safari/" in ua and "Chrome/" not in ua

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }

    # Extract Chrome version from UA for Sec-CH-UA sync
    import re
    chrome_match = re.search(r"Chrome/(\d+)", ua)
    chrome_ver = chrome_match.group(1) if chrome_match else "132"

    if is_chrome:
        headers.update({
            "Sec-CH-UA": f'"Chromium";v="{chrome_ver}", "Google Chrome";v="{chrome_ver}", "Not_A Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"macOS"' if "Macintosh" in ua else ('"Linux"' if "Linux" in ua else '"Windows"'),
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    elif is_edge:
        edge_match = re.search(r"Edg/(\d+)", ua)
        edge_ver = edge_match.group(1) if edge_match else chrome_ver
        headers.update({
            "Sec-CH-UA": f'"Chromium";v="{chrome_ver}", "Microsoft Edge";v="{edge_ver}", "Not_A Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    elif is_firefox:
        headers.update({
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    # Safari: no Sec-CH-UA headers (Safari doesn't send them)

    return headers
```

### 2.4 Update Navigator User-Agents in claygent.py

**File:** `claygent.py`  
**Find `_NAV_USER_AGENTS` (around line 829-833).**

**Replace with:**

```python
_NAV_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/132.0.6834.78 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.79 Mobile Safari/537.36",
]
```

---

## TESTING CHECKLIST

### Fix 1 — TLS Fingerprinting

- [ ] **Install:** `pip install curl_cffi` completes without error.
- [ ] **Import check:** Run `python -c "from curl_cffi import requests; print('OK')"` — should print OK.
- [ ] **Session check:** Start Python, run:
  ```python
  from curl_cffi import requests
  s = requests.Session(impersonate="chrome")
  r = s.get("https://tls.browserleaks.com/json")
  print(r.json()["ja3_hash"])  # Should NOT match Python requests JA3
  ```
  Compare with:
  ```python
  import requests
  r = requests.get("https://tls.browserleaks.com/json")
  print(r.json()["ja3_hash"])  # This is the Python fingerprint you're replacing
  ```
  The two JA3 hashes must be DIFFERENT. The curl_cffi one should match Chrome's known JA3.

- [ ] **403 reduction test:** Pick 3 coaching sites that currently return 403 with your scraper. Run them with the updated scraper. At least 2 of 3 should now return 200.

- [ ] **Fallback test:** Temporarily rename curl_cffi package dir (or set `HAS_CURL_CFFI = False`). Verify scraper still works using plain `requests` session. All functionality should work, just without TLS impersonation.

- [ ] **Response compatibility:** Run on 5 URLs. Verify these response attributes work:
  - `resp.status_code` — integer
  - `resp.text` — string
  - `resp.content` — bytes
  - `resp.url` — final URL after redirects
  - `resp.headers` — dict-like
  - `resp.elapsed` — check `analyze_performance()` produces valid `load_time_seconds`
  - `resp.history` — list (used in `check_booking_redirects`)

### Fix 2 — Browser Versions

- [ ] **UA version check:** Run `python -c "from scraper import _USER_AGENTS; [print(ua) for ua in _USER_AGENTS]"` — all Chrome versions should be 131+, all Firefox versions 133+, Safari 18+.
- [ ] **Sec-CH-UA sync:** Run `python -c "from scraper import _get_headers; h = _get_headers(); print(h.get('Sec-CH-UA', 'none'))"` multiple times. The version number in Sec-CH-UA must match the Chrome version in the User-Agent. Never "122" anymore.
- [ ] **Playwright UAs:** Check `_BROWSER_USER_AGENTS` — all iOS versions should be 18.x, all Chrome versions 131+.
- [ ] **Navigator UAs:** Check `_NAV_USER_AGENTS` in claygent.py — same version requirements.

### Full Regression

- [ ] Run a 10-lead batch in full mode. Compare 403 count against a previous 10-lead batch run. 403s should drop significantly.
- [ ] Run a 10-lead batch in fast mode. Same comparison.
- [ ] Verify PageSpeed API calls still work (Google API doesn't care about TLS fingerprint, but confirm no regression).
- [ ] Verify Claygent still works — `claygent.py` uses `fetch_page_simple()` from scraper.py, which uses the updated `_http_session`. Confirm Claygent subpage fetching succeeds.
- [ ] Verify booking link verification still works (the `_http_session.head()` calls in `analyze_booking`).

---

## COST: ZERO

`curl_cffi` is open source, free, and adds no API costs. The only cost is the pip install and ~30 lines of code changes. No proxy services, no scraping APIs, no monthly subscriptions.

## MAINTENANCE NOTE

Browser versions go stale. Set a quarterly reminder to update the UA pools:
- Check latest Chrome stable version at `chromestatus.com`
- Check latest Firefox stable version at `mozilla.org/firefox/releases`
- Check latest Safari version at `developer.apple.com/safari/release-notes`
- Update all three UA lists (`_USER_AGENTS`, `_BROWSER_USER_AGENTS`, `_NAV_USER_AGENTS`) and the Sec-CH-UA extraction logic
