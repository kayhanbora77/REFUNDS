# Air France Claim Filler

Automates filling the Air France delay/cancellation compensation form on
https://wwws.airfrance.ca/en/claim using data from a CSV file.

## Install

```cmd
pip install playwright
playwright install chromium
```

## Files

| File | Purpose |
|------|---------|
| `airfrance_claim.py` | Main automation script |
| `info_template.csv`  | Sample CSV – columns you can use |
| `info.csv`            | Your real input (place it at `C:\Coding\REFUNDS\AirFrance\info.csv`) |
| `drafts/<BookingRef>/` | Per-booking screenshots, HTML snapshots, JSON metadata (created at runtime) |
| `claim_results_*.csv` | Per-run outcome report (created at runtime) |

## CSV columns

**Required** (one per row):
- `BookingRef`
- `PaxName` *(e.g. `DANIELLE COPLEY` – first word = first name, rest = last name)*
   OR explicit `FirstName` + `LastName` columns
- `FlightNumber` *(e.g. `AF341` – matched loosely against the listed flights)*
- `Status` – `Delay` or `Cancel` / `Cancelled` / `Canceled`

**Optional** – used for the remaining wizard steps; if blank, sensible mock data
is generated automatically:
- `Email`, `Phone`, `Address`, `City`, `PostalCode`, `Country`
- `AccountHolder`, `IBAN`, `BIC`
- `FlightDate`, `FlightOrigin`, `FlightDestination`
- `DisruptionDate`, `DisruptionReason`

## What the script does

1. Opens the claim URL.
2. Fills `BookingRef`, `FirstName`, `LastName`, clicks **Search**.
3. Selects the row's `FlightNumber` from the list, clicks **Continue**.
4. Picks **Flight disruptions and missed connections**.
5. Picks **Flight delayed** (if `Status=Delay`) or **Flight canceled** (if `Status=Cancel`).
6. Picks **Compensation method = Cash**, ticks **I accept the Terms of Use**, clicks **Continue**.
7. Walks through the remaining wizard pages (contact details, flight details, bank details, review) filling each visible field from the CSV – or with mock data when a column is blank.
8. Stops on the final **review / submit** page WITHOUT clicking Submit.
9. Saves a full-page screenshot + HTML snapshot + JSON copy of the row to `drafts/<BookingRef>/`.
10. Waits for you to review/submit manually, then moves to the next booking.

## Run

```cmd
:: Default CSV at C:\Coding\REFUNDS\AirFrance\info.csv
python airfrance_claim.py

:: Or specify a CSV file
python airfrance_claim.py C:\path\to\your.csv
```

## Important

- The script NEVER clicks the final **Submit** button.
- If a CAPTCHA appears, it pauses and lets you solve it in the browser.
- If the page layout drifts, it pauses with an `[ACTION NEEDED]` message and waits for Enter.
- Keep the browser visible (default) so you can review each page.
