"""
Air France Delay / Cancellation Compensation Claim Filler
=========================================================

Automates the multi-step claim form on https://wwws.airfrance.ca/en/claim
based on the data in a CSV file (one row per passenger / flight).

What it does
------------
1.  Reads the CSV (tab- or comma-separated, encoding auto-detected).
2.  For every row:
        a) Open the claim page
        b) Fill BookingRef, FirstName, LastName -> click "Search"
        c) Pick the FlightNumber listed in the CSV -> click "Continue"
        d) Choose "Flight disruptions and missed connections"
        e) Choose "Flight delayed" or "Flight canceled" based on Status column
        f) Choose compensation method = Cash -> tick "I accept the Terms of Use"
           -> click "Continue"
        g) Fill the remaining wizard steps (passenger contact, flight details,
           bank details, etc.) using CSV values when present, otherwise with
           sensible MOCK data.
3.  Stops on the final review/submit page WITHOUT clicking Submit.
4.  Saves a full-page screenshot + an HTML snapshot of that page to ./drafts/.
5.  Waits for the operator to review / submit manually, then moves on.

The script NEVER submits anything on its own.  If a CAPTCHA or an unexpected
page appears, it pauses and lets the human handle it in the browser window.

Usage
-----
    pip install playwright
    playwright install chromium
    python airfrance_claim.py                    # uses default CSV path
    python airfrance_claim.py path/to/info.csv   # custom CSV path
    python airfrance_claim.py --headless         # headless (NOT recommended)

CSV columns
-----------
Required:
    BookingRef, PaxName (or FirstName + LastName), FlightNumber, Status
        - PaxName "DANIELLE COPLEY" -> FirstName "DANIELLE", LastName "COPLEY"
        - Status: "Delay" or "Cancel" / "Cancelled" / "Canceled"

Optional (used to fill the remaining steps; otherwise MOCK data is used):
    Email, Phone, Address, City, PostalCode, Country,
    AccountHolder, IBAN, BIC, FlightDate, FlightOrigin, FlightDestination,
    DisruptionDate, DisruptionReason

Project path: C:\\Coding\\REFUNDS\\AirFrance\\
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
from typing import Any

from playwright.sync_api import (
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
TIMEOUT = 20_000            # ms – per-action timeout
STEP_CAP = 10              # safety cap on number of wizard pages
SLOW_MO = 120              # ms – slows Playwright actions for visibility

# Field mapping: CSV column -> regex matched against field label/placeholder.
EXTRA_FIELDS: list[tuple[str, str]] = [
    ("Email",              r"e-?mail"),
    ("Phone",              r"phone|mobile|telephone"),
    ("Address",            r"address|street"),
    ("City",               r"city|town"),
    ("PostalCode",         r"postal|zip|post ?code"),
    ("Country",            r"country"),
    ("AccountHolder",      r"account holder|name of (the )?account|beneficiary|titular"),
    ("IBAN",               r"iban|account number|iban / bic"),
    ("BIC",                r"\bbic\b|swift"),
    ("FlightDate",         r"flight ?date|date of (the )?flight"),
    ("FlightOrigin",      r"departure|origin|from"),
    ("FlightDestination",  r"destination|arrival|to"),
    ("DisruptionDate",     r"disruption ?date|date of (the )?(delay|cancellation)"),
    ("DisruptionReason",   r"reason|comment|description|detail"),
]

# Mock data pool – used when a CSV column is empty.
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
# Mock data generator
# --------------------------------------------------------------------------- #
def mock_email(first: str, last: str) -> str:
    return f"{first.lower()}.{last.lower()}@example.com"


def mock_phone() -> str:
    return f"+1 ({random.randint(200, 999)}) {random.randint(200, 999)}-{random.randint(1000, 9999)}"


def mock_address() -> str:
    return f"{random.randint(10, 9999)} {random.choice(MOCK_STREETS)}"


def mock_postal() -> str:
    return f"{random.choice(string.ascii_uppercase)}{random.randint(0, 9)}{random.choice(string.ascii_uppercase)} {random.randint(0, 9)}{random.choice(string.ascii_uppercase)}{random.randint(0, 9)}"


def mock_iban() -> str:
    return "FR76" + "".join(random.choices(string.digits, k=23))


def mock_bic() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=4)) + "FRPP" + "".join(random.choices(string.ascii_uppercase + string.digits, k=2))


def fill_missing_with_mock(row: dict) -> dict:
    """Return a copy of *row* with any empty optional field replaced by mock data."""
    first = row.get("FirstName", "") or random.choice(MOCK_FIRST_NAMES)
    last = row.get("LastName", "") or random.choice(MOCK_LAST_NAMES)
    defaults: dict[str, str] = {
        "Email": mock_email(first, last),
        "Phone": mock_phone(),
        "Address": mock_address(),
        "City": random.choice(MOCK_CITIES),
        "PostalCode": mock_postal(),
        "Country": "Canada",
        "AccountHolder": f"{first} {last}".strip(),
        "IBAN": mock_iban(),
        "BIC": mock_bic(),
        "FlightDate": date.today().isoformat(),
        "FlightOrigin": "CDG",
        "FlightDestination": "YUL",
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


def click_text(page: Page, pattern: str, *, role: str | None = None, timeout: int = TIMEOUT) -> None:
    """Click the first visible element whose text matches the regex."""
    rx = re.compile(pattern, re.I)
    candidates = []
    if role:
        candidates.append(page.get_by_role(role, name=rx))
    candidates += [
        page.get_by_role("button", name=rx),
        page.get_by_role("radio", name=rx),
        page.get_by_role("link", name=rx),
        page.get_by_role("checkbox", name=rx),
        page.get_by_label(rx),
        page.get_by_text(rx),
        page.locator(f"button:has-text('{pattern}')"),
    ]
    last_err: Exception | None = None
    for loc in candidates:
        try:
            el = loc.first
            el.wait_for(state="visible", timeout=max(2000, timeout // len(candidates)))
            el.scroll_into_view_if_needed()
            el.click()
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise PWTimeout(f"Could not find clickable element matching /{pattern}/ ({last_err})")


def click_continue(page: Page) -> bool:
    """Try to click Continue/Next/Proceed. Returns True on success."""
    for pat in (r"^\s*continue\s*$", r"^\s*next\s*$", r"^\s*proceed\s*$", r"^\s*suivant\s*$"):
        try:
            click_text(page, pat, role="button", timeout=4000)
            page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            return True
        except PWTimeout:
            continue
    return False


def fill_by_label(page: Page, pattern: str, value: str, *, timeout: int = 4000) -> bool:
    """Fill the first input whose label or placeholder matches *pattern*."""
    rx = re.compile(pattern, re.I)
    for loc in (page.get_by_label(rx), page.get_by_placeholder(rx)):
        try:
            el = loc.first
            el.wait_for(state="visible", timeout=timeout)
            el.scroll_into_view_if_needed()
            try:
                if el.input_value() == "":
                    el.fill(value)
                else:
                    el.fill("")  # clear and re-type for determinism
                    el.fill(value)
            except Exception:
                el.click()
                el.fill("")
                el.type(value)
            return True
        except Exception:
            continue
    return False


def select_by_label(page: Page, pattern: str, value: str, *, timeout: int = 4000) -> bool:
    """Pick an option in a <select> whose label or value matches."""
    rx = re.compile(pattern, re.I)
    try:
        loc = page.get_by_label(rx).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.select_option(label=value)
        return True
    except Exception:
        pass
    try:
        loc = page.get_by_label(rx).first
        loc.select_option(value=value)
        return True
    except Exception:
        return False


def choose_radio(page: Page, pattern: str, *, timeout: int = 4000) -> bool:
    """Click the first radio whose label matches."""
    rx = re.compile(pattern, re.I)
    try:
        el = page.get_by_label(rx).first
        el.wait_for(state="visible", timeout=timeout)
        if not el.is_checked():
            el.check()
        return True
    except Exception:
        return False


def normalise_flight(fn: str) -> str:
    return re.sub(r"\s+", "", fn).upper()


def maybe_handle_cookie_banner(page: Page) -> None:
    for name in (r"accept all", r"accept cookies?", r"agree", r"^\s*ok\s*$", r"got it", r"allow all"):
        try:
            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
            btn.wait_for(state="visible", timeout=1500)
            btn.click()
            time.sleep(0.4)
            return
        except Exception:
            continue


def captcha_present(page: Page) -> bool:
    return page.locator(
        "iframe[src*='captcha'], iframe[title*='captcha' i], .g-recaptcha, .h-captcha, [data-captcha]"
    ).count() > 0


# --------------------------------------------------------------------------- #
# Wizard steps (one per requirement)
# --------------------------------------------------------------------------- #
def step1_search(page: Page, row: dict) -> None:
    """Open the claim page; fill BookingRef, FirstName, LastName; click Search."""
    print("  [Step 1] Search by booking reference")
    page.goto(CLAIM_URL, wait_until="domcontentloaded")
    maybe_handle_cookie_banner(page)

    ok_ref = fill_by_label(page, r"booking (reference|code)|reservation|file|reference", row["BookingRef"])
    ok_fn = fill_by_label(page, r"first ?name|given name", row["FirstName"])
    ok_ln = fill_by_label(page, r"last ?name|family name|surname", row["LastName"])

    if not (ok_ref and ok_fn and ok_ln):
        raise RuntimeError(
            "Could not find the booking reference / first name / last name inputs. "
            "Page layout may have changed."
        )

    if captcha_present(page):
        pause("CAPTCHA detected on the search form. Solve it in the browser.")

    click_text(page, r"^\s*search\s*$", role="button")
    page.wait_for_load_state("networkidle", timeout=TIMEOUT)


def step2_select_flight(page: Page, row: dict) -> None:
    """Pick the FlightNumber that exists in the CSV from the flight list."""
    print(f"  [Step 2] Select flight {row['FlightNumber']}")
    fn = normalise_flight(row["FlightNumber"])
    digits = re.sub(r"\D", "", fn)
    rx = re.compile(rf"(AF\s*)?0*{digits}\b", re.I)

    target = page.get_by_text(rx).first
    try:
        target.wait_for(state="visible", timeout=TIMEOUT)
    except PWTimeout:
        raise RuntimeError(f"Flight {row['FlightNumber']} is not listed for this booking")

    target.scroll_into_view_if_needed()
    # The visible text might be inside a label/card; click the closest clickable ancestor.
    try:
        target.click()
    except Exception:
        target.locator("xpath=ancestor::*[self::button or self::label or self::a or self::div[@role='button']][1]").first.click()

    if not click_continue(page):
        # Some layouts advance automatically when the flight is selected
        pass


def step3_disruption_type(page: Page) -> None:
    """Select 'Flight disruptions and missed connections'."""
    print("  [Step 3] Choose disruption type")
    click_text(page, r"flight disruptions? and missed connections?", role="radio")
    # Some layouts auto-advance; others need Continue.
    if not click_continue(page):
        # Try clicking the matching label/card as a fallback
        click_text(page, r"flight disruptions? and missed connections?")


def step4_status(page: Page, row: dict) -> None:
    """If Status=Delay -> 'Flight delayed'; if Cancel -> 'Flight canceled'."""
    status = (row.get("Status") or "").strip().lower()
    print(f"  [Step 4] Status='{status}' -> pick disruption status")
    if status.startswith("delay"):
        target = r"flight ?delayed"
    elif status.startswith("cancel"):
        target = r"flight ?cancell?ed"
    else:
        raise RuntimeError(f"Unknown Status value: {row.get('Status')!r}")

    # Try radio first, then plain text/card click.
    if not choose_radio(page, target):
        click_text(page, target)
    click_continue(page)


def step5_compensation_method(page: Page) -> None:
    """Choose Compensation method = Cash; tick 'I accept the Terms of Use'; Continue."""
    print("  [Step 5] Compensation method = Cash; accept Terms of Use")
    # The user wrote "Cache"; treat as Cash (bank transfer) – match either label.
    if not choose_radio(page, r"\bcash\b|bank transfer|cache|cheque"):
        click_text(page, r"\bcash\b|bank transfer|cache|cheque")

    # Terms of Use checkbox
    try:
        cb = page.get_by_label(re.compile(r"accept the terms of use", re.I)).first
        cb.wait_for(state="visible", timeout=TIMEOUT)
        if not cb.is_checked():
            cb.check()
    except Exception:
        try:
            click_text(page, r"i accept the terms of use", role="checkbox")
        except PWTimeout:
            # Fallback: find any checkbox near the "Terms of Use" text.
            cb = page.locator("input[type='checkbox']").first
            if cb.is_visible() and not cb.is_checked():
                cb.check()

    if not click_continue(page):
        raise RuntimeError("Could not click Continue after accepting Terms of Use")


def step6_remaining_wizard(page: Page, row: dict) -> str:
    """
    Walk through the remaining wizard steps (requirement #7 + #8).

    After step 5 the form typically has 5 more screens:
        1. Passenger contact details   (email, phone, address)
        2. Flight details              (date, origin, destination)
        3. Disruption details          (date, reason, comment)
        4. Compensation / bank details (account holder, IBAN, BIC)
        5. Review & submit             (DO NOT submit – save as draft)
    """
    print("  [Step 6] Walk through the remaining wizard pages")

    submit_re = re.compile(r"^\s*(submit|send|confirm)( claim| request| form)?\s*$", re.I)
    draft_saved = False

    for step_idx in range(1, STEP_CAP + 1):
        # 1. Fill every optional field that's visible on this page.
        for col, pattern in EXTRA_FIELDS:
            val = (row.get(col) or "").strip()
            if not val:
                continue
            # Try text input first, then <select>.
            if not fill_by_label(page, pattern, val, timeout=1500):
                select_by_label(page, pattern, val, timeout=1000)

        # 2. Are there any required radio groups we haven't answered? Pick the first option.
        #    (Air France usually asks "Have you already contacted us?" etc.)
        for radio_pattern in (r"\byes\b", r"i have not contacted", r"no"):
            try:
                el = page.get_by_label(re.compile(radio_pattern, re.I)).first
                if el.is_visible() and not el.is_checked():
                    el.check()
                    break
            except Exception:
                continue

        # 3. If a CAPTCHA appeared, let the human solve it.
        if captcha_present(page):
            pause("CAPTCHA detected. Solve it in the browser.")

        # 4. Is this the final review page?
        submit_btn = page.get_by_role("button", name=submit_re)
        try:
            if submit_btn.count() > 0 and submit_btn.first.is_visible():
                print("  Reached the final review/submit page – saving draft, NOT submitting.")
                save_draft(page, row)
                draft_saved = True
                return "ready-for-review"
        except Exception:
            pass

        # 5. Required-field validation check
        invalid = page.locator("[aria-invalid='true'], input:invalid, select:invalid")
        if invalid.count() > 0:
            print("  Some required fields are still empty – filling mock data and retrying.")
            # Re-fill every optional field with mock data
            for col, pattern in EXTRA_FIELDS:
                fill_by_label(page, pattern, row.get(col, ""), timeout=1000)

        # 6. Continue to the next page
        if not click_continue(page):
            # Could be the very last page already, or layout drift.
            if submit_btn.count() > 0:
                save_draft(page, row)
                return "ready-for-review"
            pause(f"Could not find Continue on step {step_idx}. Complete it manually in the browser.")

    if not draft_saved:
        save_draft(page, row)
    return "ready-for-review"


def save_draft(page: Page, row: dict) -> None:
    """Save a screenshot + HTML snapshot + JSON metadata under ./drafts/<BookingRef>/."""
    out_dir = DRAFT_DIR / (row["BookingRef"] or "UNKNOWN")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    page.screenshot(path=str(out_dir / f"review_{ts}.png"), full_page=True)
    (out_dir / f"review_{ts}.html").write_text(page.content(), encoding="utf-8")
    (out_dir / f"data_{ts}.json").write_text(
        json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  Draft saved: {out_dir / f'review_{ts}.png'}")


# --------------------------------------------------------------------------- #
# CSV loading
# --------------------------------------------------------------------------- #
def load_rows(path: str) -> list[dict]:
    """Read a tab- or comma-separated file; trim values; derive FirstName/LastName from PaxName."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"CSV file not found: {path}")

    text = None
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1254", "latin-1"):
        try:
            text = p.read_text(encoding=enc)
            break
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

        # Derive FirstName / LastName from PaxName if those columns are missing.
        if not row.get("FirstName") or not row.get("LastName"):
            parts = (row.get("PaxName") or "").split()
            if len(parts) >= 2:
                row["FirstName"] = row.get("FirstName") or parts[0]
                row["LastName"] = row.get("LastName") or " ".join(parts[1:])
            elif len(parts) == 1:
                row["FirstName"] = row.get("FirstName") or parts[0]
                row["LastName"] = row.get("LastName") or ""

        # Tidy the BookingRef column.
        if row.get("BookingRef", "").upper() in {"NULL", "N/A", "NONE", "-"}:
            row["BookingRef"] = ""

        rows.append(row)

    return rows


# --------------------------------------------------------------------------- #
# Per-row pipeline
# --------------------------------------------------------------------------- #
def process_row(page: Page, row: dict) -> str:
    row = fill_missing_with_mock(row)
    step1_search(page, row)
    step2_select_flight(page, row)
    step3_disruption_type(page)
    step4_status(page, row)
    step5_compensation_method(page)
    return step6_remaining_wizard(page, row)


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Air France claim form filler (no-submit).")
    ap.add_argument("csv_file", nargs="?", default=DEFAULT_CSV, help="Path to the CSV file.")
    ap.add_argument("--headless", action="store_true", help="Run browser headless (NOT recommended).")
    ap.add_argument("--slow-mo", type=int, default=SLOW_MO, help="Slow-mo ms for each Playwright action.")
    args = ap.parse_args()

    rows = load_rows(args.csv_file)
    if not rows:
        sys.exit("CSV has no data rows.")

    first_keys = set(rows[0].keys())
    missing = REQUIRED_COLS - first_keys
    if missing:
        sys.exit(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = Path(args.csv_file).with_name(
        f"claim_results_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    results: list[dict[str, Any]] = []

    print(f"Processing {len(rows)} row(s) from {args.csv_file}")
    print(f"Drafts will be saved to {DRAFT_DIR}")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            slow_mo=args.slow_mo,
            args=["--disable-blink-features=AutomationControlled"],
        )

        for i, row in enumerate(rows, 1):
            if not row.get("BookingRef"):
                print(f"[{i}/{len(rows)}] Skipping row without BookingRef.")
                continue

            ctx = browser.new_context(
                locale="en-CA",
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            )
            ctx.set_default_timeout(TIMEOUT)
            page = ctx.new_page()

            ref = row["BookingRef"]
            print(f"\n[{i}/{len(rows)}] BookingRef={ref}  Pax={row['FirstName']} {row['LastName']}  "
                  f"Flight={row['FlightNumber']}  Status={row['Status']}")

            try:
                outcome = process_row(page, row)
                err = ""
                print("  >>> Form filled. Review in the browser window. (Not submitted.)")
            except Exception as e:
                outcome, err = "error", str(e)
                err_dir = DRAFT_DIR / (ref or "UNKNOWN") / "errors"
                err_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(err_dir / f"error_{datetime.now():%H%M%S}.png"), full_page=True)
                print(f"  !!! ERROR: {err}")

            results.append({"BookingRef": ref, "Outcome": outcome, "Error": err})

            if i < len(rows):
                input("  Press Enter to close this window and move on to the next booking... ")
            else:
                input("  Press Enter to close the browser and finish. ")

            ctx.close()

        browser.close()

    with open(results_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["BookingRef", "Outcome", "Error"])
        w.writeheader()
        w.writerows(results)

    print("\n" + "=" * 70)
    print(f"Done. {len(results)} row(s) processed. Results: {results_path}")
    print(f"Draft screenshots & HTML snapshots: {DRAFT_DIR}")


if __name__ == "__main__":
    main()
