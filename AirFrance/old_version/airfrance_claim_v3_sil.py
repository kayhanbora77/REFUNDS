"""
Air France Delay / Cancellation Compensation Claim Filler  (V2 — robust selectors)
==================================================================================

Improvements over V1
--------------------
* Waits for the JavaScript SPA to finish rendering (networkidle + extra sleep).
* Detects and switches into any iframe that contains the form.
* Multiple selector strategies per field: label -> placeholder -> id -> name ->
  aria-label -> nearby heading text -> type+position.
* Diagnostic dump: if a required field can't be found, prints every visible
  <input>, <select>, <textarea> and iframe on the page (with id / name /
  placeholder / aria-label) and saves a screenshot + HTML snapshot, so the
  operator can tell us exactly what selectors to add.
* Smarter Continue / Search button matching (English + French).
* Better error messages.

Usage
-----
    pip install playwright
    playwright install chromium
    python airfrance_claim_v2.py                    # uses default CSV path
    python airfrance_claim_v2.py path/to/info.csv
    python airfrance_claim_v2.py --headless         # not recommended

Project path:  C:\\Coding\\REFUNDS\\AirFrance\\
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
TIMEOUT = 25_000            # ms – per-action timeout
SPA_LOAD_WAIT = 3500        # ms – extra wait after networkidle for SPA render
STEP_CAP = 10
SLOW_MO = 120

# (CSV column, list of regexes that might match the field's label/placeholder/id/name)
EXTRA_FIELDS: list[tuple[str, list[str]]] = [
    ("Email",              [r"e-?mail", r"courriel"]),
    ("Phone",              [r"phone|mobile|telephone|t[ée]l[ée]phone"]),
    ("Address",            [r"address|street|adresse|rue"]),
    ("City",               r"city|town|ville"),
    ("PostalCode",         [r"postal|zip|post ?code|code postal"]),
    ("Country",            [r"country|pays"]),
    ("AccountHolder",      [r"account holder|name of (the )?account|beneficiary|titular|titulaire"]),
    ("IBAN",               [r"iban|account number|num[ée]ro de compte"]),
    ("BIC",                [r"\bbic\b|swift"]),
    ("FlightDate",         [r"flight ?date|date of (the )?flight|date du vol"]),
    ("FlightOrigin",       [r"departure|origin|from|d[ée]part|origine"]),
    ("FlightDestination",  [r"destination|arrival|to|arriv[ée]e"]),
    ("DisruptionDate",     [r"disruption ?date|date of (the )?(delay|cancellation)|date du (retard|annulation)"]),
    ("DisruptionReason",   [r"reason|comment|description|detail|raison|commentaire"]),
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
def mock_email(first: str, last: str) -> str: return f"{first.lower()}.{last.lower()}@example.com"
def mock_phone() -> str: return f"+1 ({random.randint(200, 999)}) {random.randint(200, 999)}-{random.randint(1000, 9999)}"
def mock_address() -> str: return f"{random.randint(10, 9999)} {random.choice(MOCK_STREETS)}"
def mock_postal() -> str:
    return (f"{random.choice(string.ascii_uppercase)}{random.randint(0, 9)}"
            f"{random.choice(string.ascii_uppercase)} {random.randint(0, 9)}"
            f"{random.choice(string.ascii_uppercase)}{random.randint(0, 9)}")
def mock_iban() -> str: return "FR76" + "".join(random.choices(string.digits, k=23))
def mock_bic() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=4)) + "FRPP" + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=2))

def fill_missing_with_mock(row: dict) -> dict:
    first = row.get("FirstName", "") or random.choice(MOCK_FIRST_NAMES)
    last = row.get("LastName", "") or random.choice(MOCK_LAST_NAMES)
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


def _flatten(x): 
    if isinstance(x, str): return [x]
    return list(x)


def get_active_frame(page: Page) -> Frame | Page:
    """Return the frame that actually contains the form (Page if no iframe)."""
    # Check the top-level frame first.
    for f in [page] + page.frames:
        try:
            if f.locator("input, select, textarea").count() > 0:
                return f
        except Exception:
            continue
    return page


def diagnose(page: Page, label: str) -> None:
    """Print every visible form control + iframe; save HTML + screenshot."""
    print(f"\n--- DIAGNOSE: {label} ---")
    try:
        frames_info = []
        for fi, f in enumerate(page.frames):
            frames_info.append(f"  frame[{fi}]: url={f.url}")
        print("\n".join(frames_info) if frames_info else "  (no iframes)")
    except Exception:
        pass

    inputs_dump = page.evaluate("""() => {
        const out = [];
        const els = document.querySelectorAll('input, select, textarea, button');
        for (const el of els) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            out.push({
                tag: el.tagName,
                type: el.type || '',
                id: el.id || '',
                name: el.name || '',
                placeholder: el.placeholder || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                value: el.value || '',
                text: (el.innerText || '').slice(0, 60),
                labels: Array.from(el.labels || []).map(l => l.innerText.trim()).join(' | '),
            });
        }
        return out;
    }""")
    print(f"  Visible form controls on main page: {len(inputs_dump)}")
    for i, el in enumerate(inputs_dump[:30], 1):
        print(f"  [{i}] {el['tag']} type={el['type']!r} id={el['id']!r} "
              f"name={el['name']!r} placeholder={el['placeholder']!r} "
              f"ariaLabel={el['ariaLabel']!r} labels={el['labels']!r}")

    err_dir = DRAFT_DIR / "diagnostics"
    err_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    page.screenshot(path=str(err_dir / f"diag_{ts}.png"), full_page=True)
    (err_dir / f"diag_{ts}.html").write_text(page.content(), encoding="utf-8")
    print(f"  Screenshot + HTML saved to {err_dir / f'diag_{ts}.png'}")
    print("--- END DIAGNOSE ---\n")


def click_text(frame: Frame | Page, pattern: str, *, role: str | None = None, timeout: int = TIMEOUT) -> None:
    rx = re.compile(pattern, re.I)
    candidates: list = []
    if role:
        candidates.append(frame.get_by_role(role, name=rx))
    candidates += [
        frame.get_by_role("button", name=rx),
        frame.get_by_role("link", name=rx),
        frame.get_by_role("radio", name=rx),
        frame.get_by_role("checkbox", name=rx),
        frame.get_by_label(rx),
        frame.get_by_text(rx),
        frame.locator(f"button:has-text('{pattern}')"),
        frame.locator(f"a:has-text('{pattern}')"),
    ]
    last_err: Exception | None = None
    for loc in candidates:
        try:
            el = loc.first
            el.wait_for(state="visible", timeout=max(2000, timeout // len(candidates)))
            el.scroll_into_view_if_needed()
            el.click()
            return
        except Exception as e:
            last_err = e
            continue
    raise PWTimeout(f"Could not find clickable element matching /{pattern}/ ({last_err})")


def click_continue(frame: Frame | Page) -> bool:
    for pat in (r"^\s*continue\s*$", r"^\s*next\s*$", r"^\s*proceed\s*$",
                r"^\s*suivant\s*$", r"^\s*continuer\s*$", r"^\s*find my booking\s*$"):
        try:
            click_text(frame, pat, role="button", timeout=4000)
            try:
                frame.wait_for_load_state("networkidle", timeout=TIMEOUT)
            except Exception:
                pass
            time.sleep(SPA_LOAD_WAIT / 2000)
            return True
        except PWTimeout:
            continue
    return False


def fill_field(frame: Frame | Page, patterns: Iterable[str], value: str, *, timeout: int = 4000) -> bool:
    """Try every regex in *patterns* against label, placeholder, id, name, aria-label."""
    for pat in _flatten(patterns) if isinstance(patterns, str) else patterns:
        rx = re.compile(pat, re.I)
        # Strategy 1: get_by_label (label element)
        for getter in (
            lambda: frame.get_by_label(rx),
            lambda: frame.get_by_placeholder(rx),
            lambda: frame.locator(f"input[id*='{pat}']"),
            lambda: frame.locator(f"input[name*='{pat}']"),
            lambda: frame.locator(f"input[aria-label*='{pat}' i]"),
            lambda: frame.locator(f"input[placeholder*='{pat}' i]"),
        ):
            try:
                el = getter().first
                el.wait_for(state="visible", timeout=timeout)
                el.scroll_into_view_if_needed()
                try:
                    el.fill(value)
                except Exception:
                    el.click()
                    el.fill("")
                    el.type(value)
                return True
            except Exception:
                continue
    return False


def select_field(frame: Frame | Page, patterns: Iterable[str], value: str, *, timeout: int = 4000) -> bool:
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for getter in (
            lambda: frame.get_by_label(rx),
            lambda: frame.locator(f"select[id*='{pat}']"),
            lambda: frame.locator(f"select[name*='{pat}']"),
        ):
            try:
                el = getter().first
                el.wait_for(state="visible", timeout=timeout)
                try:
                    el.select_option(label=value)
                except Exception:
                    el.select_option(value=value)
                return True
            except Exception:
                continue
    return False


def choose_radio(frame: Frame | Page, patterns: Iterable[str], *, timeout: int = 4000) -> bool:
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for getter in (
            lambda: frame.get_by_label(rx),
            lambda: frame.get_by_role("radio", name=rx),
            lambda: frame.locator(f"input[type='radio'][aria-label*='{pat}' i]"),
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


def normalise_flight(fn: str) -> str: return re.sub(r"\s+", "", fn).upper()


def maybe_handle_cookie_banner(page: Page) -> None:
    for name in (r"accept all", r"accept cookies?", r"agree", r"^\s*ok\s*$", r"got it", r"allow all", r"tout accepter"):
        try:
            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
            btn.wait_for(state="visible", timeout=1500)
            btn.click()
            time.sleep(0.4)
            return
        except Exception:
            continue


def captcha_present(frame: Frame | Page) -> bool:
    try:
        return frame.locator(
            "iframe[src*='captcha'], iframe[title*='captcha' i], .g-recaptcha, .h-captcha, [data-captcha]"
        ).count() > 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Wizard steps
# --------------------------------------------------------------------------- #
def step1_search(page: Page, row: dict) -> Frame | Page:
    """Open claim page; wait for SPA; fill booking ref/name; click Search. Returns the active frame."""
    print("  [Step 1] Search by booking reference")
    page.goto(CLAIM_URL, wait_until="domcontentloaded", timeout=60_000)
    maybe_handle_cookie_banner(page)

    # SPA might still be rendering. Wait for any input to appear, then networkidle.
    print(f"  Waiting for SPA to render (max 30s)…")
    deadline = time.time() + 30
    while time.time() < deadline:
        if page.locator("input, select, textarea").count() > 0:
            break
        time.sleep(0.5)
    try:
        page.wait_for_load_state("networkidle", timeout=TIMEOUT)
    except PWTimeout:
        pass
    time.sleep(SPA_LOAD_WAIT / 1000)

    # The form may live inside an iframe.
    frame = get_active_frame(page)
    print(f"  Active frame: {'iframe:' + frame.url if hasattr(frame, 'url') else 'main page'}")

    # Try multiple selector strategies for each field.
    ref_patterns = [r"booking (reference|code|number)|reservation|file (reference|number)|record locator",
                    r"reference", r"booking", r"reservation"]
    fn_patterns = [r"first ?name|given name|pr[ée]nom"]
    ln_patterns = [r"last ?name|family name|surname|nom"]

    ok_ref = fill_field(frame, ref_patterns, row["BookingRef"])
    ok_fn = fill_field(frame, fn_patterns, row["FirstName"])
    ok_ln = fill_field(frame, ln_patterns, row["LastName"])

    if not (ok_ref and ok_fn and ok_ln):
        diagnose(page, f"step1 search (ref={ok_ref} fn={ok_fn} ln={ok_ln})")
        raise RuntimeError(
            "Could not find booking reference / first name / last name inputs. "
            "Diagnostic info has been printed & saved to drafts/diagnostics/."
        )

    if captcha_present(frame) or captcha_present(page):
        pause("CAPTCHA detected on the search form. Solve it in the browser.")

    # Click Search / Find my booking / Continue.
    clicked = False
    for pat in (r"^\s*search\s*$", r"^\s*find my booking\s*$", r"^\s*find\s*$",
                r"^\s*continue\s*$", r"^\s*rechercher\s*$", r"^\s*suivant\s*$"):
        try:
            click_text(frame, pat, role="button", timeout=3000)
            clicked = True
            break
        except PWTimeout:
            continue
    if not clicked:
        # Fallback: any visible submit button.
        try:
            frame.locator("button[type='submit'], input[type='submit']").first.click()
            clicked = True
        except Exception:
            pass
    if not clicked:
        diagnose(page, "step1 search button")
        raise RuntimeError("Could not find the Search button on the claim page.")

    try:
        page.wait_for_load_state("networkidle", timeout=TIMEOUT)
    except PWTimeout:
        pass
    time.sleep(SPA_LOAD_WAIT / 1000)
    return get_active_frame(page)


def step2_select_flight(frame: Frame | Page, page: Page, row: dict) -> Frame | Page:
    print(f"  [Step 2] Select flight {row['FlightNumber']}")
    fn = normalise_flight(row["FlightNumber"])
    digits = re.sub(r"\D", "", fn)
    rx = re.compile(rf"(AF\s*)?0*{digits}\b", re.I)

    target = frame.get_by_text(rx).first
    try:
        target.wait_for(state="visible", timeout=TIMEOUT)
    except PWTimeout:
        diagnose(page, f"step2 flight {row['FlightNumber']} not listed")
        raise RuntimeError(f"Flight {row['FlightNumber']} is not listed for this booking")

    target.scroll_into_view_if_needed()
    try:
        target.click()
    except Exception:
        try:
            target.locator(
                "xpath=ancestor::*[self::button or self::label or self::a or self::div[@role='button'] or self::li][1]"
            ).first.click()
        except Exception:
            target.click(force=True)

    click_continue(frame)
    return get_active_frame(page)


def step3_disruption_type(frame: Frame | Page, page: Page) -> Frame | Page:
    print("  [Step 3] Choose disruption type")
    pat = r"flight disruptions? and missed connections?"
    if not choose_radio(frame, [pat]):
        try:
            click_text(frame, pat)
        except PWTimeout:
            diagnose(page, "step3 disruption type")
            raise
    if not click_continue(frame):
        # Some layouts auto-advance
        pass
    return get_active_frame(page)


def step4_status(frame: Frame | Page, page: Page, row: dict) -> Frame | Page:
    status = (row.get("Status") or "").strip().lower()
    print(f"  [Step 4] Status='{status}'")
    if status.startswith("delay"):
        target = r"flight ?delayed"
    elif status.startswith("cancel"):
        target = r"flight ?cancell?ed"
    else:
        raise RuntimeError(f"Unknown Status value: {row.get('Status')!r}")

    if not choose_radio(frame, [target]):
        try:
            click_text(frame, target)
        except PWTimeout:
            diagnose(page, "step4 status")
            raise
    click_continue(frame)
    return get_active_frame(page)


def step5_compensation_method(frame: Frame | Page, page: Page) -> Frame | Page:
    print("  [Step 5] Compensation method = Cash; accept Terms of Use")
    if not choose_radio(frame, [r"\bcash\b|bank transfer|cache|cheque"]):
        try:
            click_text(frame, r"\bcash\b|bank transfer|cache|cheque")
        except PWTimeout:
            diagnose(page, "step5 compensation method")
            raise

    # Terms of Use checkbox
    try:
        cb = frame.get_by_label(re.compile(r"accept the terms of use", re.I)).first
        cb.wait_for(state="visible", timeout=TIMEOUT)
        if not cb.is_checked():
            cb.check()
    except Exception:
        try:
            click_text(frame, r"i accept the terms of use", role="checkbox")
        except PWTimeout:
            try:
                cb = frame.locator("input[type='checkbox']").first
                if cb.is_visible() and not cb.is_checked():
                    cb.check()
            except Exception:
                diagnose(page, "step5 terms checkbox")
                raise

    if not click_continue(frame):
        diagnose(page, "step5 continue")
        raise RuntimeError("Could not click Continue after accepting Terms of Use")
    return get_active_frame(page)


def step6_remaining_wizard(frame: Frame | Page, page: Page, row: dict) -> str:
    print("  [Step 6] Walk through remaining wizard pages")
    submit_re = re.compile(r"^\s*(submit|send|confirm)( claim| request| form)?\s*$", re.I)

    for step_idx in range(1, STEP_CAP + 1):
        frame = get_active_frame(page)
        # 1. Fill optional fields
        for col, patterns in EXTRA_FIELDS:
            val = (row.get(col) or "").strip()
            if not val:
                continue
            if not fill_field(frame, patterns, val, timeout=1500):
                select_field(frame, patterns, val, timeout=1000)

        # 2. Default "yes / no" radio groups
        for rp in (r"\byes\b", r"i have not contacted", r"\bno\b"):
            try:
                el = frame.get_by_label(re.compile(rp, re.I)).first
                if el.is_visible() and not el.is_checked():
                    el.check()
                    break
            except Exception:
                continue

        if captcha_present(frame) or captcha_present(page):
            pause("CAPTCHA detected. Solve it in the browser.")

        # 3. Is this the final review page?
        submit_btn = frame.get_by_role("button", name=submit_re)
        try:
            if submit_btn.count() > 0 and submit_btn.first.is_visible():
                print("  Reached review/submit page – saving draft, NOT submitting.")
                save_draft(page, row)
                return "ready-for-review"
        except Exception:
            pass

        # 4. Required-field validation
        invalid = frame.locator("[aria-invalid='true'], input:invalid, select:invalid")
        if invalid.count() > 0:
            print("  Some required fields empty – filling mock data and retrying.")
            for col, patterns in EXTRA_FIELDS:
                fill_field(frame, patterns, row.get(col, ""), timeout=1000)

        # 5. Continue
        if not click_continue(frame):
            if submit_btn.count() > 0:
                save_draft(page, row)
                return "ready-for-review"
            pause(f"Could not find Continue on step {step_idx}. Complete it manually.")

    save_draft(page, row)
    return "ready-for-review"


def save_draft(page: Page, row: dict) -> None:
    out_dir = DRAFT_DIR / (row["BookingRef"] or "UNKNOWN")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    page.screenshot(path=str(out_dir / f"review_{ts}.png"), full_page=True)
    (out_dir / f"review_{ts}.html").write_text(page.content(), encoding="utf-8")
    (out_dir / f"data_{ts}.json").write_text(
        json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Draft saved: {out_dir / f'review_{ts}.png'}")


# --------------------------------------------------------------------------- #
# CSV loading
# --------------------------------------------------------------------------- #
def load_rows(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        sys.exit(f"CSV file not found: {path}")
    text = None
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1254", "latin-1"):
        try:
            text = p.read_text(encoding=enc); break
        except (UnicodeError, UnicodeDecodeError):
            continue
    if text is None:
        sys.exit(f"Cannot decode {path}")

    first_line = text.splitlines()[0] if text else ""
    delim = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    rows: list[dict] = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not row.get("FirstName") or not row.get("LastName"):
            parts = (row.get("PaxName") or "").split()
            if len(parts) >= 2:
                row["FirstName"] = row.get("FirstName") or parts[0]
                row["LastName"] = row.get("LastName") or " ".join(parts[1:])
            elif len(parts) == 1:
                row["FirstName"] = row.get("FirstName") or parts[0]
                row["LastName"] = row.get("LastName") or ""
        if row.get("BookingRef", "").upper() in {"NULL", "N/A", "NONE", "-"}:
            row["BookingRef"] = ""
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Per-row pipeline
# --------------------------------------------------------------------------- #
def process_row(page: Page, row: dict) -> str:
    row = fill_missing_with_mock(row)
    frame = step1_search(page, row)
    frame = step2_select_flight(frame, page, row)
    frame = step3_disruption_type(frame, page)
    frame = step4_status(frame, page, row)
    frame = step5_compensation_method(frame, page)
    return step6_remaining_wizard(frame, page, row)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:

    from airfrance_claim_v2 import load_rows, fill_missing_with_mock

    rows = load_rows(r"C:\Coding\REFUNDS\AirFrance\info.csv")
    r = fill_missing_with_mock(rows[0])
    print("First:", r["FirstName"])
    print("Last :", r["LastName"])
    print("Ref  :", r["BookingRef"])
    print("Flight:", r["FlightNumber"])
    print("Status:", repr(r["Status"]))

    ap = argparse.ArgumentParser(description="Air France claim form filler (no-submit). V2 robust.")
    ap.add_argument("csv_file", nargs="?", default=DEFAULT_CSV, help="Path to the CSV file.")
    ap.add_argument("--headless", action="store_true", help="Run browser headless.")
    ap.add_argument("--slow-mo", type=int, default=SLOW_MO)
    args = ap.parse_args()

    rows = load_rows(args.csv_file)
    if not rows:
        sys.exit("CSV has no data rows.")
    missing = REQUIRED_COLS - set(rows[0].keys())
    if missing:
        sys.exit(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = Path(args.csv_file).with_name(
        f"claim_results_{datetime.now():%Y%m%d_%H%M%S}.csv")
    results: list[dict[str, Any]] = []

    print(f"Processing {len(rows)} row(s) from {args.csv_file}")
    print(f"Drafts will be saved to {DRAFT_DIR}")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless, slow_mo=args.slow_mo,
            args=["--disable-blink-features=AutomationControlled"])
        for i, row in enumerate(rows, 1):
            if not row.get("BookingRef"):
                print(f"[{i}/{len(rows)}] Skipping row without BookingRef."); continue
            ctx = browser.new_context(
                locale="en-CA", viewport={"width": 1366, "height": 900},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
            ctx.set_default_timeout(TIMEOUT)
            page = ctx.new_page()
            ref = row["BookingRef"]
            print(f"\n[{i}/{len(rows)}] BookingRef={ref}  Pax={row['FirstName']} {row['LastName']}  "
                  f"Flight={row['FlightNumber']}  Status={row['Status']}")
            try:
                outcome = process_row(page, row)
                err = ""
                print("  >>> Form filled. Review in the browser. (Not submitted.)")
            except Exception as e:
                outcome, err = "error", str(e)
                err_dir = DRAFT_DIR / (ref or "UNKNOWN") / "errors"
                err_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(err_dir / f"error_{datetime.now():%H%M%S}.png"), full_page=True)
                print(f"  !!! ERROR: {err}")
            results.append({"BookingRef": ref, "Outcome": outcome, "Error": err})
            if i < len(rows):
                input("  Press Enter to close this window and move to the next booking... ")
            else:
                input("  Press Enter to close the browser and finish. ")
            ctx.close()
        browser.close()

    with open(results_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["BookingRef", "Outcome", "Error"])
        w.writeheader(); w.writerows(results)
    print("\n" + "=" * 70)
    print(f"Done. {len(results)} row(s) processed. Results: {results_path}")
    print(f"Drafts: {DRAFT_DIR}")

if __name__ == "__main__":
    main()
