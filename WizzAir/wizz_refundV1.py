#!/usr/bin/env python3
"""
Wizz Air Refund Assistant — browser-assisted, review-first workflow.

This tool:
- Reads booking cases from CSV.
- Validates basic case data.
- Routes direct Wizz Air bookings to the official Wizz Air refund page.
- Routes agency bookings to a manual agency follow-up workflow.
- Creates a refund-request draft for valid direct bookings.
- Opens a visible Playwright browser; the user signs in and navigates manually.
- NEVER clicks a final refund/submit/confirm button.

No passwords, payment details, or one-time codes are collected by this script.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Error as PlaywrightError

# All paths are anchored to the script's own folder so the tool behaves the
# same whether it is launched from here or from the workspace root.
BASE_DIR = Path(__file__).resolve().parent
INPUT_CSV = BASE_DIR / "wizz_refunds.csv"
OUTPUT_CSV = BASE_DIR / "refund_results.csv"
DRAFT_DIR = BASE_DIR / "refund_requests"
PROFILE_DIR = BASE_DIR / ".playwright_profile"

REFUND_URL = (
    "https://www.wizzair.com/en-gb/help-centre/"
    "delay-cancellation-and-refund/cancelled-flight/"
    "cancelled-flight-what-to-do"
)

REQUIRED_COLUMNS = [
    "case_id",
    "booking_reference",
    "flight_number",
    "departure_date",
    "booking_channel",
    "cancellation_by_airline",
    "agency_name",
]

VALID_CHANNELS = {"direct", "agency"}
TRUE_VALUES = {"true", "yes", "1", "y"}
FALSE_VALUES = {"false", "no", "0", "n"}


def clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_bool(value: Any) -> bool | None:
    value = clean(value).lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return None


def valid_reference(value: str) -> bool:
    # Typical Wizz Air booking confirmation codes are six characters.
    # This is a format check only, not proof the booking exists.
    return bool(re.fullmatch(r"[A-Z0-9]{6}", value.upper()))


def valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return cleaned[:80] or "case"


def make_draft(row: dict[str, str]) -> str:
    ref = clean(row.get("booking_reference")).upper()
    flight = clean(row.get("flight_number")).upper()
    departure = clean(row.get("departure_date"))
    return f"""Subject: Request for refund to original payment method — cancelled flight {flight}

Dear Wizz Air Customer Service,

I am contacting you about the following booking:

Booking reference: {ref}
Flight number: {flight}
Scheduled departure date: {departure}

I understand that this flight was cancelled by the airline. I would like to
request a refund to the original form of payment, if this option is available
for my booking.

Please confirm the refund amount, the applicable refund method, and the
expected processing time. Please let me know if you need any further
information.

Kind regards
"""


def process_case(row: dict[str, str]) -> dict[str, str]:
    case_id = clean(row.get("case_id"))
    ref = clean(row.get("booking_reference")).upper()
    flight = clean(row.get("flight_number")).upper()
    departure = clean(row.get("departure_date"))
    channel = clean(row.get("booking_channel")).lower()
    airline_cancelled = normalize_bool(row.get("cancellation_by_airline"))
    agency_name = clean(row.get("agency_name"))

    errors: list[str] = []
    if not case_id:
        errors.append("Missing case_id")
    if not valid_reference(ref):
        errors.append("Booking reference should be six letters/numbers (format check only)")
    if not flight:
        errors.append("Missing flight_number")
    if not valid_date(departure):
        errors.append("departure_date must use YYYY-MM-DD")
    if channel not in VALID_CHANNELS:
        errors.append("booking_channel must be direct or agency")
    if airline_cancelled is None:
        errors.append("cancellation_by_airline must be yes/no")
    if channel == "agency" and not agency_name:
        errors.append("agency_name is required for agency bookings")

    result = {
        "case_id": case_id,
        "booking_reference": ref,
        "flight_number": flight,
        "departure_date": departure,
        "booking_channel": channel,
        "status": "",
        "next_action": "",
        "draft_file": "",
        "errors": "; ".join(errors),
    }

    if errors:
        result["status"] = "INVALID_INPUT"
        result["next_action"] = "Correct the input CSV and rerun"
        return result

    if channel == "agency":
        result["status"] = "AGENCY_MANUAL_ACTION"
        result["next_action"] = (
            f"Contact booking provider ({agency_name}) and request a refund "
            "to the original payment method; do not submit through Wizz Air "
            "unless the agency or Wizz Air instructs you to."
        )
        return result

    if airline_cancelled is False:
        result["status"] = "ELIGIBILITY_REVIEW"
        result["next_action"] = (
            "Cancellation by airline is not confirmed. Check the booking terms "
            "and official options; no refund entitlement is assumed."
        )
        return result

    if airline_cancelled is None:
        result["status"] = "ELIGIBILITY_REVIEW"
        result["next_action"] = "Confirm who cancelled the flight"
        return result

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFT_DIR / f"{safe_filename(case_id)}_{safe_filename(ref)}.txt"
    draft_path.write_text(make_draft(row), encoding="utf-8")

    result["status"] = "READY_FOR_MANUAL_REVIEW"
    result["next_action"] = (
        "Open the official Wizz Air page, sign in manually, verify the booking "
        "and select the cash/original-payment option if offered. This tool "
        "will not submit the request."
    )
    result["draft_file"] = str(draft_path)
    return result


def read_cases(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Input file not found: {path.resolve()}\n"
            f"Create wizz_refunds.csv in {BASE_DIR} and edit it "
            "(see README.md for the required columns)."
        )

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header row.")
        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames)
        if missing:
            raise ValueError("Missing CSV columns: " + ", ".join(sorted(missing)))
        return [dict(row) for row in reader]


def write_results(rows: list[dict[str, str]], path: Path) -> None:
    fields = [
        "case_id", "booking_reference", "flight_number", "departure_date",
        "booking_channel", "status", "next_action", "draft_file", "errors"
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def open_direct_case_in_browser(case: dict[str, str], headless: bool = False) -> None:
    """Open official Wizz Air help page; do not interact with form controls."""
    print("\n" + "=" * 72)
    print(f"Case: {case['case_id']} | Booking: {case['booking_reference']}")
    print(f"Flight: {case['flight_number']} | Departure: {case['departure_date']}")
    print("Before proceeding, confirm this is the correct booking.")
    print("The browser will open the official Wizz Air refund instructions.")
    print("Sign in yourself. Do not share your password or one-time codes.")
    input("Press Enter to open the browser, or Ctrl+C to stop... ")

    try:
        with sync_playwright() as p:
            # Persistent profile keeps the user's own browser session locally.
            # Do not sync/share this directory; it may contain session cookies.
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR.resolve()),
                headless=headless,
                viewport={"width": 1365, "height": 900},
                accept_downloads=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(REFUND_URL, wait_until="domcontentloaded", timeout=60000)

            print("\nBrowser opened.")
            print("Manual steps:")
            print("  1. Sign in on the official Wizz Air website if required.")
            print("  2. Open the correct booking and verify flight cancellation.")
            print("  3. Review the refund amount and choose original-payment refund if offered.")
            print("  4. Submit yourself only after checking all details.")
            print("  5. Close the browser when finished.")
            print("\nSafety: this program will not click Submit, Refund, Confirm, or Pay.")
            input("Press Enter here when you have finished reviewing the page... ")
            context.close()

    except PlaywrightError as exc:
        print(f"Playwright/browser error: {exc}", file=sys.stderr)
        print("You can open the official page manually:", REFUND_URL)
        webbrowser.open(REFUND_URL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Review-first Wizz Air refund assistant")
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open a visible browser for each eligible direct Wizz Air booking",
    )
    parser.add_argument(
        "--case-id",
        help="Process/open only the specified case_id",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=INPUT_CSV,
        help=f"Cases CSV to read (default: {INPUT_CSV})",
    )
    args = parser.parse_args()

    try:
        cases = read_cases(args.csv)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2

    results = [process_case(case) for case in cases]
    write_results(results, OUTPUT_CSV)

    print(f"Cases read: {len(cases)}")
    print(f"Results written: {OUTPUT_CSV.resolve()}")
    print(f"Draft folder: {DRAFT_DIR.resolve()}")
    print("\nStatus summary:")
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    if args.open_browser:
        eligible = [
            (case, result)
            for case, result in zip(cases, results)
            if result["status"] == "READY_FOR_MANUAL_REVIEW"
        ]
        if args.case_id:
            eligible = [
                pair for pair in eligible
                if pair[0]["case_id"] == args.case_id
            ]
        if not eligible:
            print("\nNo eligible direct-booking cases to open.")
        for case, _result in eligible:
            open_direct_case_in_browser(case)

    print("\nNo refund was submitted by this program.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
