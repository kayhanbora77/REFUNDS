# Wizz Air Refund Assistant (review-first)

This project is a **browser-assisted refund workflow**, not a fully autonomous refund submitter.

## What it does

- Reads cases from `wizz_refunds.csv`.
- Validates basic fields and routes direct bookings vs. agency bookings.
- Creates a text draft for valid direct bookings where airline cancellation is marked `yes`.
- Writes a case summary to `refund_results.csv`.
- Optionally opens a visible Playwright Chromium browser on Wizz Air's official refund-help page.
- Stops for manual review. It never clicks a final Submit/Refund/Confirm button.

## Requirements

- Python 3.10+
- Chromium installed by Playwright

Install:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Prepare the CSV

Copy `wizz_refunds.example.csv` to `wizz_refunds.csv` and edit it.

Required columns:

- `case_id`: your internal unique case label
- `booking_reference`: booking confirmation code (format check only)
- `flight_number`
- `departure_date`: `YYYY-MM-DD`
- `booking_channel`: `direct` or `agency`
- `cancellation_by_airline`: `yes` or `no`
- `agency_name`: required for agency bookings

Use only data you are authorized to process. Do not add passwords, payment card numbers, passport numbers, or one-time codes to the CSV.

## Run

Process CSV and create drafts:

```bash
python wizz_refund.py
```

Open the browser for each valid direct booking marked as cancelled by the airline:

```bash
python wizz_refund.py --open-browser
```

Process only one case in browser mode:

```bash
python wizz_refund.py --open-browser --case-id CASE001
```

The program pauses in the terminal before opening each case. Sign in manually and verify the booking and refund option. The program does not automate form filling or final submission; you must perform any final action yourself.

## Agency bookings

The program does not guess agency login URLs or attempt to log into third-party websites. It flags agency bookings so you can use the original provider's official refund process.

## Security notes

- The script never asks for or stores your Wizz Air password.
- `.playwright_profile/` may contain browser session cookies. Keep it private and do not upload/share it. Delete it if you want to remove the locally stored browser profile.
- Do not run this against bookings you are not authorized to manage.
- A `READY_FOR_MANUAL_REVIEW` status is not a legal eligibility determination or guarantee of refund.
- Refund choices depend on the booking and current Wizz Air terms. Check the official page and cancellation notice before submitting.
