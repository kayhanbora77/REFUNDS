"""
Air France Delay / Cancellation Claim Filler  (V3)
==================================================
Flow (never submits - stops on the review page for manual checking):
1.use https://wwws.airfrance.ca/en/claim. If 'Air France uses cookies' popup exists click Reject.
2.Read info.csv file.Set BookingRef, FirstName, LastName on https://wwws.airfrance.ca/en/claim .Click search
3.Read 'FlightNumber' in csv file put and choose in screen and click continue
4.Click 'Flight disruptions and missed connections'
5.If Csv Status=Delay click 'Flight delayed'.If Status=cancel click='Flight canceled'
6.After step 5 there are 5 steps in screen.Step1= Choose Compensation method=Cache. Click 'I accept the Terms of Use.'Continue
7.Step2= Expenses incurred.Would you like to claim additional expenses? Radio button select No.Fill mock text.
8.Step3= Personal information. Select Title from combobox. Select DANIELLE COPLEY=FirstName+LastName from Select the point of contact for this claim from the list below
Email address=enter mock email address. Country code=Select Turkey(+90) from combobox.Enter Telephone number mock value.Select Home address Country or regionTurkey from combobox
9. Step4= Add passengers to your claim.COPLEY DANIELLE (FirstName+LastName) must be checked in checkbox.Click continue button
10.Step5= In which country is your bank located?Country of residence of the bank.Select Turkey from Combobox
Account holder information
Account holder's first and last name *=Enter mock data
Address of account holder *=Enter mock data
City of residence=enter mock data
IBAN (only uppercase letters and numbers, no spaces) *=enter mock data

Checkbox=I do not want to provide my bank details online.Check this checkbox
Click Continue button
11.Do not click 'submit claim' button. Wait manual checking

Usage:
    pip install playwright
    playwright install chromium
    python airfrance_claim_v3.py [path/to/info.csv] [--slow-mo 120]

Required CSV columns: BookingRef, FirstName, LastName, FlightNumber, Status
(Optional: PaxName, Email, Phone, Address, City, PostalCode, Country,
 AccountHolder, IBAN, BIC, FlightDate, FlightOrigin, FlightDestination,
 DisruptionDate, DisruptionReason)

NOTE: mock values (email, IBAN, ...) are for filling the form only.
Replace them with the passenger's real data before anyone submits.
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
from datetime import datetime, date
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
DRAFT_DIR = Path(r"C:\Coding\REFUNDS\AirFrance\drafts")
TIMEOUT = 25_000
SPA_LOAD_WAIT = 3.0          # seconds, extra wait after networkidle
STEP_CAP = 10
SLOW_MO = 120

Frm = "Frame | Page"

# CSV column -> regexes matched against id/name/placeholder/aria-label/label text
EXTRA_FIELDS: list[tuple[str, list[str]]] = [
    ("Email",             [r"e-?mail", r"courriel"]),
    ("Phone",             [r"phone|mobile|t[ée]l[ée]phone"]),
    ("Address",           [r"address|street|adresse|\brue\b"]),
    ("City",              [r"\bcity\b|\btown\b|\bville\b"]),
    ("PostalCode",        [r"postal|zip|post ?code|code postal"]),
    ("Country",           [r"country|\bpays\b"]),
    ("AccountHolder",     [r"account holder|name of (the )?account|beneficiary|titulaire"]),
    ("IBAN",              [r"iban|account number|num[ée]ro de compte"]),
    ("BIC",               [r"\bbic\b|swift"]),
    ("FlightDate",        [r"flight ?date|date of (the )?flight|date du vol"]),
    ("FlightOrigin",      [r"\bfrom\b|departure|origin|d[ée]part|origine"]),
    ("FlightDestination", [r"destination|arrival|\bto\b|arriv[ée]e"]),
    ("DisruptionDate",    [r"disruption ?date|date of (the )?(delay|cancellation)|date du (retard|annulation)"]),
    ("DisruptionReason",  [r"reason|comment|description|detail|raison|commentaire"]),
]

MOCK_FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Casey", "Morgan", "Riley", "Sam", "Jamie"]
MOCK_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
MOCK_CITIES = ["Montreal", "Toronto", "Vancouver", "Ottawa", "Calgary"]
MOCK_STREETS = ["Main St", "King St", "Queen St", "Maple Ave", "Oak Rd"]
MOCK_REASONS = [
    "Flight was delayed due to operational reasons.",
    "Connecting flight was missed because of late arrival.",
    "Cancellation caused significant inconvenience to my travel plans.",
]
REQUIRED_COLS = {"BookingRef", "FirstName", "LastName", "FlightNumber", "Status"}


# --------------------------------------------------------------------------- #
# Mock data
# --------------------------------------------------------------------------- #
def mock_email(first: str, last: str) -> str:
    return f"{first.lower()}.{last.lower()}@example.com"

def mock_phone() -> str:
    return f"+1 ({random.randint(200, 999)}) {random.randint(200, 999)}-{random.randint(1000, 9999)}"

def mock_address() -> str:
    return f"{random.randint(10, 9999)} {random.choice(MOCK_STREETS)}"

def mock_postal() -> str:
    L, D = string.ascii_uppercase, string.digits
    return (f"{random.choice(L)}{random.choice(D)}{random.choice(L)} "
            f"{random.choice(D)}{random.choice(L)}{random.choice(D)}")

def mock_iban() -> str:
    return "FR76" + "".join(random.choices(string.digits, k=23))

def mock_bic() -> str:
    return ("".join(random.choices(string.ascii_uppercase, k=4)) + "FRPP"
            + "".join(random.choices(string.ascii_uppercase + string.digits, k=2)))

def fill_missing_with_mock(row: dict) -> dict:
    first = row.get("FirstName") or random.choice(MOCK_FIRST_NAMES)
    last = row.get("LastName") or random.choice(MOCK_LAST_NAMES)
    defaults = {
        "Email": mock_email(first, last), "Phone": mock_phone(),
        "Address": mock_address(), "City": random.choice(MOCK_CITIES),
        "PostalCode": mock_postal(), "Country": "Canada",
        "AccountHolder": f"{first} {last}".strip(),
        "IBAN": mock_iban(), "BIC": mock_bic(),
        "FlightDate": date.today().isoformat(),
        "FlightOrigin": "CDG", "FlightDestination": "YUL",
        "DisruptionDate": date.today().isoformat(),
        "DisruptionReason": random.choice(MOCK_REASONS),
    }
    out = dict(row)
    for k, v in defaults.items():
        if not (out.get(k) or "").strip():
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Generic helpers
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
        dump = page.evaluate("""() => [...document.querySelectorAll('input,select,textarea,button')]
            .filter(e => { const r = e.getBoundingClientRect(); return r.width && r.height; })
            .map(e => ({tag: e.tagName, type: e.type || '', id: e.id, name: e.name,
                        ph: e.placeholder || '', aria: e.getAttribute('aria-label') || '',
                        text: (e.innerText || '').slice(0, 40)}))""")
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
    # Preferred: the wizard's submit button labelled Continue/Next.
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


def captcha_present(frame) -> bool:
    """True only for a VISIBLE challenge (image puzzle / checkbox).
    The invisible reCAPTCHA Enterprise iframe (size=invisible) needs no action
    and is ignored, so the script never stops for it."""
    try:
        pg = frame if hasattr(frame, "frames") else getattr(frame, "page", frame)
        for f in pg.frames:
            u = f.url or ""
            if "recaptcha" in u and "/bframe" in u:      # challenge popup frame
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
# Wizard steps
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
    reject_cookies(page)           # popup sometimes appears late

    frame = get_active_frame(page)
    ok_ref = fill_field(frame, [r"booking (reference|code|number)|reservation|record locator|file (reference|number)",
                                r"reference", r"booking"], row["BookingRef"])
    ok_fn = fill_field(frame, [r"first ?name|given name|pr[ée]nom"], row["FirstName"])
    ok_ln = fill_field(frame, [r"last ?name|family name|surname|\bnom\b"], row["LastName"])

    if not (ok_ref and ok_fn and ok_ln):
        # Fallback: the form's 3 unlabeled text inputs, in on-screen order:
        # booking reference, first name, last name.
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

    # The header also has a button labelled "Search" (site search) - the one we
    # need is the form's submit button, so try that first.
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

    # Wait until the results page replaces the search form.
    try:
        frame.locator("input[id^='mat-input-server-app']").first.wait_for(state="detached", timeout=20_000)
    except Exception:
        pass
    settle(page)

    # If the form is still there, the site rejected the search - show its message.
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
    # Screen shows "AF 274" (space, maybe non-breaking) while CSV has "AF274".
    rx = re.compile(rf"\b{re.escape(carrier)}[\s ]*0*{digits}(?!\d)", re.I)
    # The page is a radio group; each radio has aria-label
    # "Select flight AF274 from Paris (CDG) to Tokyo (HND) on Monday, 2 February 2026."
    radios = frame.locator("input[type='radio']")
    try:
        radios.first.wait_for(state="attached", timeout=TIMEOUT)
    except PWTimeout:
        diagnose(page, "no flight radios on page")
        raise RuntimeError("Flight list did not load")
    time.sleep(1)

    label_rx = re.compile(rf"select flight\s+{re.escape(carrier)}[\s ]*0*{digits}(?!\d)", re.I)
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

    # Tick it: native check -> click custom mat-radio wrapper -> force.
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
        diagnose(page, "page did not advance after flight select")
        raise RuntimeError("Still on the flight selection page after Continue")
    settle(page, 1.5)
    return get_active_frame(page)


def step3_disruption_type(frame, page: Page):
    print("  [Step 4] 'Flight disruptions and missed connections'")
    pat = r"flight disruptions? and missed connections?"
    if not choose_radio(frame, [pat]):
        try:
            click_text(frame, pat)
        except PWTimeout:
            diagnose(page, "disruption type")
            raise
    click_continue(frame, page)
    return get_active_frame(page)


def step4_status(frame, page: Page, row: dict):
    status = (row.get("Status") or "").strip().lower()
    print(f"  [Step 5] Status = {status!r}")
    if status.startswith("delay"):
        target = r"flight ?delayed"
    elif status.startswith("cancel"):
        target = r"flight ?cancell?ed"       # matches 'canceled' and 'cancelled'
    else:
        raise RuntimeError(f"Unknown Status value: {row.get('Status')!r} (use Delay or Cancel)")
    if not choose_radio(frame, [target]):
        try:
            click_text(frame, target)
        except PWTimeout:
            diagnose(page, "status")
            raise
    click_continue(frame, page)
    return get_active_frame(page)


def step5_compensation_method(frame, page: Page):
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
                cb = frame.locator("input[type='checkbox']").first
                if not cb.is_checked():
                    cb.check(force=True)
            except Exception:
                diagnose(page, "terms checkbox")
                raise

    if not click_continue(frame, page):
        diagnose(page, "continue after terms")
        raise RuntimeError("Could not click Continue after accepting Terms of Use")
    return get_active_frame(page)


def step6_remaining_wizard(frame, page: Page, row: dict) -> str:
    print("  [Step 7] Fill remaining wizard pages (mock data where missing)")
    submit_re = re.compile(r"^\s*(submit|send|confirm)( claim| request| form)?\s*$", re.I)

    for step_idx in range(1, STEP_CAP + 1):
        frame = get_active_frame(page)

        for col, patterns in EXTRA_FIELDS:
            val = (row.get(col) or "").strip()
            if val and not fill_field(frame, patterns, val):
                select_field(frame, patterns, val)

        for rp in (r"i have not contacted", r"\byes\b"):
            try:
                el = frame.get_by_label(re.compile(rp, re.I)).first
                if el.is_visible() and not el.is_checked():
                    el.check()
                    break
            except Exception:
                continue

        # Any still-unchecked required checkbox (declarations / consent)
        try:
            for i in range(min(frame.locator("input[type='checkbox']:visible").count(), 6)):
                cb = frame.locator("input[type='checkbox']:visible").nth(i)
                if not cb.is_checked():
                    cb.check(force=True)
        except Exception:
            pass

        if captcha_present(frame) or captcha_present(page):
            pause("CAPTCHA detected. Solve it in the browser.")

        # Final review page? -> STOP (never click submit)
        try:
            sb = frame.get_by_role("button", name=submit_re)
            if sb.count() > 0 and sb.first.is_visible():
                print("  Reached review/submit page - NOT submitting.")
                save_draft(page, row)
                return "ready-for-review"
        except Exception:
            pass

        if not click_continue(frame, page):
            pause(f"Could not find Continue on wizard page {step_idx}. "
                  f"Complete it manually (do NOT submit), then press Enter.")
            save_draft(page, row)
            return "manual-review"

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
# CSV loading
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
    return step6_remaining_wizard(frame, page, row)


def main() -> None:
    ap = argparse.ArgumentParser(description="Air France claim form filler (never submits).")
    ap.add_argument("csv_file", nargs="?", default=DEFAULT_CSV)
    ap.add_argument("--slow-mo", type=int, default=SLOW_MO)
    args = ap.parse_args()

    rows = load_rows(args.csv_file)
    if not rows:
        sys.exit("CSV has no data rows.")
    missing = REQUIRED_COLS - set(rows[0].keys())
    if missing:
        sys.exit(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = Path(args.csv_file).with_name(f"claim_results_{datetime.now():%Y%m%d_%H%M%S}.csv")
    results: list[dict[str, Any]] = []
    print(f"Processing {len(rows)} row(s) from {args.csv_file}\n" + "=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=args.slow_mo,
                                    args=["--disable-blink-features=AutomationControlled"])
        for i, row in enumerate(rows, 1):
            if not row.get("BookingRef"):
                print(f"[{i}/{len(rows)}] Skipping row without BookingRef.")
                continue
            ctx = browser.new_context(
                locale="en-CA", viewport={"width": 1366, "height": 900},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
            ctx.set_default_timeout(TIMEOUT)
            page = ctx.new_page()
            ref = row["BookingRef"]
            print(f"\n[{i}/{len(rows)}] {ref}  {row['FirstName']} {row['LastName']}  "
                  f"Flight={row['FlightNumber']}  Status={row['Status']}")
            try:
                outcome, err = process_row(page, row), ""
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
    print("\n" + "=" * 70 + f"\nDone. Results: {results_path}\nDrafts: {DRAFT_DIR}")


if __name__ == "__main__":
    main()