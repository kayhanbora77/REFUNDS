"""
run_all.py  -  v2: Dropbox folder -> Air France claim

WHAT CHANGED FROM v1
--------------------
* Gmail is gone.  No credentials.json, no token.json, no
  "find the last Assignment_Form email" step.  The whole Gmail
  branch of mail_dropbox.py is simply not called any more.
* Attachments are now read from a Dropbox folder whose name is
  "CaseId.FirstName LastName"  (e.g. "33531.Kayhan Bora").
  The folder is looked up under the local "Agency Data/Claims"
  sync directory first; if that isn't synced on this machine the
  script falls back to the Dropbox API and mirrors the folder
  into a temp dir.
* The letter is still built from letter_template.docx using the
  info.csv row that matches CaseId (preferred) or FirstName+LastName.
  If a letter .docx is already in the folder (from a previous run),
  it is reused instead of being regenerated.
* Stage 2 is unchanged: airfrance_claim_v5 fills the wizard with
  the PDFs from the folder as "Files" and the letter text as the
  Step-2 reason.  The form is never submitted - the script stops
  on the review page.

Neither original script needs to be edited.  Put run_all.py next
to them (or anywhere on PYTHONPATH) and run:

    python run_all.py [path\\to\\info.csv] [--folder "33531.Kayhan Bora"] [--headless]

If --folder is omitted, the script lists every folder under the
Claims root that looks like "CaseId.FirstName LastName" and asks
you which one to process.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

# Folders that hold the two scripts (edit if you moved them)
MAIL_DIR = Path(r"C:\Coding\APPLY_AIRLINE\AirFrancePipeLine")
AF_DIR = Path(r"C:\Coding\APPLY_AIRLINE\AirFrancePipeLine")
# The folder of this script is searched first, then the two folders above
sys.path[:0] = [str(Path(__file__).resolve().parent), str(MAIL_DIR), str(AF_DIR)]

import mail_dropbox as md              # noqa: E402
import airfrance_claim_v5 as af        # noqa: E402

# Sanity-check that mail_dropbox.py exposes the helpers we reuse.
_missing = [n for n in (
    "build_replacements", "build_letter", "iter_paragraphs",
    "find_claims_folder", "get_dropbox_client", "DROPBOX_REMOTE_CLAIMS",
    "TEMPLATE_DOCX", "safe_filename", "OLD_NAME",
) if not hasattr(md, n)]
if _missing:
    sys.exit(
        f"Python loaded an OLD mail_dropbox.py:\n  {getattr(md, '__file__', '?')}\n"
        f"It is missing: {', '.join(_missing)}\n"
        "Replace that file with the current mail_dropbox.py (or copy the new one "
        "next to run_all.py) and start again."
    )
# And that airfrance_claim_v5 exposes what we need.
_missing_af = [n for n in (
    "load_rows", "process_row", "get_reason_for_status",
    "DEFAULT_CSV", "DEFAULT_DRAFTS_DIR", "SLOW_MO", "TIMEOUT",
    "CHROME_LAUNCH_ARGS", "CHROME_USER_AGENT",
) if not hasattr(af, n)]
if _missing_af:
    sys.exit(
        f"Python loaded an OLD airfrance_claim_v5.py:\n  {getattr(af, '__file__', '?')}\n"
        f"It is missing: {', '.join(_missing_af)}\n"
        "Replace that file with the current airfrance_claim_v5.py and start again."
    )

from docx import Document              # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# Air France's textbox may have a length limit; longer letters are cut here.
REASON_MAX_CHARS = 3999

# "CaseId.FirstName LastName"  ->  (FirstName, LastName, CaseId)
# e.g. "33531.Kayhan Bora" -> ("Kayhan", "Bora", "33531")
# The first word after the dot is the first name; everything after it
# is the last name (so multi-word last names like "van der Berg" work).
FOLDER_NAME_RE = re.compile(
    r"^(?P<case>[^.]+)\.(?P<first>\S+)(?:\s+(?P<last>.+))?$"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def norm(s: str) -> str:
    """Casefold + accent-strip + sort tokens, for fuzzy name comparison."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return " ".join(sorted(re.sub(r"[^a-z0-9]+", " ", s).split()))


def parse_folder_name(name: str) -> tuple[str, str, str]:
    """Split '33531.Kayhan Bora' -> ('Kayhan', 'Bora', '33531').

    Format: <CaseId>.<FirstName> <LastName>
      * CaseId is everything before the first dot.
      * FirstName is the first whitespace-delimited token after the dot.
      * LastName is everything after the first token (supports multi-word
        last names like "van der Berg").  If only one token is present,
        LastName is left empty (the name match in find_row will then rely
        on FirstName + CaseId).
    """
    m = FOLDER_NAME_RE.match(name)
    if not m:
        sys.exit(
            f"Folder name {name!r} does not look like 'CaseId.FirstName LastName'.\n"
            "Expected format: '<CaseId>.<FirstName> <LastName>'  "
            "(e.g. '33531.Kayhan Bora').\n"
            "Rename the folder (or pass --folder with the right name) and retry."
        )
    first = m.group("first").strip()
    last = (m.group("last") or "").strip()
    case_id = m.group("case").strip()
    if not case_id:
        sys.exit(f"Folder name {name!r} has no CaseId before the dot.")
    return first, last, case_id


def find_row(rows: list[dict], first: str, last: str, case_id: str) -> dict:
    """Find the info.csv row for this passenger.

    Tries CaseId first (exact, case-insensitive), then a name match
    (FirstName + LastName in either order, accent-folded).  Refuses to
    guess when multiple rows match - filling the form for the wrong
    booking would be worse than stopping.
    """
    # 1. CaseId match (most reliable - the folder name carries it).
    if case_id:
        for row in rows:
            for k in ("CaseId", "Case ID", "Case"):
                v = (row.get(k) or "").strip()
                if v and v.lower() == case_id.lower():
                    return row

    # 2. Name match (FirstName + LastName, either order, accent-folded).
    wanted_full = norm(f"{first} {last}")
    wanted_last_first = norm(f"{last} {first}")
    matches: list[dict] = []
    for row in rows:
        first_v = (row.get("FirstName") or row.get("FirstName1") or "").strip()
        last_v = (row.get("LastName") or row.get("LastName1") or "").strip()
        full = f"{first_v} {last_v}"
        if norm(full) in (wanted_full, wanted_last_first):
            matches.append(row)
        elif norm(row.get("PaxName") or "") in (wanted_full, wanted_last_first):
            matches.append(row)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listing = "\n".join(
            f"  - BookingRef={m.get('BookingRef')}  Flight={m.get('FlightNumber')}  "
            f"Name={(m.get('FirstName') or m.get('FirstName1', ''))} "
            f"{(m.get('LastName') or m.get('LastName1', ''))}"
            for m in matches
        )
        sys.exit(
            f"Multiple info.csv rows match {first} {last}:\n{listing}\n"
            "Add a CaseId column to info.csv (and use it in the folder name) so "
            "the right row can be picked unambiguously."
        )

    in_csv = ", ".join(
        f"{(r.get('FirstName') or r.get('FirstName1', ''))} "
        f"{(r.get('LastName') or r.get('LastName1', ''))} "
        f"({r.get('BookingRef', '')})"
        for r in rows
    )
    sys.exit(
        f"No row in info.csv matches {first} {last} (case_id={case_id}).\n"
        f"  Rows in info.csv: {in_csv}\n"
        "Add a row for this passenger (FirstName/LastName, or CaseId) to info.csv "
        "and run again."
    )


def letter_text(path: Path) -> str:
    """Read the .docx body text, capped to REASON_MAX_CHARS."""
    doc = Document(str(path))
    parts = [p.text.strip() for p in md.iter_paragraphs(doc) if p.text.strip()]
    text = "\n".join(parts)
    if len(text) > REASON_MAX_CHARS:
        print(f"  WARNING: letter has {len(text)} chars; cutting to {REASON_MAX_CHARS}.")
        text = text[:REASON_MAX_CHARS]
    return text


def find_letter_in_folder(folder: Path) -> Path | None:
    """Return an existing letter .docx in the folder, if any.

    The previous mail_dropbox.py wrote it as "<Owner> - letter_template.docx",
    so we match that pattern first, then fall back to any .docx that isn't
    the bare template.
    """
    candidates = sorted(folder.glob("*letter_template*.docx"))
    if candidates:
        return candidates[0]
    template_name = md.TEMPLATE_DOCX.name.lower()
    for f in sorted(folder.glob("*.docx")):
        if f.name.lower() != template_name:
            return f
    return None


# --------------------------------------------------------------------------- #
# Dropbox folder discovery
# --------------------------------------------------------------------------- #
def list_local_claims_folders() -> list[Path]:
    """Local Dropbox Claims folders whose name matches CaseId.FirstName LastName."""
    root = md.find_claims_folder()
    if root is None or not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and FOLDER_NAME_RE.match(p.name)
    )


def list_remote_claims_folders(dbx) -> list[str]:
    """Remote Dropbox Claims folders matching CaseId.FirstName LastName."""
    import dropbox
    out: list[str] = []
    try:
        res = dbx.files_list_folder(md.DROPBOX_REMOTE_CLAIMS)
        entries = list(res.entries)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            entries += res.entries
        for e in entries:
            if isinstance(e, dropbox.files.FolderMetadata) \
                    and FOLDER_NAME_RE.match(e.name):
                out.append(e.name)
    except Exception as exc:
        print(f"  (could not list remote Dropbox folders: {exc})")
    return sorted(out)


def download_remote_folder(dbx, folder_name: str) -> Path:
    """Mirror a remote Dropbox folder into a temp dir; return its path."""
    import dropbox
    local = Path(tempfile.gettempdir()) / "MailDropbox" / folder_name
    if local.exists():
        for f in local.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass
    local.mkdir(parents=True, exist_ok=True)

    remote = f"{md.DROPBOX_REMOTE_CLAIMS}/{folder_name}"
    res = dbx.files_list_folder(remote)
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries += res.entries

    for e in entries:
        if not isinstance(e, dropbox.files.FileMetadata):
            continue
        try:
            dbx.files_download_to_file(str(local / e.name), f"{remote}/{e.name}")
            print(f"  downloaded from Dropbox: {e.name}")
        except Exception as exc:
            print(f"  (could not download {e.name}: {exc})")
    return local


def upload_letter_to_remote(dbx, letter: Path, folder_name: str) -> None:
    """Push a generated letter back to the remote Dropbox folder."""
    import dropbox
    remote = f"{md.DROPBOX_REMOTE_CLAIMS}/{folder_name}/{letter.name}"
    try:
        with open(letter, "rb") as fh:
            dbx.files_upload(fh.read(), remote,
                             mode=dropbox.files.WriteMode.overwrite)
        print(f"  uploaded letter to Dropbox: {remote}")
    except Exception as exc:
        print(f"  (could not upload letter to Dropbox: {exc})")


def pick_folder(names: list[str]) -> str:
    if not names:
        sys.exit(
            "No Dropbox Claims folders look like 'CaseId.FirstName LastName'.\n"
            f"  Looked under: {md.DROPBOX_REMOTE_CLAIMS}\n"
            "  Create a folder there named '<CaseId>.<FirstName> <LastName>' "
            "(e.g. '33531.Kayhan Bora') and put the passenger's PDF attachments "
            "inside it."
        )
    if len(names) == 1:
        return names[0]
    print("Multiple Dropbox Claims folders found:")
    for i, n in enumerate(names, 1):
        print(f"  [{i}] {n}")
    while True:
        choice = input("Pick one (number): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        print("Invalid choice; try again.")


def resolve_folder(folder_arg: str | None) -> tuple[Path, str, bool]:
    """Return (folder_path, folder_name, used_api).

    * If `folder_arg` is an existing directory path, use it directly.
    * Else treat `folder_arg` as a folder name and look it up under the
      local Claims root first, then via the Dropbox API.
    * If `folder_arg` is None, list candidates and ask the user to pick.
    """
    # 1. Explicit path on disk
    if folder_arg:
        p = Path(folder_arg)
        if p.is_dir():
            return p, p.name, False

    name = folder_arg

    # 2. No name given -> list and pick
    if name is None:
        local_folders = list_local_claims_folders()
        if local_folders:
            name = pick_folder([p.name for p in local_folders])
            return next(p for p in local_folders if p.name == name), name, False
        dbx = md.get_dropbox_client()
        remote_names = list_remote_claims_folders(dbx)
        name = pick_folder(remote_names)
        return download_remote_folder(dbx, name), name, True

    # 3. Name given -> local first, then API
    root = md.find_claims_folder()
    if root is not None and (root / name).is_dir():
        return root / name, name, False
    print(f"  folder not found under local Claims root; trying Dropbox API...")
    dbx = md.get_dropbox_client()
    return download_remote_folder(dbx, name), name, True


# --------------------------------------------------------------------------- #
# Stage 1: Dropbox folder -> attachments + letter
# --------------------------------------------------------------------------- #
def stage1(folder_arg: str | None, csv_path: str) -> dict:
    print("=== Stage 1: Dropbox folder -> attachments + letter ===")
    folder, folder_name, used_api = resolve_folder(folder_arg)
    print(f"Folder: {folder}")

    first, last, case_id = parse_folder_name(folder_name)
    owner = f"{first} {last}"
    print(f"  parsed: first={first!r} last={last!r} case_id={case_id!r}")

    # --- PDFs in the folder ---
    pdfs = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )
    if not pdfs:
        sys.exit(f"No PDF attachments found in {folder}.")
    print(f"  found {len(pdfs)} PDF(s):")
    for p in pdfs:
        print(f"    - {p.name}")

    # --- Match a row in info.csv (by CaseId, then by name) ---
    rows = af.load_rows(csv_path)
    if not rows:
        sys.exit(f"{csv_path} has no data rows.")
    row = find_row(rows, first, last, case_id)
    print(f"  matched info.csv row: BookingRef={row.get('BookingRef')} "
          f"Flight={row.get('FlightNumber')}")

    # --- Letter: reuse the one in the folder, or generate from template ---
    letter_path = find_letter_in_folder(folder)
    if letter_path is not None:
        print(f"  reusing existing letter: {letter_path.name}")
    else:
        if not md.TEMPLATE_DOCX.is_file():
            sys.exit(
                f"Letter template not found: {md.TEMPLATE_DOCX}\n"
                "Either put a generated letter .docx in the folder, or restore "
                "letter_template.docx at the path above."
            )
        letter_values = md.build_replacements(row)
        letter_path = folder / f"{md.safe_filename(owner)} - {md.TEMPLATE_DOCX.name}"
        md.build_letter(md.TEMPLATE_DOCX, letter_path,
                        {**letter_values, md.OLD_NAME: owner})
        print(f"  generated letter: {letter_path.name}")
        if used_api:
            # We downloaded to a temp dir; push the letter back so the next
            # run can reuse it.
            try:
                dbx = md.get_dropbox_client()
                upload_letter_to_remote(dbx, letter_path, folder_name)
            except Exception as exc:
                print(f"  (could not upload letter back to Dropbox: {exc})")

    return {
        "folder": folder,
        "folder_name": folder_name,
        "case_id": case_id,
        "first": first,
        "last": last,
        "owner": owner,
        "info": row,
        "attachments": pdfs,
        "letter": letter_path,
    }


# --------------------------------------------------------------------------- #
# Stage 2: Air France claim form
# --------------------------------------------------------------------------- #
def stage2(info: dict, csv_path: str, headless: bool) -> None:
    print("\n=== Stage 2: Air France claim form ===")
    pdfs = [str(p) for p in info["attachments"]]
    if not pdfs:
        sys.exit("No PDF attachments to upload to Air France.")
    reason = letter_text(info["letter"])
    if not reason:
        sys.exit(f"The letter {info['letter']} has no text to use as the reason.")

    rows = af.load_rows(csv_path)
    row = dict(find_row(rows, info["first"], info["last"], info["case_id"]))
    row["Files"] = "|".join(pdfs)                      # downloaded PDFs
    af.get_reason_for_status = lambda status: reason   # letter text = reason
    print(f"Row: {row.get('BookingRef')}  {row.get('FirstName')} {row.get('LastName')}"
          f"  Flight={row.get('FlightNumber')}  Status={row.get('Status')}")
    print(f"Files ({len(pdfs)}): " + " | ".join(Path(p).name for p in pdfs))

    af.DRAFT_DIR = Path(af.DEFAULT_DRAFTS_DIR)
    af.DRAFT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
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


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Dropbox folder -> Air France claim.  No Gmail."
    )
    ap.add_argument("csv_file", nargs="?", default=af.DEFAULT_CSV,
                    help=f"path to info.csv (default: {af.DEFAULT_CSV})")
    ap.add_argument("--folder", default=None,
                    help="Dropbox Claims folder name like '33531.Kayhan Bora' "
                         "(format: <CaseId>.<FirstName> <LastName>), or a full "
                         "path to that folder.  If omitted, the script lists every "
                         "matching folder and asks you to pick one.")
    ap.add_argument("--headless", action="store_true",
                    help="run the browser headless (no GUI)")
    args = ap.parse_args()

    info = stage1(args.folder, args.csv_file)
    stage2(info, args.csv_file, args.headless)


if __name__ == "__main__":
    main()
