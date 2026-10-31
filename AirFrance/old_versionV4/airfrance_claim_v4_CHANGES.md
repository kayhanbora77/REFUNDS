# Air France Claim Filler - V4 Changes Summary

## Latest update (V4.2): Fixed option iteration limit (Turkey was unreachable)

### Root cause of Step 3 hang (continued from V4.1)

After V4.1 fixed the combobox-finding logic, the script still failed with:
```
! Dropdown opened but no option matched: ['^Turkey\\ \\(\\+90\\)$']
! Could not select country code='Turkey (+90)'
```

I reopened the live site in agent-browser and counted the options in the Country code dropdown:
- **Total options**: 252
- **Turkey position**: index **227** (alphabetically near the end — after Tajikistan, Tanzania, Thailand, Timor-Leste, Togo, Tonga, Trinidad and Tobago, Tunisia)

The old `select_combobox_option` had this line:
```python
for j in range(min(cnt, 200)):   # ← BUG: only iterates first 200 options
    o = opts.nth(j)
    txt = (o.inner_text() or "").strip()
    if opt_rx.search(txt):
        ...
```

**Turkey was at index 227 — past the 200-option limit. The loop stopped at Afghanistan–Suriname and never reached Turkey.** The same bug applied to the Home address Country dropdown (Turkey at index 227 of 248 options).

### Fix

Replaced the per-option Python iteration with a single `page.evaluate()` call that fetches ALL option texts + indices in one round-trip. The new logic:

1. **Single round-trip**: Get all visible options via `document.querySelectorAll('mat-option')` and return `[{index, text, id}, ...]` — no N round-trips to the browser.
2. **Three matching strategies** (in order):
   - **Exact regex match**: Try `opt_rx.search(text)` on each option's text
   - **Normalized match**: Replace all whitespace (including `\u00A0`) with single space, then retry the regex
   - **Substring fallback**: Strip `^...$` anchors from the pattern and do a `in` substring check
3. **No artificial cap**: Iterates ALL options (verified working with 252+ options).
4. **Single click**: Once the matching index is found, calls `page.locator("mat-option:visible").nth(match_idx).click()` — only one Playwright operation.
5. **Verbose logging**: Reports `Selected option (index=227): Turkey (+90)` so you can see exactly what was selected.

### Verification (live site)

I tested the new logic against the live Air France site using agent-browser, navigating all the way to Step 3:

| Test | Result |
|---|---|
| Country code dropdown opened | ✅ 252 options visible |
| Turkey found via regex `^Turkey\ \(\+90\)$` | ✅ Matched at index 227 |
| Click on `mat-option:visible.nth(227)` | ✅ Succeeded |
| Country code mat-select value after click | ✅ `Turkey (+90)` |
| Home address Country dropdown opened | ✅ 248 options visible |
| Turkey found at index 227 | ✅ Matched |
| Click succeeded | ✅ |
| Home country mat-select value after click | ✅ `Turkey` |

The script should now correctly select Turkey (+90) and Turkey on Step 3, then proceed to Step 4.

---

## V4.1 (previous): Fixed `select_combobox_option` for Air France's custom DOM

### Root cause

The Air France claim form uses a **custom Angular Material layout** where the field label is a SIBLING of the `bwc-form-select` wrapper, not inside `mat-form-field`. The DOM structure is:

```html
<div>
  <label class="bwc-form-field-label">Title</label>      <!-- label is OUTSIDE form-field -->
  <bwc-form-select>
    <mat-form-field>
      <mat-select>...</mat-select>                       <!-- old code looked here for label -->
    </mat-form-field>
  </bwc-form-select>
</div>
```

The old `select_combobox_option` searched `mat-form-field.innerText` and `aria-label` for the label text — both of which are empty in this layout. As a result, no mat-select ever matched, and all 4 combobox selections on Step 3 (Title, point of contact, country code, home country) failed silently.

### V4.1 fix

New `select_combobox_option` has two strategies:

1. **Primary (Air France-specific)**: Find `bwc-form-select:visible`, then read its sibling `<label class="bwc-form-field-label">` text. Match against `label_patterns`. If matched, click the `mat-select` inside the `bwc-form-select`.
2. **Fallback (generic Angular Material)**: Iterate all `mat-select:visible`, check `aria-label`, `aria-labelledby`, walk up to 6 ancestors looking for sibling labels, and finally check `mat-form-field.innerText`.

Plus the function now:
- **Skips if already has a value** — checks `.mat-mdc-select-value` innerText before clicking
- **Tries 4 click strategies** — `click()`, `click(force=True)`, click `.mat-mdc-select-trigger`, click `.mat-mdc-select-value`
- **Verifies dropdown opened** — waits for `mat-option:visible` after click; if not visible, tries next click strategy
- **Uses `page.locator`** for the option search (not `frame.locator`) — Angular Material appends the options overlay to `<body>`, outside the form's frame
- **Logs every step** — "Found mat-select for label /X/: '...'", "Selected option: '...'"

### V4.1 also fixed

- **`step6_wizard` retry loop**: Was silently looping up to 12 times when a handler failed. Now tracks per-step retries (`MAX_RETRIES_PER_STEP = 2`) and pauses for manual intervention if a step fails twice in a row.
- **`wizard_step3_personal_info`**: Changed `fill_field` calls for email and phone to use `only_empty=False` (so retries don't falsely report "could not fill").
- **Iteration logging**: Each iteration now prints `(iteration N, step=X, attempt M/2)` so you can see retry progress.

---

## Original V4 changes (preserved for context)

### What was broken in V3

The V3 script's `step6_remaining_wizard` function was too generic. It tried to match form fields by regex against all identifying attributes (id, name, placeholder, aria-label, label text), but:

1. **Wrong radio button logic** — The loop `for rp in (r"i have not contacted", r"\byes\b")` would have clicked **"Yes"** for "Would you like to claim additional expenses?" — but your instructions say **"No"**.

2. **No mat-select support** — Angular Material `mat-select` dropdowns (Title, point of contact, country code, country) need a two-step interaction: click to open the dropdown, then click the option. The V3 `select_field` helper only handled native `<select>` elements.

3. **No "Flight delayed" handler** — The wizard's sub-category step (Flight delayed / Flight canceled) wasn't being clicked. The V3 `step4_status` function tried `choose_radio` or `click_text` but the actual UI uses SELECT buttons, not radios.

4. **No bank form layout detection** — When you select Turkey as the bank country, the form dynamically switches to a simpler layout (just Account holder name, Address, City, IBAN — no BIC/SWIFT/bank name). The V3 script's generic field matching didn't account for this.

5. **No "opt-out" checkbox handler** — The V3 script's `for i in range(...checkboxes...)` loop tried to check *every* unchecked checkbox, which is dangerous. It could check the wrong checkbox or skip the right one.

6. **Stuck at Step 2** — Because the V3 script never clicked "No" radio on the Expenses step, the form's required-field validation prevented `Continue` from advancing. The script kept looping (10 iterations) without progress, then saved a draft and reported success incorrectly.

### New functions in V4

| Function | Purpose |
|---|---|
| `select_combobox_option(frame, page, label_patterns, option_patterns)` | Handles Angular `mat-select` dropdowns. **(V4.1 — finds sibling label via `bwc-form-select`. V4.2 — no option count limit, 3 matching strategies.)** |
| `check_checkbox_by_label(frame, patterns)` | Checks a checkbox by matching its label/aria-label against regex patterns. |
| `detect_wizard_step(page)` | Reads page text and returns which wizard step (2, 3, 4, 5, or 99 for review page) is currently visible. |
| `wizard_step2_expenses` | Clicks the **"No"** radio (with fallbacks), fills the context textbox with mock text, clicks Continue. |
| `wizard_step3_personal_info` | Selects Title (Mr.), point of contact (FirstName+LastName with I/J variant matching), fills email, selects Turkey (+90) country code, fills phone, selects Turkey for home address, Continue. |
| `wizard_step4_passengers` | Finds the passenger checkbox matching FirstName+LastName (tries "LAST FIRST" and "FIRST LAST" orderings + I/J spelling variants). |
| `wizard_step5_bank_details` | Selects Turkey as bank country (which switches form layout), waits for re-render, fills account holder name/address/city/IBAN, then checks "I do not want to provide my bank details online". |
| `step6_wizard` | Replaces V3's `step6_remaining_wizard`. **(V4.1 — bails out after 2 retries per step instead of looping 12 times.)** |
| `mock_iban_tr()` | Generates a mock Turkish IBAN: `TR` + 24 random digits. |

### Other improvements

- **Anti-bot Chrome flags**: `--disable-quic` + custom user-agent string.
- **Cross-platform paths**: Default CSV path is `info.csv` (relative). Default drafts dir is `./drafts`.
- **`--headless` flag**: Lets you run on servers without a GUI.
- **I/J spelling variants**: The Air France booking system may store names differently from your CSV (e.g., "AVRAI" in CSV vs "AVRAJ" in booking).
- **Existing-claim modal handling**: If a prior claim exists for the same flight+category.
- **Better diagnosis**: `diagnose()` now includes `mat-select` and `[role=combobox]` elements in the dump.

## Usage

```bash
pip install playwright
playwright install chromium

# Visible browser (default)
python airfrance_claim_v4.py info.csv

# Headless mode
python airfrance_claim_v4.py info.csv --headless

# Custom drafts directory
python airfrance_claim_v4.py info.csv --drafts-dir C:\Coding\REFUNDS\AirFrance\drafts

# Slow down for debugging (default 120ms)
python airfrance_claim_v4.py info.csv --slow-mo 300
```

## CSV format

Required columns: `BookingRef`, `FirstName`, `LastName`, `FlightNumber`, `Status`

Optional columns (filled with mock data if missing): `Title`, `Email`, `Phone`, `PhoneCountryCode`, `Country`, `Address`, `City`, `AccountHolder`, `IBAN`, `DisruptionReason`

## Output

- **Drafts**: `<drafts-dir>/<BookingRef>/review_<timestamp>.png` (+ `.html` + `.json`)
- **Diagnostics**: `<drafts-dir>/diagnostics/diag_<timestamp>.png` (+ `.html`)
- **Results CSV**: written next to input CSV as `claim_results_<timestamp>.csv`

## Important note

The script **never** clicks the "Submit claim" button. When it reaches the review page, it saves a draft screenshot and exits, leaving the form open in the browser for your manual review. Replace all mock values (email, phone, IBAN, address, etc.) with the passenger's real data before submitting.
