#!/usr/bin/env python3
"""
Wizz Air claim AUTOFILL — fills the form from CSV, then STOPS for manual review.

Flow:
  1. Reads a case from wizz_refunds.csv (same file as the refund assistant).
  2. Opens a visible Chromium window (persistent local profile).
  3. YOU sign in and navigate to the claim form yourself.
  4. Press Enter in the terminal -> script fills the fields it can find,
     highlights them in yellow, saves a screenshot, and reports what it
     could NOT find.
  5. YOU check everything and press Submit yourself.

This script never clicks Submit / Send / Confirm / Pay. It only types into
fields, picks dropdown values and (optionally) attaches a proof file.

Usage:
  python wizz_autofill.py --case-id C001
  python wizz_autofill.py --case-id C001 --url "https://wizzair.com/..."
  python wizz_autofill.py --case-id C001 --dump-fields   # list form fields (to tune FIELD_MAP)

Optional CSV columns (extra to the refund assistant ones):
  first_name, last_name, email, phone, departure_airport, arrival_airport,
  scheduled_arrival, actual_arrival, delay_minutes, comment, proof_file
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
INPUT_CSV = BASE_DIR / "wizz_refunds.csv"
PROFILE_DIR = BASE_DIR / ".playwright_profile"
SHOT_DIR = BASE_DIR / "autofill_screenshots"

START_URL = "https://www.wizzair.com/en-gb/help-centre"

# CSV column -> regexes matched against the field's label / placeholder / aria-label.
# I could not inspect the live form, so these are best guesses. Run with
# --dump-fields on the real form and edit the patterns to match.
FIELD_MAP: dict[str, list[str]] = {
    "first_name": [r"first\s*name", r"given\s*name"],
    "last_name": [r"last\s*name", r"surname", r"family\s*name"],
    "email": [r"e-?mail"],
    "phone": [r"phone", r"mobile"],
    "booking_reference": [r"booking\s*(reference|code|number)", r"confirmation\s*code", r"PNR"],
    "flight_number": [r"flight\s*(number|no)"],
    "departure_date": [r"(flight|departure)\s*date", r"date\s*of\s*(flight|departure)"],
    "departure_airport": [r"departure\s*(airport|city)", r"\bfrom\b"],
    "arrival_airport": [r"arrival\s*(airport|city)", r"destination", r"\bto\b"],
    "scheduled_arrival": [r"scheduled\s*arrival"],
    "actual_arrival": [r"actual\s*arrival", r"arrival\s*time"],
    "delay_minutes": [r"delay\s*(duration|time|length)"],
    "comment": [r"comment", r"message", r"describe", r"additional\s*(info|details)"],
}


def clean(v) -> str:
    return str(v or "").strip()


def load_case(csv_path: Path, case_id: str) -> dict[str, str]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if clean(row.get("case_id")) == case_id:
                return {k: clean(v) for k, v in row.items() if k}
    raise SystemExit(f"case_id {case_id!r} not found in {csv_path}")


def all_frames(page):
    return [page.main_frame, *[f for f in page.frames if f != page.main_frame]]


def find_control(page, patterns: list[str]):
    """Return (locator, kind) for the first visible matching input/select/textarea."""
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for frame in all_frames(page):
            for getter in (
                lambda: frame.get_by_label(rx),
                lambda: frame.get_by_placeholder(rx),
                lambda: frame.locator("input,select,textarea").filter(
                    has=frame.locator(f"xpath=self::*[@aria-label]")
                ),
            ):
                try:
                    loc = getter()
                    for i in range(min(loc.count(), 3)):
                        el = loc.nth(i)
                        if el.is_visible() and el.is_enabled():
                            tag = el.evaluate("e => e.tagName.toLowerCase()")
                            if tag in ("input", "select", "textarea"):
                                return el, tag
                except PlaywrightError:
                    continue
    return None, None


def highlight(el) -> None:
    try:
        el.evaluate(
            "e => { e.style.outline='3px solid #f5c400'; e.style.background='#fffbe0'; }"
        )
    except PlaywrightError:
        pass


def autofill(page, case: dict[str, str]) -> tuple[list[str], list[str]]:
    filled, missing = [], []
    for column, patterns in FIELD_MAP.items():
        value = case.get(column, "")
        if not value:
            continue
        el, tag = find_control(page, patterns)
        if el is None:
            missing.append(column)
            continue
        try:
            if tag == "select":
                try:
                    el.select_option(label=value)
                except PlaywrightError:
                    el.select_option(value=value)
            else:
                el.fill(value)
            highlight(el)
            filled.append(column)
        except PlaywrightError:
            missing.append(column)

    proof = case.get("proof_file", "")
    if proof:
        p = Path(proof)
        if not p.is_absolute():
            p = BASE_DIR / p
        if p.exists():
            attached = False
            for frame in all_frames(page):
                inputs = frame.locator("input[type=file]")
                if inputs.count():
                    inputs.first.set_input_files(str(p))
                    attached = True
                    break
            (filled if attached else missing).append("proof_file")
        else:
            missing.append(f"proof_file (file not found: {p})")
    return filled, missing


def dump_fields(page) -> None:
    print("\nVisible form controls (use these to tune FIELD_MAP):")
    for fi, frame in enumerate(all_frames(page)):
        for el in frame.locator("input,select,textarea").all():
            try:
                if not el.is_visible():
                    continue
                info = el.evaluate(
                    """e => ({tag:e.tagName.toLowerCase(), type:e.type||'', name:e.name||'',
                              id:e.id||'', ph:e.placeholder||'', aria:e.getAttribute('aria-label')||'',
                              label:(e.labels&&e.labels[0]?e.labels[0].innerText:'')})"""
                )
                print(f"  frame{fi}: {info}")
            except PlaywrightError:
                continue


def main() -> int:
    ap = argparse.ArgumentParser(description="Autofill Wizz Air claim form; never submits.")
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--csv", type=Path, default=INPUT_CSV)
    ap.add_argument("--url", default=START_URL, help="Page to open first")
    ap.add_argument("--dump-fields", action="store_true", help="List form fields instead of filling")
    args = ap.parse_args()

    case = load_case(args.csv, args.case_id)
    print(f"Case {args.case_id}: {case.get('booking_reference')} / {case.get('flight_number')} / {case.get('departure_date')}")
    SHOT_DIR.mkdir(exist_ok=True)

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport={"width": 1365, "height": 900},
                accept_downloads=False,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)

            print("\nIn the browser: sign in yourself and open the compensation/claim form.")
            print("Do NOT submit yet. Come back here when the empty form is on screen.")
            input("Press Enter to autofill (the active tab is used)... ")

            # Use whichever tab is currently frontmost / last opened.
            page = ctx.pages[-1]
            page.wait_for_load_state("domcontentloaded")

            if args.dump_fields:
                dump_fields(page)
            else:
                filled, missing = autofill(page, case)
                shot = SHOT_DIR / f"{args.case_id}_filled.png"
                page.screenshot(path=str(shot), full_page=True)
                print(f"\nFilled  : {', '.join(filled) or '-'}")
                print(f"Not found: {', '.join(missing) or '-'}  (fill these by hand or edit FIELD_MAP)")
                print(f"Screenshot: {shot}")
                print("\nFilled fields are highlighted yellow. Multi-step form? Press Next yourself,")
                print("then run again or fill the next page by hand.")

            print("\nReview everything, then press Submit yourself in the browser.")
            input("Press Enter here to close the browser (this does NOT submit)... ")
            ctx.close()
    except PlaywrightError as exc:
        print(f"Browser error: {exc}", file=sys.stderr)
        return 1

    print("No form was submitted by this program.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())