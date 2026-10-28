"""
Air France Delay / Cancellation Claim Filler  (V4 - Fixed)
==========================================================
Fixes vs V3:
- Explicit handlers for each wizard step (expenses, personal info, passengers, bank)
- Properly handles mat-select comboboxes (Title, point of contact, country codes)
- Clicks 'No' radio for additional expenses (was incorrectly trying 'Yes' before)
- Selects Turkey (+90) for country code and Turkey for home address
- Selects Turkey for bank country (which switches the form to TR layout)
- Fills account holder / address / city / IBAN with mock data
- Checks 'I do not want to provide my bank details online' checkbox
- Anti-bot Chrome flags: --disable-quic + custom UA (Akamai blocks HTTP/2 from headless)
- Cross-platform paths (use --drafts-dir, default is ./drafts)
- --headless flag for CI/servers (defaults to False for visual debugging)
- Page-state detection so handlers can recover if a step is already done

Flow (never submits - stops on the review page for manual checking):
1. Open https://wwws.airfrance.ca/en/claim. Reject cookies popup.
2. Fill BookingRef, FirstName, LastName. Click Search.
3. Select FlightNumber from radio list. Click Continue.
4. Click 'Flight disruptions and missed connections'.
5. If Status=Delay click 'Flight delayed'; if Status=Cancel click 'Flight canceled'.
6. Step 1: Compensation method = Cash. Accept Terms of Use. Continue.
7. Step 2: Expenses incurred = No. Fill mock context. Continue.
8. Step 3: Personal info:
   - Title combobox = Mr.
   - Point of contact combobox = FirstName + LastName
   - Email = mock
   - Country code = Turkey (+90)
   - Phone = mock
   - Home address Country = Turkey
   Continue.
9. Step 4: Passengers:
   - Check the passenger matching FirstName + LastName
   Continue.
10. Step 5: Bank details:
    - Bank country = Turkey
    - Account holder's first and last name = mock
    - Address of account holder = mock
    - City of residence = mock
    - IBAN = mock (Turkish format)
    - Check 'I do not want to provide my bank details online'
    Continue.
11. STOP - do NOT click Submit claim. Save draft for manual review.

Usage:
    pip install playwright
    playwright install chromium
    python airfrance_claim_v4.py [path/to/info.csv] [--headless] [--slow-mo 120] [--drafts-dir ./drafts]

Required CSV columns: BookingRef, FirstName, LastName, FlightNumber, Status
Optional columns: Title, Email, Phone, PhoneCountryCode, Country, Address, City,
                 AccountHolder, IBAN, DisruptionReason
                 (all optional fields are filled with mock data if missing)
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import string
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import (
    Frame,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CLAIM_URL = "https://wwws.airfrance.ca/en/claim"
DEFAULT_CSV = r"C:\Coding\REFUNDS\AirFrance\info.csv"
DEFAULT_DRAFTS_DIR = Path(r"C:\Coding\REFUNDS\AirFrance\drafts")
TIMEOUT = 25_000
SPA_LOAD_WAIT = 3.0          # seconds, extra wait after networkidle
STEP_CAP = 12                # Max iterations of the wizard loop
SLOW_MO = 120

# Anti-bot: Akamai blocks HTTP/2 from headless Chrome on wwws.airfrance.ca
# Use --disable-quic + a real Chrome UA to bypass
CHROME_LAUNCH_ARGS = [
    "--disable-quic",
    "--disable-blink-features=AutomationControlled",
]
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Mock data defaults (only used when CSV doesn't provide the field)
MOCK_FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Casey", "Morgan", "Riley", "Sam", "Jamie"]
MOCK_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
MOCK_BANK_CITIES = ["Istanbul", "Ankara", "Izmir"]
MOCK_STREETS = ["Istiklal Street", "Ataturk Boulevard", "Bagdat Avenue"]
MOCK_REASONS = [
    "Flight was delayed by over 3 hours causing inconvenience to travel plans.",
    "Connecting flight was missed because of late arrival.",
    "Cancellation caused significant inconvenience to my travel plans.",
]
REQUIRED_COLS = {"BookingRef", "FirstName", "LastName", "FlightNumber", "Status"}

# Default values for the wizard steps (when CSV doesn't override)
DEFAULT_TITLE = "Mr."
DEFAULT_BANK_COUNTRY = "Turkey"
DEFAULT_PHONE_COUNTRY_CODE = "Turkey (+90)"
DEFAULT_HOME_COUNTRY = "Turkey"
DEFAULT_BANK_CITY = "Istanbul"
DEFAULT_BANK_ADDRESS = "123 Istiklal Street, Istanbul"
MOCK_IBAN_TR_TEMPLATE = "TR3200100099999{random_6_digits}"  # Turkish IBAN format (mock)


# --------------------------------------------------------------------------- #
# Mock data
# --------------------------------------------------------------------------- #
def mock_email(first: str, last: str) -> str:
    return f"{first.lower()}.{last.lower()}@example.com"


def mock_phone() -> str:
    return f"555{random.randint(1000000, 9999999)}"


def mock_address() -> str:
    return f"{random.randint(10, 9999)} {random.choice(MOCK_STREETS)}"


def mock_iban_tr() -> str:
    """Mock Turkish IBAN: TR + 24 digits (no spaces, uppercase letters/numbers only)."""
    return "TR" + "".join(random.choices(string.digits, k=24))


def fill_missing_with_mock(row: dict) -> dict:
    first = row.get("FirstName") or random.choice(MOCK_FIRST_NAMES)
    last = row.get("LastName") or random.choice(MOCK_LAST_NAMES)
    defaults = {
        "Email": mock_email(first, last),
        "Phone": mock_phone(),
        "Address": mock_address(),
        "City": DEFAULT_BANK_CITY,
        "Country": DEFAULT_HOME_COUNTRY,
        "AccountHolder": f"{first} {last}".strip(),
        "IBAN": mock_iban_tr(),
        "DisruptionReason": random.choice(MOCK_REASONS),
        "Title": DEFAULT_TITLE,
        "PhoneCountryCode": DEFAULT_PHONE_COUNTRY_CODE,
        "BankCountry": DEFAULT_BANK_COUNTRY,
    }
    out = dict(row)
    for k, v in defaults.items():
        if not (out.get(k) or "").strip():
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Generic helpers (preserved from V3)
# --------------------------------------------------------------------------- #
def pause(msg: str) -> None:
    print(f"\n[ACTION NEEDED] {msg}")
    input("Press Enter here when done... ")


def get_active_frame(page: Page):
    """Return the frame that contains the form controls (Page if no iframe)."""
    for f in [page] + [fr for fr in page.frames if fr != page.main_frame]:
        try:
            if f.locator("input, select, textarea").count() > 0:
                return f
        except Exception:
            continue
    return page


def settle(page: Page, extra: float = SPA_LOAD_WAIT) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=TIMEOUT)
    except Exception:
        pass
    time.sleep(extra)


def diagnose(page: Page, label: str) -> None:
    print(f"\n--- DIAGNOSE: {label} ---")
    for fi, f in enumerate(page.frames):
        print(f"  frame[{fi}]: {f.url}")
    try:
        dump = page.evaluate("""() => [...document.querySelectorAll('input,select,textarea,button,mat-select,[role=combobox]')]
            .filter(e => { const r = e.getBoundingClientRect(); return r.width && r.height; })
            .map(e => ({tag: e.tagName, type: e.type || '', id: e.id, name: e.name,
                        ph: e.placeholder || '', aria: e.getAttribute('aria-label') || '',
                        text: (e.innerText || '').slice(0, 60)}))""")
        for i, el in enumerate(dump[:40], 1):
            print(f"  [{i}] {el}")
    except Exception as e:
        print(f"  (could not dump controls: {e})")
    d = DRAFT_DIR / "diagnostics"
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(d / f"diag_{ts}.png"), full_page=True)
        (d / f"diag_{ts}.html").write_text(page.content(), encoding="utf-8")
        print(f"  Saved screenshot + HTML to {d}")
    except Exception:
        pass
    print("--- END DIAGNOSE ---\n")


def click_text(frame, pattern: str, *, role: str | None = None, timeout: int = TIMEOUT) -> None:
    rx = re.compile(pattern, re.I)
    candidates = []
    if role:
        candidates.append(frame.get_by_role(role, name=rx))
    candidates += [
        frame.get_by_role("button", name=rx),
        frame.get_by_role("link", name=rx),
        frame.get_by_role("radio", name=rx),
        frame.get_by_role("checkbox", name=rx),
        frame.get_by_label(rx),
        frame.get_by_text(rx),
    ]
    per = max(1500, timeout // len(candidates))
    last: Exception | None = None
    for loc in candidates:
        try:
            el = loc.first
            el.wait_for(state="visible", timeout=per)
            el.scroll_into_view_if_needed()
            el.click()
            return
        except Exception as e:
            last = e
    raise PWTimeout(f"No clickable element matching /{pattern}/ ({last})")


def reject_cookies(page: Page) -> bool:
    """Click 'Reject' on the 'Air France uses cookies' popup if present."""
    pats = (r"^\s*reject( all)?( cookies)?\s*$", r"\breject\b", r"tout refuser",
            r"^\s*refuser\b", r"^\s*decline\b")
    for root in [page] + [f for f in page.frames if f != page.main_frame]:
        for pat in pats:
            try:
                btn = root.get_by_role("button", name=re.compile(pat, re.I)).first
                btn.wait_for(state="visible", timeout=2500)
                btn.click()
                print("  Cookie popup: clicked Reject")
                time.sleep(0.6)
                return True
            except Exception:
                continue
    return False


def click_continue(frame, page: Page) -> bool:
    """Click the wizard's Continue button (returns True on success)."""
    # Preferred: submit button with Continue/Next text
    try:
        btn = frame.locator("button[type='submit']:visible",
                            has_text=re.compile(r"^\s*(continue|next|proceed|suivant|continuer)\s*$", re.I)).first
        btn.wait_for(state="visible", timeout=4000)
        btn.scroll_into_view_if_needed()
        btn.click()
        settle(page, SPA_LOAD_WAIT / 2)
        return True
    except Exception:
        pass
    # Fallback: any button with Continue text
    for pat in (r"^\s*continue\s*$", r"^\s*next\s*$", r"^\s*proceed\s*$",
                r"^\s*suivant\s*$", r"^\s*continuer\s*$"):
        try:
            click_text(frame, pat, role="button", timeout=4000)
            settle(page, SPA_LOAD_WAIT / 2)
            return True
        except PWTimeout:
            continue
    return False


# ---- field discovery: match regex against all identifying attributes -------- #
_DESC_JS = """el => {
  const lab = Array.from(el.labels || []).map(l => l.innerText).join(' ');
  const lb = (el.getAttribute('aria-labelledby') || '').split(' ')
      .map(i => { const n = document.getElementById(i); return n ? n.innerText : ''; }).join(' ');
  const ff = el.closest('mat-form-field, .mat-mdc-form-field, .mat-form-field, [class*=form-field], label');
  const ffText = ff ? ff.innerText : '';
  return {
    desc: [el.id, el.name, el.placeholder || '', el.getAttribute('aria-label') || '',
           lab, lb, ffText, el.getAttribute('autocomplete') || ''].join(' | '),
    value: el.value || '', type: el.type || ''
  };
}"""


def _find_control(frame, tag: str, patterns: Iterable[str], only_empty: bool):
    for pat in patterns:
        rx = re.compile(pat, re.I)
        loc = frame.locator(f"{tag}:visible")
        try:
            n = min(loc.count(), 80)
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                info = el.evaluate(_DESC_JS)
            except Exception:
                continue
            if info["type"] in ("hidden", "checkbox", "radio", "submit", "button"):
                continue
            if only_empty and info["value"].strip():
                continue
            if rx.search(info["desc"]):
                return el
    return None


def fill_field(frame, patterns: Iterable[str], value: str, *, only_empty: bool = True) -> bool:
    if not value:
        return False
    for tag in ("input", "textarea"):
        el = _find_control(frame, tag, patterns, only_empty)
        if el is None:
            continue
        try:
            el.scroll_into_view_if_needed()
            try:
                el.fill(value)
            except Exception:
                el.click()
                el.press("Control+A")
                el.type(value, delay=20)
            return True
        except Exception:
            continue
    return False


def select_field(frame, patterns: Iterable[str], value: str) -> bool:
    el = _find_control(frame, "select", patterns, only_empty=False)
    if el is None:
        return False
    try:
        try:
            el.select_option(label=value)
        except Exception:
            el.select_option(value=value)
        return True
    except Exception:
        return False


def choose_radio(frame, patterns: Iterable[str], *, timeout: int = 4000) -> bool:
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for getter in (
            lambda: frame.get_by_role("radio", name=rx),
            lambda: frame.get_by_label(rx),
        ):
            try:
                el = getter().first
                el.wait_for(state="visible", timeout=timeout)
                try:
                    if not el.is_checked():
                        el.check()
                except Exception:
                    el.click()
                return True
            except Exception:
                continue
    return False


def check_checkbox_by_label(frame, patterns: Iterable[str], *, timeout: int = 3000) -> bool:
    """Check a checkbox whose label/aria-label matches one of the patterns."""
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for getter in (
            lambda: frame.get_by_role("checkbox", name=rx),
            lambda: frame.get_by_label(rx),
        ):
            try:
                el = getter().first
                el.wait_for(state="visible", timeout=timeout)
                if not el.is_checked():
                    el.check()
                return True
            except Exception:
                continue
    return False


# ---- NEW: mat-select combobox helper --------------------------------------- #
def select_combobox_option(
    frame,
    page: Page,
    label_patterns: Iterable[str],
    option_patterns: Iterable[str],
    *,
    timeout: int = 5000,
) -> bool:
    """
    Angular Material mat-select dropdowns need: click to open, then click option.
    Searches by label text near the combobox.

    Args:
        frame: Playwright frame/page
        page: Playwright page (for settling after)
        label_patterns: regex patterns to find the right mat-select
                        (matched against surrounding form-field text or aria-label)
        option_patterns: regex patterns to find the right option in the opened dropdown

    Returns True if an option was successfully selected.
    """
    label_patterns = list(label_patterns)
    option_patterns = list(option_patterns)

    # Try to find the mat-select whose surrounding label matches one of label_patterns
    for pat in label_patterns:
        rx = re.compile(pat, re.I)
        # Find all mat-select elements
        try:
            selects = frame.locator("mat-select:visible")
            n = selects.count()
        except Exception:
            n = 0
        for i in range(min(n, 20)):
            sel = selects.nth(i)
            try:
                # Get the surrounding form field text
                info = sel.evaluate("""el => {
                    const ff = el.closest('mat-form-field, .mat-mdc-form-field');
                    return ff ? ff.innerText : el.getAttribute('aria-label') || '';
                }""")
                if not rx.search(info):
                    continue
                # Found it - click to open the dropdown
                sel.scroll_into_view_if_needed()
                sel.click()
                time.sleep(0.8)
                # Now find the matching option in the overlay (options render outside the form)
                for opt_pat in option_patterns:
                    opt_rx = re.compile(opt_pat, re.I)
                    try:
                        # First try exact text match
                        opts = frame.locator("mat-option:visible")
                        cnt = opts.count()
                        for j in range(min(cnt, 100)):
                            o = opts.nth(j)
                            txt = (o.inner_text() or "").strip()
                            if opt_rx.search(txt) or opt_rx.fullmatch(txt):
                                o.scroll_into_view_if_needed()
                                o.click()
                                time.sleep(0.5)
                                return True
                    except Exception:
                        continue
                print(f"  ! Combobox matched label /{pat}/ but no option matched {option_patterns}")
                # Close dropdown by pressing Escape
                try:
                    sel.press("Escape")
                except Exception:
                    pass
            except Exception:
                continue
    return False


def captcha_present(frame) -> bool:
    """True only for a VISIBLE challenge (image puzzle / checkbox)."""
    try:
        pg = frame if hasattr(frame, "frames") else getattr(frame, "page", frame)
        for f in pg.frames:
            u = f.url or ""
            if "recaptcha" in u and "/bframe" in u:
                try:
                    box = f.frame_element().bounding_box()
                    if box and box["width"] > 100 and box["height"] > 100:
                        return True
                except Exception:
                    continue
        return frame.locator(".h-captcha:visible, iframe[title*='challenge' i]:visible").count() > 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Wizard steps 1-5 (initial booking search + flight + category + status)
# --------------------------------------------------------------------------- #
def step1_search(page: Page, row: dict):
    print("  [Step 1-2] Open claim page, reject cookies, search booking")
    page.goto(CLAIM_URL, wait_until="domcontentloaded", timeout=60_000)

    deadline = time.time() + 30
    while time.time() < deadline:
        if page.locator("input, select, textarea").count() > 0:
            break
        time.sleep(0.5)
    reject_cookies(page)
    settle(page)
    reject_cookies(page)  # popup sometimes appears late

    frame = get_active_frame(page)
    ok_ref = fill_field(frame, [r"booking (reference|code|number)|reservation|record locator|file (reference|number)",
                                r"reference", r"booking"], row["BookingRef"])
    ok_fn = fill_field(frame, [r"first ?name|given name|pr[ée]nom"], row["FirstName"])
    ok_ln = fill_field(frame, [r"last ?name|family name|surname|\bnom\b"], row["LastName"])

    if not (ok_ref and ok_fn and ok_ln):
        print("  Label match failed - using positional fallback for the first 3 text inputs")
        try:
            inputs = frame.locator("input[type='text']:visible, input:not([type]):visible")
            inputs.first.wait_for(state="visible", timeout=TIMEOUT)
            if inputs.count() >= 3:
                for idx, key in enumerate(("BookingRef", "FirstName", "LastName")):
                    box = inputs.nth(idx)
                    box.scroll_into_view_if_needed()
                    box.click()
                    box.fill("")
                    box.type(row[key], delay=40)
                ok_ref = ok_fn = ok_ln = True
        except Exception as e:
            print(f"  Positional fallback failed: {e}")

    if not (ok_ref and ok_fn and ok_ln):
        diagnose(page, f"step1 inputs (ref={ok_ref} fn={ok_fn} ln={ok_ln})")
        raise RuntimeError("Could not find booking reference / first name / last name inputs "
                           "(see drafts/diagnostics).")

    if captcha_present(frame) or captcha_present(page):
        pause("CAPTCHA detected. Solve it in the browser.")

    clicked = False
    for loc in (
        frame.locator("button[type='submit']:visible", has_text=re.compile(r"search|find|rechercher|continue", re.I)),
        frame.locator("form button[type='submit']:visible"),
        frame.locator("button[type='submit']:visible"),
    ):
        try:
            btn = loc.first
            btn.wait_for(state="visible", timeout=3000)
            btn.scroll_into_view_if_needed()
            btn.click()
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        diagnose(page, "step1 search button")
        raise RuntimeError("Could not find the Search (submit) button.")

    try:
        frame.locator("input[id^='mat-input-server-app']").first.wait_for(state="detached", timeout=20_000)
    except Exception:
        pass
    settle(page)

    try:
        if page.locator("input[id^='mat-input-server-app']:visible").count() > 0:
            errs = page.locator("[role='alert'], mat-error, .error, [class*='error']:visible").all_inner_texts()
            msg = " | ".join(t.strip() for t in errs if t.strip())[:300]
            diagnose(page, "search did not leave the form")
            raise RuntimeError(f"Search did not go through. Site message: {msg or '(none found)'}")
    except RuntimeError:
        raise
    except Exception:
        pass
    return get_active_frame(page)


def step2_select_flight(frame, page: Page, row: dict):
    print(f"  [Step 3] Select flight {row['FlightNumber']}")
    digits = re.sub(r"\D", "", row["FlightNumber"])
    if not digits:
        raise RuntimeError(f"FlightNumber has no digits: {row['FlightNumber']!r}")
    carrier = re.sub(r"[^A-Za-z]", "", row["FlightNumber"]) or "AF"

    radios = frame.locator("input[type='radio']")
    try:
        radios.first.wait_for(state="attached", timeout=TIMEOUT)
    except PWTimeout:
        diagnose(page, "no flight radios on page")
        raise RuntimeError("Flight list did not load")
    time.sleep(1)

    label_rx = re.compile(rf"select flight\s+{re.escape(carrier)}[\s\u00A0]*0*{digits}(?!\d)", re.I)
    chosen = None
    for i in range(radios.count()):
        r = radios.nth(i)
        try:
            lab = r.get_attribute("aria-label") or ""
        except Exception:
            continue
        if label_rx.search(lab):
            chosen = r
            print(f"  Matched radio: {lab}")
            break
    if chosen is None:
        diagnose(page, f"flight {row['FlightNumber']} not listed")
        raise RuntimeError(f"Flight {row['FlightNumber']} is not listed for this booking")

    for attempt in (
        lambda: chosen.check(force=True, timeout=3000),
        lambda: chosen.locator("xpath=ancestor::mat-radio-button[1]").first.click(timeout=3000),
        lambda: chosen.locator("xpath=ancestor::label[1]").first.click(timeout=3000),
        lambda: chosen.click(force=True, timeout=3000),
    ):
        try:
            attempt()
            if chosen.is_checked():
                break
        except Exception:
            continue
    if not chosen.is_checked():
        diagnose(page, "could not tick flight radio")
        raise RuntimeError(f"Could not select flight {row['FlightNumber']}")

    start_url = page.url
    if not click_continue(frame, page):
        diagnose(page, "continue after flight select")
        raise RuntimeError("Could not click Continue after selecting the flight")
    try:
        page.wait_for_url(lambda u: u != start_url, timeout=20_000)
    except Exception:
        pass
    settle(page, 1.5)
    return get_active_frame(page)


def step3_disruption_type(frame, page: Page):
    """Click 'Flight disruptions and missed connections' - either the SELECT button
    or the heading text (fallback). Handles the 'existing claim' UPDATE modal if it appears."""
    print("  [Step 4] 'Flight disruptions and missed connections'")
    pat = r"flight disruptions? and missed connections?"

    # Try clicking the SELECT button whose aria-label matches the category
    clicked = False
    try:
        btns = frame.locator("button:visible", has_text=re.compile(r"^\s*select\s*$", re.I))
        for i in range(btns.count()):
            b = btns.nth(i)
            aria = b.get_attribute("aria-label") or ""
            if re.search(pat, aria, re.I):
                b.scroll_into_view_if_needed()
                b.click()
                clicked = True
                print("  Clicked SELECT for 'Flight disruptions and missed connections'")
                break
    except Exception:
        pass

    if not clicked:
        if not choose_radio(frame, [pat]):
            try:
                click_text(frame, pat)
                clicked = True
            except PWTimeout:
                diagnose(page, "disruption type")
                raise

    settle(page, 1.5)

    # Handle the 'existing claim' UPDATE modal (if a prior claim exists for this flight+category)
    try:
        upd_btn = frame.get_by_role("button", name=re.compile(r"^\s*update\s*$", re.I)).first
        upd_btn.wait_for(state="visible", timeout=3000)
        print("  Existing claim found - clicking UPDATE to proceed")
        upd_btn.click()
        time.sleep(2)
        # Fill the context textbox in the modal
        fill_field(frame, [r"context|comment|message|description|detail|share any"],
                   "Flight was delayed. Updating existing claim with additional information.",
                   only_empty=False)
        # Click the modal's Update button (the second one - inside the dialog)
        try:
            upd_btn2 = frame.locator("[role='dialog'] button:visible",
                                     has_text=re.compile(r"^\s*update\s*$", re.I)).last
            upd_btn2.click()
            time.sleep(2)
        except Exception:
            pass
    except Exception:
        pass  # No existing claim, proceed normally

    settle(page, 1.5)
    return get_active_frame(page)


def step4_status(frame, page: Page, row: dict):
    """Click 'Flight delayed' or 'Flight canceled' based on the CSV Status value."""
    status = (row.get("Status") or "").strip().lower()
    print(f"  [Step 5] Status = {status!r}")
    if status.startswith("delay"):
        target = r"flight ?delayed"
    elif status.startswith("cancel"):
        target = r"flight ?cancell?ed"  # matches both 'canceled' and 'cancelled'
    else:
        raise RuntimeError(f"Unknown Status value: {row.get('Status')!r} (use Delay or Cancel)")

    # Try clicking the SELECT button whose aria-label matches the target sub-category
    clicked = False
    try:
        btns = frame.locator("button:visible", has_text=re.compile(r"^\s*select\s*$", re.I))
        for i in range(btns.count()):
            b = btns.nth(i)
            aria = b.get_attribute("aria-label") or ""
            if re.search(target, aria, re.I):
                b.scroll_into_view_if_needed()
                b.click()
                clicked = True
                print(f"  Clicked SELECT for sub-category matching: {target}")
                break
    except Exception:
        pass

    if not clicked:
        if not choose_radio(frame, [target]):
            try:
                click_text(frame, target)
                clicked = True
            except PWTimeout:
                diagnose(page, "status")
                raise

    settle(page, 2.0)
    return get_active_frame(page)


def step5_compensation_method(frame, page: Page):
    """Step 1 of wizard: Choose Cash compensation, accept Terms of Use, Continue."""
    print("  [Step 6] Compensation = Cash; accept Terms of Use")
    pat = r"\bcash\b|bank transfer|esp[èe]ces|virement"
    if not choose_radio(frame, [pat]):
        try:
            click_text(frame, pat)
        except PWTimeout:
            diagnose(page, "compensation method")
            raise

    try:
        cb = frame.get_by_label(re.compile(r"accept the terms of use", re.I)).first
        cb.wait_for(state="visible", timeout=TIMEOUT)
        if not cb.is_checked():
            cb.check()
    except Exception:
        try:
            click_text(frame, r"accept the terms of use", role="checkbox")
        except PWTimeout:
            try:
                cb = frame.locator("input[type='checkbox']:visible").first
                if not cb.is_checked():
                    cb.check(force=True)
            except Exception:
                diagnose(page, "terms checkbox")
                raise

    if not click_continue(frame, page):
        diagnose(page, "continue after terms")
        raise RuntimeError("Could not click Continue after accepting Terms of Use")
    return get_active_frame(page)


# --------------------------------------------------------------------------- #
# NEW: Explicit handlers for the 5 wizard steps
# --------------------------------------------------------------------------- #
def wizard_step2_expenses(frame, page: Page, row: dict):
    """Step 2 of wizard: Expenses incurred - click 'No' radio, fill context, Continue."""
    print("  [Wizard Step 2] Expenses incurred = No + context")

    # Click "No" radio for "Would you like to claim additional expenses?"
    no_clicked = False
    # Try aria-label / role-based match
    for pat in (r"^no$", r"^no[,\.]?$", r"^no\b"):
        rx = re.compile(pat, re.I)
        for getter in (
            lambda: frame.get_by_role("radio", name=rx),
            lambda: frame.get_by_label(rx),
        ):
            try:
                el = getter().first
                el.wait_for(state="visible", timeout=3000)
                if not el.is_checked():
                    el.check()
                no_clicked = True
                print("  Clicked 'No' for additional expenses")
                break
            except Exception:
                continue
        if no_clicked:
            break

    if not no_clicked:
        # Fallback: find radio button whose ancestor label/mat-radio-button contains "No"
        try:
            radios = frame.locator("mat-radio-button:visible")
            cnt = radios.count()
            for i in range(min(cnt, 20)):
                rb = radios.nth(i)
                txt = (rb.inner_text() or "").strip()
                if re.fullmatch(r"no", txt, re.I):
                    radio = rb.locator("input[type='radio']").first
                    if not radio.is_checked():
                        radio.check(force=True)
                    no_clicked = True
                    print("  Clicked 'No' (fallback via mat-radio-button)")
                    break
        except Exception:
            pass

    if not no_clicked:
        print("  ! Could not find/click 'No' radio - it may already be selected by default")

    # Fill the context textbox with mock or row data
    context = (row.get("DisruptionReason") or "").strip()
    if not context:
        context = random.choice(MOCK_REASONS)

    context_filled = fill_field(
        frame,
        [r"share any|context|comment|message|description|detail|additional"],
        context,
        only_empty=False,  # Force fill even if there's existing content
    )
    if not context_filled:
        # Positional fallback: the textarea is likely the only one on this page
        try:
            ta = frame.locator("textarea:visible").first
            ta.wait_for(state="visible", timeout=3000)
            ta.scroll_into_view_if_needed()
            ta.fill(context)
            context_filled = True
        except Exception as e:
            print(f"  ! Could not fill context: {e}")
    print(f"  Filled context: {context[:60]}{'...' if len(context) > 60 else ''}")

    if not click_continue(frame, page):
        diagnose(page, "wizard step 2 (expenses) continue")
        raise RuntimeError("Could not advance past Step 2 (Expenses incurred)")
    return get_active_frame(page)


def wizard_step3_personal_info(frame, page: Page, row: dict):
    """Step 3 of wizard: Personal information - Title, contact, email, country code, phone, home country."""
    first = (row.get("FirstName") or "").strip()
    last = (row.get("LastName") or "").strip()
    full_name = f"{first} {last}".strip()
    title = (row.get("Title") or DEFAULT_TITLE).strip()
    email = (row.get("Email") or "").strip() or mock_email(first, last)
    phone = (row.get("Phone") or "").strip() or mock_phone()
    phone_country = (row.get("PhoneCountryCode") or DEFAULT_PHONE_COUNTRY_CODE).strip()
    home_country = (row.get("Country") or DEFAULT_HOME_COUNTRY).strip()

    print(f"  [Wizard Step 3] Personal info: Title={title}, Contact={full_name}, Country={home_country}")

    # 1. Select Title (mat-select combobox)
    title_variants = [title, title.rstrip("."), f"{title.rstrip('.')}."]
    if not select_combobox_option(
        frame, page,
        label_patterns=[r"^title$", r"title\b"],
        option_patterns=[rf"^{re.escape(t)}$" for t in title_variants],
    ):
        print(f"  ! Could not select Title='{title}'")

    # 2. Select point of contact (matches FirstName + LastName)
    # The booking may have a different spelling (e.g., AVRAI vs AVRAJ) - try variants
    name_variants = [full_name]
    if first:
        # Common OCR/encoding confusions
        for f in {first, first.replace("I", "J"), first.replace("J", "I")}:
            name_variants.append(f"{f} {last}")
    # Deduplicate while preserving order
    seen = set()
    name_variants = [v for v in name_variants if not (v in seen or seen.add(v))]

    contact_selected = False
    for name in name_variants:
        if select_combobox_option(
            frame, page,
            label_patterns=[r"point of contact"],
            option_patterns=[rf"^{re.escape(name)}$", rf"^{re.escape(name)}\b"],
        ):
            print(f"  Selected point of contact: {name}")
            contact_selected = True
            break
    if not contact_selected:
        print(f"  ! Could not select point of contact matching '{full_name}' (tried: {name_variants})")

    # 3. Fill email
    if not fill_field(frame, [r"e-?mail", r"courriel"], email):
        print(f"  ! Could not fill email={email}")
    else:
        print(f"  Filled email: {email}")

    # 4. Select country code = Turkey (+90)
    if not select_combobox_option(
        frame, page,
        label_patterns=[r"country code"],
        option_patterns=[rf"^{re.escape(phone_country)}$"],
    ):
        print(f"  ! Could not select country code='{phone_country}'")
    else:
        print(f"  Selected country code: {phone_country}")

    # 5. Fill phone number
    if not fill_field(frame, [r"phone|telephone|tel\b"], phone):
        print(f"  ! Could not fill phone={phone}")
    else:
        print(f"  Filled phone: {phone}")

    # 6. Select home address country = Turkey
    if not select_combobox_option(
        frame, page,
        label_patterns=[r"country or region", r"home address.*country", r"country.*region"],
        option_patterns=[rf"^{re.escape(home_country)}$"],
    ):
        print(f"  ! Could not select home country='{home_country}'")
    else:
        print(f"  Selected home country: {home_country}")

    if not click_continue(frame, page):
        diagnose(page, "wizard step 3 (personal info) continue")
        raise RuntimeError("Could not advance past Step 3 (Personal information)")
    return get_active_frame(page)


def wizard_step4_passengers(frame, page: Page, row: dict):
    """Step 4 of wizard: Add passengers - check the matching passenger checkbox."""
    first = (row.get("FirstName") or "").strip().upper()
    last = (row.get("LastName") or "").strip().upper()

    print(f"  [Wizard Step 4] Passengers: looking for '{first} {last}' or '{last} {first}'")

    # Try various name orderings: "LAST FIRST" (Air France convention) or "FIRST LAST"
    candidates = []
    if first and last:
        candidates.append(f"{last} {first}")
        candidates.append(f"{first} {last}")
        # Also try variant spellings (I/J confusion - common OCR issue)
        for f in {first, first.replace("I", "J"), first.replace("J", "I")}:
            candidates.append(f"{last} {f}")
            candidates.append(f"{f} {last}")
    # Deduplicate while preserving order
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    found = False
    # First try: exact match on aria-label
    for cand in candidates:
        try:
            cb = frame.get_by_role("checkbox", name=re.compile(rf"^\s*{re.escape(cand)}\s*$", re.I)).first
            cb.wait_for(state="visible", timeout=2000)
            if not cb.is_checked():
                cb.check()
            found = True
            print(f"  Checked passenger: {cand}")
            break
        except Exception:
            continue

    # Fallback: partial match (substring)
    if not found:
        for cand in candidates:
            try:
                cb = frame.get_by_role("checkbox", name=re.compile(re.escape(cand), re.I)).first
                cb.wait_for(state="visible", timeout=2000)
                if not cb.is_checked():
                    cb.check()
                found = True
                print(f"  Checked passenger (partial): {cand}")
                break
            except Exception:
                continue

    if not found:
        print(f"  ! No passenger checkbox matched '{first} {last}' - the main passenger may "
              f"already be auto-checked (disabled)")

    if not click_continue(frame, page):
        diagnose(page, "wizard step 4 (passengers) continue")
        raise RuntimeError("Could not advance past Step 4 (Add passengers)")
    return get_active_frame(page)


def wizard_step5_bank_details(frame, page: Page, row: dict):
    """Step 5 of wizard: Bank details - select Turkey, fill account holder, address, city, IBAN, check opt-out."""
    first = (row.get("FirstName") or "").strip()
    last = (row.get("LastName") or "").strip()

    bank_country = (row.get("BankCountry") or DEFAULT_BANK_COUNTRY).strip()
    account_holder = (row.get("AccountHolder") or f"{first} {last}".strip()).strip()
    address = (row.get("Address") or DEFAULT_BANK_ADDRESS).strip()
    city = (row.get("City") or DEFAULT_BANK_CITY).strip()
    iban = (row.get("IBAN") or mock_iban_tr()).strip()

    print(f"  [Wizard Step 5] Bank details: country={bank_country}, holder={account_holder}")

    # 1. Select bank country = Turkey (this switches the form to the TR layout)
    if not select_combobox_option(
        frame, page,
        label_patterns=[r"country.*residence.*bank|bank.*country|country of residence"],
        option_patterns=[rf"^{re.escape(bank_country)}$"],
    ):
        print(f"  ! Could not select bank country='{bank_country}'")
    else:
        print(f"  Selected bank country: {bank_country}")

    # Wait for the form to re-render with country-specific fields
    time.sleep(1.5)
    frame = get_active_frame(page)

    # 2. Fill account holder's first and last name
    if not fill_field(
        frame,
        [r"account holder.*(first|name)|first and last name", r"account holder"],
        account_holder,
        only_empty=False,
    ):
        print(f"  ! Could not fill account holder name")
    else:
        print(f"  Filled account holder: {account_holder}")

    # 3. Fill address of account holder
    if not fill_field(
        frame,
        [r"address of account holder|account holder.*address|address.*holder"],
        address,
        only_empty=False,
    ):
        print(f"  ! Could not fill address")
    else:
        print(f"  Filled address: {address}")

    # 4. Fill city of residence
    if not fill_field(
        frame,
        [r"city of residence", r"\bcity\b"],
        city,
        only_empty=False,
    ):
        print(f"  ! Could not fill city")
    else:
        print(f"  Filled city: {city}")

    # 5. Fill IBAN
    if not fill_field(
        frame,
        [r"iban"],
        iban,
        only_empty=False,
    ):
        print(f"  ! Could not fill IBAN")
    else:
        print(f"  Filled IBAN: {iban}")

    # 6. Check "I do not want to provide my bank details online"
    if not check_checkbox_by_label(frame, [r"do not want.*bank details", r"do not want to provide"]):
        # Fallback: try the last visible checkbox on the page
        try:
            cbs = frame.locator("input[type='checkbox']:visible")
            cb = cbs.last
            if not cb.is_checked():
                cb.check(force=True)
            print("  Checked opt-out checkbox (fallback - last checkbox on page)")
        except Exception as e:
            print(f"  ! Could not check opt-out checkbox: {e}")
    else:
        print("  Checked 'I do not want to provide my bank details online'")

    if not click_continue(frame, page):
        diagnose(page, "wizard step 5 (bank) continue")
        raise RuntimeError("Could not advance past Step 5 (Bank details)")
    return get_active_frame(page)


# --------------------------------------------------------------------------- #
# Wizard dispatcher - replaces V3's step6_remaining_wizard
# --------------------------------------------------------------------------- #
def detect_wizard_step(page: Page) -> int:
    """Detect which wizard step is currently visible.
    Returns:
        2 = Expenses incurred
        3 = Personal information
        4 = Add passengers
        5 = Bank details
        99 = Review/submit page (final)
        0 = unknown
    """
    try:
        txt = (page.inner_text("body") or "").lower()
    except Exception:
        txt = ""
    if "submit claim" in txt and ("summary" in txt or "please double-check" in txt):
        return 99
    if "would you like to claim additional expenses" in txt:
        return 2
    if "select the point of contact" in txt or (
        "personal information" in txt and "country code" in txt
    ):
        return 3
    if "add passengers to your claim" in txt:
        return 4
    if "in which country is your bank located" in txt or (
        "bank details" in txt and "account holder" in txt
    ):
        return 5
    return 0


def step6_wizard(frame, page: Page, row: dict) -> str:
    """Run the remaining wizard steps (2-5) explicitly. Stops at the review page (does NOT submit)."""
    print("  [Step 7] Run wizard steps 2-5 explicitly")

    handlers = {
        2: wizard_step2_expenses,
        3: wizard_step3_personal_info,
        4: wizard_step4_passengers,
        5: wizard_step5_bank_details,
    }

    for iteration in range(1, STEP_CAP + 1):
        frame = get_active_frame(page)
        step = detect_wizard_step(page)

        if step == 99:
            print("  Reached review/submit page - NOT submitting.")
            save_draft(page, row)
            return "ready-for-review"

        if step in handlers:
            try:
                frame = handlers[step](frame, page, row)
                settle(page, 1.5)
                continue
            except Exception as e:
                print(f"  ! Step {step} handler error: {e}")
                diagnose(page, f"wizard step {step}")
                # Don't abort - try once more or fall through to manual pause
        else:
            print(f"  (iteration {iteration}) Unknown wizard step (detect_wizard_step={step})")

        if captcha_present(frame) or captcha_present(page):
            pause("CAPTCHA detected. Solve it in the browser.")

        if not click_continue(frame, page):
            pause(f"Could not find Continue on wizard page (detected step={step}). "
                  f"Complete it manually (do NOT submit), then press Enter.")
            save_draft(page, row)
            return "manual-review"

    print("  ! Reached STEP_CAP without finding review page - saving draft for manual check")
    save_draft(page, row)
    return "ready-for-review"


def save_draft(page: Page, row: dict) -> None:
    out_dir = DRAFT_DIR / (row["BookingRef"] or "UNKNOWN")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    page.screenshot(path=str(out_dir / f"review_{ts}.png"), full_page=True)
    (out_dir / f"review_{ts}.html").write_text(page.content(), encoding="utf-8")
    (out_dir / f"data_{ts}.json").write_text(json.dumps(row, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
    print(f"  Draft saved: {out_dir / f'review_{ts}.png'}")


# --------------------------------------------------------------------------- #
# CSV loading (preserved from V3)
# --------------------------------------------------------------------------- #
def load_rows(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        sys.exit(f"CSV file not found: {path}")
    text = None
    for enc in ("utf-8-sig", "utf-16", "utf-8", "cp1254", "latin-1"):
        try:
            text = p.read_text(encoding=enc)
            break
        except (UnicodeError, UnicodeDecodeError):
            continue
    if text is None:
        sys.exit(f"Cannot decode {path}")

    lines = text.splitlines()
    first = lines[0] if lines else ""
    delim = "\t" if first.count("\t") > first.count(",") and first.count("\t") >= first.count(";") else (
        ";" if first.count(";") > first.count(",") else ",")
    rows: list[dict] = []
    for raw in csv.DictReader(lines, delimiter=delim):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not row.get("FirstName") or not row.get("LastName"):
            parts = (row.get("PaxName") or "").split()
            if parts:
                row["FirstName"] = row.get("FirstName") or parts[0]
                row["LastName"] = row.get("LastName") or " ".join(parts[1:])
        if row.get("BookingRef", "").upper() in {"NULL", "N/A", "NONE", "-"}:
            row["BookingRef"] = ""
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Per-row pipeline + main
# --------------------------------------------------------------------------- #
def process_row(page: Page, row: dict) -> str:
    row = fill_missing_with_mock(row)
    frame = step1_search(page, row)
    frame = step2_select_flight(frame, page, row)
    frame = step3_disruption_type(frame, page)
    frame = step4_status(frame, page, row)
    frame = step5_compensation_method(frame, page)
    return step6_wizard(frame, page, row)


# Module-level drafts dir (set by main())
DRAFT_DIR = Path(DEFAULT_DRAFTS_DIR)


def main() -> None:
    global DRAFT_DIR
    ap = argparse.ArgumentParser(description="Air France claim form filler (never submits).")
    ap.add_argument("csv_file", nargs="?", default=DEFAULT_CSV,
                    help=f"Path to CSV file (default: {DEFAULT_CSV})")
    ap.add_argument("--slow-mo", type=int, default=SLOW_MO,
                    help=f"Slow motion delay in ms (default: {SLOW_MO})")
    ap.add_argument("--headless", action="store_true",
                    help="Run browser in headless mode (no GUI). Default: False")
    ap.add_argument("--drafts-dir", default=DEFAULT_DRAFTS_DIR,
                    help=f"Directory to save drafts (default: {DEFAULT_DRAFTS_DIR})")
    args = ap.parse_args()

    DRAFT_DIR = Path(args.drafts_dir)
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)

    rows = load_rows(args.csv_file)
    if not rows:
        sys.exit("CSV has no data rows.")
    missing = REQUIRED_COLS - set(rows[0].keys())
    if missing:
        sys.exit(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    results_path = Path(args.csv_file).with_name(f"claim_results_{datetime.now():%Y%m%d_%H%M%S}.csv")
    results: list[dict[str, Any]] = []
    print(f"Processing {len(rows)} row(s) from {args.csv_file}")
    print(f"Drafts dir: {DRAFT_DIR.resolve()}")
    print(f"Headless: {args.headless}")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            slow_mo=args.slow_mo,
            args=CHROME_LAUNCH_ARGS + [f"--user-agent={CHROME_USER_AGENT}"],
        )
        for i, row in enumerate(rows, 1):
            if not row.get("BookingRef"):
                print(f"[{i}/{len(rows)}] Skipping row without BookingRef.")
                continue
            ctx = browser.new_context(
                locale="en-CA", viewport={"width": 1366, "height": 900},
                user_agent=CHROME_USER_AGENT,
            )
            ctx.set_default_timeout(TIMEOUT)
            page = ctx.new_page()
            ref = row["BookingRef"]
            print(f"\n[{i}/{len(rows)}] {ref}  {row['FirstName']} {row['LastName']}  "
                  f"Flight={row['FlightNumber']}  Status={row['Status']}")
            try:
                outcome = process_row(page, row)
                err = ""
                print("  >>> Form filled. Check it in the browser. NOT submitted.")
            except Exception as e:
                outcome, err = "error", str(e)
                ed = DRAFT_DIR / ref / "errors"
                ed.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(ed / f"error_{datetime.now():%H%M%S}.png"), full_page=True)
                except Exception:
                    pass
                print(f"  !!! ERROR: {err}")
            results.append({"BookingRef": ref, "Outcome": outcome, "Error": err})
            input("  Press Enter to close this window"
                  + (" and continue with the next booking... " if i < len(rows) else " and finish. "))
            ctx.close()
        browser.close()

    with open(results_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["BookingRef", "Outcome", "Error"])
        w.writeheader()
        w.writerows(results)
    print("\n" + "=" * 70)
    print(f"Done. Results: {results_path}")
    print(f"Drafts: {DRAFT_DIR}")


if __name__ == "__main__":
    main()
