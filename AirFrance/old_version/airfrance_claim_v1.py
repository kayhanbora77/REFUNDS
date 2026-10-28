"""
Air France delay/cancellation compensation claim filler (Playwright).

Usage:
    pip install playwright
    playwright install chromium
    python airfrance_claim.py            # reads the default CSV below

The script NEVER clicks the final Submit button. For each booking it fills the
whole form up to the review/submit page, saves a screenshot to ./drafts/, and
leaves the browser window open so you can check it and submit by hand. Press
Enter in the terminal to move to the next booking (that closes the window, and
any claim you did not submit yourself is discarded).

Default input: C:/Coding/REFUNDS/AirFrance/info.csv (tab- or comma-separated, auto-detected)

Columns used:
    BookingRef, PaxName, FlightNumber, Status
    - PaxName "DANIELLE COPLEY" -> FirstName "DANIELLE", LastName "COPLEY"
      (first word = first name, the rest = last name; FirstName/LastName
      columns are used instead if present)
    - Status: "Delay" or "Cancel"/"Cancelled"/"Canceled" (spaces are trimmed)

Optional CSV columns used to fill the remaining steps (add whatever the form asks for):
    Email, Phone, Address, City, PostalCode, Country,
    AccountHolder, IBAN, BIC, FlightDate

If the site shows a CAPTCHA or an unexpected page, the script pauses and lets you
solve/handle it in the browser window, then press Enter in the terminal to continue.
It does not try to bypass any bot protection.
"""
import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

CLAIM_URL = "https://wwws.airfrance.ca/en/claim"
DEFAULT_CSV = r"C:\Coding\REFUNDS\AirFrance\info.csv"
TIMEOUT = 15_000

# Extra form fields: (CSV column, regex matched against the field's label/placeholder)
EXTRA_FIELDS = [
    ("Email", r"e-?mail"),
    ("Phone", r"phone|mobile"),
    ("Address", r"address|street"),
    ("City", r"city|town"),
    ("PostalCode", r"postal|zip|post code"),
    ("AccountHolder", r"account holder|name of (the )?account|beneficiary"),
    ("IBAN", r"iban|account number"),
    ("BIC", r"bic|swift"),
]


# ----------------------------------------------------------------- helpers
def pause(msg: str):
    print(f"\n[ACTION NEEDED] {msg}")
    input("Press Enter here when done... ")


def click_text(page: Page, pattern: str, role: str | None = None, timeout=TIMEOUT):
    """Click the first visible element whose text matches the regex."""
    rx = re.compile(pattern, re.I)
    candidates = []
    if role:
        candidates.append(page.get_by_role(role, name=rx))
    candidates += [
        page.get_by_role("button", name=rx),
        page.get_by_role("radio", name=rx),
        page.get_by_role("link", name=rx),
        page.get_by_label(rx),
        page.get_by_text(rx),
    ]
    for loc in candidates:
        try:
            loc.first.wait_for(state="visible", timeout=timeout // len(candidates))
            loc.first.click()
            return
        except PWTimeout:
            continue
    raise PWTimeout(f"Could not find clickable element matching /{pattern}/")


def click_continue(page: Page):
    click_text(page, r"^\s*(continue|next)\s*$", role="button")
    page.wait_for_load_state("networkidle")


def fill_by_label(page: Page, pattern: str, value: str) -> bool:
    rx = re.compile(pattern, re.I)
    for loc in (page.get_by_label(rx), page.get_by_placeholder(rx)):
        try:
            el = loc.first
            el.wait_for(state="visible", timeout=1500)
            if el.input_value() == "":
                el.fill(value)
            return True
        except (PWTimeout, Exception):
            continue
    return False


def normalise_flight(fn: str) -> str:
    return re.sub(r"\s+", "", fn).upper()


def maybe_handle_cookie_banner(page: Page):
    for name in (r"accept all", r"accept", r"agree", r"ok"):
        try:
            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
            btn.wait_for(state="visible", timeout=1500)
            btn.click()
            return
        except PWTimeout:
            continue


def captcha_present(page: Page) -> bool:
    return page.locator("iframe[src*='captcha'], iframe[title*='captcha' i], .g-recaptcha, .h-captcha").count() > 0


# ----------------------------------------------------------------- steps
def step1_search(page: Page, row: dict):
    page.goto(CLAIM_URL, wait_until="domcontentloaded")
    maybe_handle_cookie_banner(page)
    if not fill_by_label(page, r"booking (reference|code)|reservation|reference", row["BookingRef"]):
        raise RuntimeError("Booking reference field not found")
    if not fill_by_label(page, r"first name|given name", row["FirstName"]):
        raise RuntimeError("First name field not found")
    if not fill_by_label(page, r"last name|family name|surname", row["LastName"]):
        raise RuntimeError("Last name field not found")
    if captcha_present(page):
        pause("CAPTCHA detected. Solve it in the browser.")
    click_text(page, r"^\s*search\s*$", role="button")
    page.wait_for_load_state("networkidle")


def step2_select_flight(page: Page, row: dict):
    fn = normalise_flight(row["FlightNumber"])
    digits = re.sub(r"\D", "", fn)
    # Match "AF1234", "AF 1234" or just "1234" in the flight list
    rx = re.compile(rf"(AF\s*)?0*{digits}\b", re.I)
    target = page.get_by_text(rx).first
    try:
        target.wait_for(state="visible", timeout=TIMEOUT)
    except PWTimeout:
        raise RuntimeError(f"Flight {row['FlightNumber']} not listed for this booking")
    target.click()
    click_continue(page)


def step3_disruption_type(page: Page):
    click_text(page, r"flight disruptions? and missed connections?")
    # some layouts need a Continue, others advance automatically
    try:
        click_continue(page)
    except PWTimeout:
        pass


def step4_status(page: Page, row: dict):
    status = row["Status"].strip().lower()
    if status.startswith("delay"):
        click_text(page, r"flight delayed")
    elif status.startswith("cancel"):
        click_text(page, r"flight cancell?ed")
    else:
        raise RuntimeError(f"Unknown Status value: {row['Status']!r}")
    try:
        click_continue(page)
    except PWTimeout:
        pass


def step5_compensation(page: Page):
    # "Cache" in the request is treated as cash / bank transfer
    click_text(page, r"\bcash\b|bank transfer|cache")
    # Terms of Use checkbox
    cb = page.get_by_label(re.compile(r"accept the terms of use", re.I)).first
    try:
        cb.wait_for(state="visible", timeout=TIMEOUT)
        cb.check()
    except PWTimeout:
        click_text(page, r"I accept the Terms of Use")
    click_continue(page)


def step6_remaining(page: Page, row: dict):
    """Fill whatever steps remain, using the optional CSV columns, until Submit."""
    for _ in range(8):  # safety cap on number of pages
        for col, pattern in EXTRA_FIELDS:
            val = (row.get(col) or "").strip()
            if val:
                fill_by_label(page, pattern, val)

        submit_btn = page.get_by_role("button", name=re.compile(r"^\s*(submit|send|confirm)( claim| request)?\s*$", re.I))
        if submit_btn.count() > 0 and submit_btn.first.is_visible():
            # Final submit page reached: do NOT click. Leave it for manual review.
            Path("drafts").mkdir(exist_ok=True)
            page.screenshot(path=f"drafts/{row['BookingRef']}.png", full_page=True)
            return "ready-for-review"

        # Required fields we could not fill -> let the human handle them
        invalid = page.locator("[aria-invalid='true'], input:invalid")
        try:
            click_continue(page)
        except PWTimeout:
            pause("Could not find Continue/Submit on this page. Complete it manually.")
            continue
        if invalid.count() > 0 and page.locator("[aria-invalid='true']").count() > 0:
            pause("Some required fields look empty/invalid. Fill them in the browser.")
    raise RuntimeError("Ran out of steps before reaching Submit")


# ----------------------------------------------------------------- csv
def load_rows(path: str) -> list[dict]:
    """Read a tab- or comma-separated file, trim all values, derive First/LastName from PaxName."""
    for enc in ("utf-8-sig", "utf-16", "cp1254", "latin-1"):
        try:
            text = Path(path).read_text(encoding=enc)
            break
        except (UnicodeError, UnicodeDecodeError):
            continue
    else:
        sys.exit(f"Cannot decode {path}")

    first_line = text.splitlines()[0] if text else ""
    delim = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delim)

    rows = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not row.get("FirstName") or not row.get("LastName"):
            parts = row.get("PaxName", "").split()
            if len(parts) >= 2:
                row["FirstName"], row["LastName"] = parts[0].title(), " ".join(parts[1:]).title()
            elif len(parts) == 1:
                row["FirstName"], row["LastName"] = "", parts[0].title()
        if row.get("BookingRef", "").upper() == "NULL":
            row["BookingRef"] = ""
        rows.append(row)
    return rows


# ----------------------------------------------------------------- main
def process_row(page: Page, row: dict) -> str:
    step1_search(page, row)
    step2_select_flight(page, row)
    step3_disruption_type(page)
    step4_status(page, row)
    step5_compensation(page)
    return step6_remaining(page, row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file", nargs="?", default=DEFAULT_CSV)
    args = ap.parse_args()

    rows = load_rows(args.csv_file)
    if not rows:
        sys.exit("CSV has no data rows")

    required = {"BookingRef", "FirstName", "LastName", "FlightNumber", "Status"}
    missing = required - set(rows[0].keys())
    if missing:
        sys.exit(f"CSV is missing columns (or PaxName): {', '.join(sorted(missing))}")

    out_path = Path(args.csv_file).with_name(f"claim_results_{datetime.now():%Y%m%d_%H%M%S}.csv")
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=150)  # visible: you review it
        for i, row in enumerate(rows, 1):
            ctx = browser.new_context(locale="en-CA")
            ctx.set_default_timeout(TIMEOUT)
            page = ctx.new_page()
            ref = row["BookingRef"]
            print(f"[{i}/{len(rows)}] {ref} {row['FirstName']} {row['LastName']}")
            try:
                outcome = process_row(page, row)
                err = ""
                print("  Form filled and left UNSUBMITTED. Check it in the browser window.")
            except Exception as e:  # keep going with the next booking
                outcome, err = "error", str(e)
                page.screenshot(path=f"error_{ref}.png")
                print(f"  ERROR: {err}")
            results.append({"BookingRef": ref, "Outcome": outcome, "Error": err})
            input("  Review/submit manually if OK, then press Enter for the next booking... ")
            ctx.close()

        browser.close()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["BookingRef", "Outcome", "Error"])
        w.writeheader()
        w.writerows(results)
    print(f"\nDone. Results written to {out_path}")


if __name__ == "__main__":
    main()