"""
run_all.py  -  Stage 1: mail_dropbox  ->  Stage 2: airfrance_claim_v5

Stage 1 (mail_dropbox.run): finds the last Assignment_Form_<CASE>_<name>.pdf
email, creates Claims\\<CASE>.<Name>, downloads all attachments there and builds
the letter from letter_template.docx (XXX -> sender name).

Stage 2 (airfrance_claim_v5): fills the Air France claim wizard for the row of
info.csv that belongs to that sender, with two changes:
  * "Files" (Step 2 upload) = the PDF files downloaded in stage 1
    (the "Files" column of info.csv is ignored / may be missing)
  * Step 2 reason text = the text of the generated letter (letter_template.docx
    after the name replacement), instead of the fixed Delay/Cancel/MissConn text

Neither original script needs to be edited. Put run_all.py anywhere and run:
    python run_all.py [path\\to\\info.csv] [--headless]
It never clicks "Submit claim" (airfrance_claim_v5 stops on the review page).
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

# Folders that hold the two scripts (edit if you moved them)
MAIL_DIR = Path(r"C:\Coding\APPLY_AIRLINE\AirFrancePipeLine")
AF_DIR = Path(r"C:\Coding\APPLY_AIRLINE\AirFrancePipeLine")
# The folder of this script is searched first, then the two folders above
sys.path[:0] = [str(Path(__file__).resolve().parent), str(MAIL_DIR), str(AF_DIR)]

import mail_dropbox as md              # noqa: E402
import airfrance_claim_v5 as af        # noqa: E402

if not hasattr(md, "run"):
    sys.exit(
        f"Python loaded an OLD mail_dropbox.py:\n  {getattr(md, '__file__', '?')}\n"
        "It has no run() function. Replace that file with the new mail_dropbox.py "
        "(or copy the new one next to run_all.py) and start again."
    )
from docx import Document              # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# Air France's textbox may have a length limit; longer letters are cut here.
REASON_MAX_CHARS = 3999


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return " ".join(sorted(re.sub(r"[^a-z0-9]+", " ", s).split()))


def find_row(rows: list[dict], names: list[str]) -> dict:
    wanted = {norm(n) for n in names if n}
    for row in rows:
        full = f"{row.get('FirstName', '')} {row.get('LastName', '')}"
        if norm(full) in wanted or norm(row.get("PaxName", "")) in wanted:
            return row
    # Never guess: filling the form for the wrong booking would be worse than stopping.
    in_csv = ", ".join(
        f"{r.get('FirstName1', '')} {r.get('LastName1', '')} ({r.get('BookingRef', '')})"
        for r in rows
    )
    sys.exit(
        "No row in info.csv matches the names from the email.\n"
        f"  Names tried (sender + names in the Assignment_Form file names): {names}\n"
        f"  Rows in info.csv: {in_csv}\n"
        "Add a row for this passenger (FirstName/LastName) to info.csv and run again."
    )


def letter_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text.strip() for p in md.iter_paragraphs(doc) if p.text.strip()]
    text = "\n".join(parts)
    if len(text) > REASON_MAX_CHARS:
        print(f"  WARNING: letter has {len(text)} chars; cutting to {REASON_MAX_CHARS}.")
        text = text[:REASON_MAX_CHARS]
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file", nargs="?", default=af.DEFAULT_CSV)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    # ---------------- Stage 1: mail -> folder -> letter ----------------
    print("=== Stage 1: Gmail -> Dropbox -> letter ===")
    info = md.run()
    pdfs = [str(p) for p in info["attachments"] if p.suffix.lower() == ".pdf"]
    if not pdfs:
        sys.exit("The email had no PDF attachments to upload to Air France.")
    reason = letter_text(info["letter"])
    if not reason:
        sys.exit(f"The letter {info['letter']} has no text to use as the reason.")

    # ---------------- Stage 2: Air France form ----------------
    print("\n=== Stage 2: Air France claim form ===")
    rows = af.load_rows(args.csv_file)
    if not rows:
        sys.exit("info.csv has no data rows.")
    row = dict(find_row(rows, info["names"]))
    row["Files"] = "|".join(pdfs)                      # downloaded PDFs
    af.get_reason_for_status = lambda status: reason   # letter text = reason
    print(f"Row: {row.get('BookingRef')}  {row.get('FirstName')} {row.get('LastName')}"
          f"  Flight={row.get('FlightNumber')}  Status={row.get('Status')}")
    print(f"Files ({len(pdfs)}): " + " | ".join(Path(p).name for p in pdfs))

    af.DRAFT_DIR = Path(af.DEFAULT_DRAFTS_DIR)
    af.DRAFT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            slow_mo=af.SLOW_MO,
            args=af.CHROME_LAUNCH_ARGS + [f"--user-agent={af.CHROME_USER_AGENT}"],
        )
        ctx = browser.new_context(
            locale="en-CA", viewport={"width": 1366, "height": 900},
            user_agent=af.CHROME_USER_AGENT,
        )
        ctx.set_default_timeout(af.TIMEOUT)
        page = ctx.new_page()
        try:
            outcome = af.process_row(page, row)
            print(f"\n>>> {outcome}: form filled, NOT submitted. Check it in the browser.")
        except Exception as e:
            print(f"\n!!! ERROR: {e}")
        input("Press Enter to close the browser and finish... ")
        ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
