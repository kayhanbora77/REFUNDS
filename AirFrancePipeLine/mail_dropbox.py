"""
Gmail -> Dropbox -> Word pipeline

1. Connect to Gmail (the mailbox that owns credentials.json / token.json)
2. Read the sender e-mail address from the "Email" column of info.csv
3. Find the LAST email from that address with a PDF attachment named
   Assignment_Form_<CASE_ID>_<first>_<last>.pdf
4. Create folder  Agency Data\\Claims\\<CASE_ID>.<Firstname Lastname>
5. Download ALL attachments of that email into the folder
6. Copy letter_template.docx into the folder, filling the placeholders
   from the info.csv row (CaseId, FlightNumber, BookingRef, names, airports).
   If LastName2/FirstName2 are empty, "and LASTNAME2 FIRSTNAME2" is removed.
7. Upload the folder to Dropbox.

Install:
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2 python-docx dropbox
"""

import base64
import csv
import io
import json
import os
import re
import sys
import tempfile
from email.utils import parseaddr
from pathlib import Path

from docx import Document
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ----------------------------------------------------------------------------
# CONFIG - edit these
# ----------------------------------------------------------------------------
INFO_CSV = Path(r"C:\Coding\APPLY_AIRLINE\AirFrancePipeLine\info.csv")
EMAIL_COLUMN = ""
SENDER_EMAIL_OVERRIDE = ""
DROPBOX_CLAIMS: Path | None = None

TEMPLATE_DOCX = Path(r"C:\Coding\APPLY_AIRLINE\AirFrancePipeLine\letter_template.docx")
CREDENTIALS_FILE = Path(r"C:\Coding\APPLY_AIRLINE\AirFrancePipeLine\credentials.json")
TOKEN_FILE = Path(__file__).resolve().parent / "token.json"

# Legacy placeholder (harmless if the template no longer contains it)
OLD_NAME = "XXX"

# Template placeholder -> info.csv column
TEMPLATE_FIELDS = {
    "CASE_ID": "CaseId",
    "FLIGHT_NUMBER": "FlightNumber",
    "BOOKING_NUMBER": "BookingRef",
    "LAST_NAME1": "LastName1",
    "FIRST_NAME1": "FirstName1",
    "FROM_AIRPORT_LONGNAME": "FromAirportLongName",
    "FROM_AIRPORT": "FromAirport",
    "TO_AIRPORT_LONGNAME": "ToAirportLongName",
    "TO_AIRPORT": "ToAirport",
}
# Second passenger placeholders (the template spells them without underscore)
LAST_NAME2_KEYS = ("LASTNAME2", "LAST_NAME2")
FIRST_NAME2_KEYS = ("FIRSTNAME2", "FIRST_NAME2")

NAME_SOURCE = "header"
FORM_RE = re.compile(r"^Assignment_Form_(\d+)_(.+)\.pdf$", re.IGNORECASE)

DROPBOX_APP_KEY = "94aa13slcrac8zt"
DROPBOX_REMOTE_CLAIMS = "/Agency Data/Claims"
DROPBOX_TOKEN_FILE = Path(__file__).resolve().parent / "dropbox_token.json"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
EMAIL_VALUE_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")
EMPTY_VALUES = {"", "nan", "none", "null", "-", "n/a"}


# ----------------------------------------------------------------------------
# info.csv
# ----------------------------------------------------------------------------
def resolve_info_csv() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        Path(INFO_CSV) if INFO_CSV else None,
        here / "info.csv",
        here.parent / "AirFrancePipeLine" / "info.csv",
        Path.cwd() / "info.csv",
    ]
    for c in candidates:
        if c and c.is_file():
            return c
    tried = "\n  ".join(str(c) for c in candidates if c)
    raise SystemExit(
        f"info.csv not found. Looked at:\n  {tried}\n"
        f"Set INFO_CSV to the file that holds the 'Email' column."
    )


def find_email_column(header: list[str]) -> str | None:
    if EMAIL_COLUMN:
        for h in header:
            if h and h.strip().lower() == EMAIL_COLUMN.strip().lower():
                return h
        return None

    norm: dict[str, str] = {}
    for h in header:
        if not h:
            continue
        key = re.sub(r"[^a-z0-9]", "", h.strip().lower())
        if key and key not in norm:
            norm[key] = h

    for key in ("email", "emailaddress", "eaddress", "mail", "contactemail"):
        if key in norm:
            return norm[key]
    for key, h in norm.items():
        if "email" in key:
            return h
    return None


def sniff_delimiter(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()][:10]
    best, best_cols = ",", 0
    for d in (",", ";", "\t", "|"):
        counts = {len(next(csv.reader([ln], delimiter=d))) for ln in lines}
        if len(counts) != 1:
            continue
        cols = counts.pop()
        if cols < 2:
            continue
        if cols > best_cols:
            best, best_cols = d, cols
    return best


def read_csv_table(path: Path) -> tuple[list[str], list[list[str]]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    delimiter = sniff_delimiter(text)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        raise SystemExit(f"{path} is empty.")

    for i, row in enumerate(rows[:5]):
        if find_email_column(row):
            return row, rows[i + 1:]
    return rows[0], rows[1:]


def load_info_records() -> tuple[list[str], list[dict[str, str]]]:
    """Return (sender e-mails, one dict per data row keyed by column name)."""
    path = resolve_info_csv()
    header, rows = read_csv_table(path)

    col = find_email_column(header)
    if col is None:
        msg = f"No 'Email' column found in {path}. Columns: {header}"
        if EMAIL_COLUMN:
            msg += f"\nEMAIL_COLUMN is set to {EMAIL_COLUMN!r} - is that the column?"
        raise SystemExit(msg)

    names = [h.strip() for h in header]
    records: list[dict[str, str]] = []
    emails: list[str] = []
    seen: set[str] = set()
    idx = header.index(col)
    for row in rows:
        rec = {names[i]: (row[i] if i < len(row) else "").strip()
               for i in range(len(names))}
        records.append(rec)
        raw = (row[idx] if idx < len(row) else "").strip()
        if raw.lower() in EMPTY_VALUES:
            continue
        for addr in re.split(r"[;,|]", raw):
            addr = addr.strip()
            if EMAIL_VALUE_RE.match(addr) and addr.lower() not in seen:
                seen.add(addr.lower())
                emails.append(addr)

    if not emails:
        raise SystemExit(f"No valid e-mail address in column {col!r} of {path}.")
    return emails, records


def pick_record(records: list[dict[str, str]], sender_email: str) -> dict[str, str]:
    """The info.csv row for this sender (falls back to the first row)."""
    for rec in records:
        for k, v in rec.items():
            if k.lower().replace("_", "") == "email" and \
                    sender_email.lower() in v.lower():
                return rec
    return records[0]


def get_field(rec: dict[str, str], column: str) -> str:
    """Case-insensitive column lookup; empty / null-like values -> ''."""
    for k, v in rec.items():
        if k.strip().lower() == column.lower():
            v = (v or "").strip()
            return "" if v.lower() in EMPTY_VALUES else v
    return ""


def build_replacements(rec: dict[str, str]) -> dict[str, str]:
    """Placeholder -> value. Order matters: the 2nd-passenger removal comes
    first, and longer placeholders before their prefixes (FROM_AIRPORT_LONGNAME
    before FROM_AIRPORT)."""
    reps: dict[str, str] = {}

    last2 = get_field(rec, "LastName2")
    first2 = get_field(rec, "FirstName2")
    if last2 and first2:
        for k in LAST_NAME2_KEYS:
            reps[k] = last2
        for k in FIRST_NAME2_KEYS:
            reps[k] = first2
    else:
        # remove ' and LASTNAME2 FIRSTNAME2' completely
        for ln in LAST_NAME2_KEYS:
            for fn in FIRST_NAME2_KEYS:
                reps[f" and {ln} {fn}"] = ""
                reps[f" and {fn} {ln}"] = ""

    for placeholder in sorted(TEMPLATE_FIELDS, key=len, reverse=True):
        reps[placeholder] = get_field(rec, TEMPLATE_FIELDS[placeholder])
    return reps


def build_gmail_query(emails: list[str]) -> str:
    if len(emails) == 1:
        return f"from:{emails[0]} has:attachment filename:pdf"
    return "{" + " ".join(f"from:{e}" for e in emails) + "} has:attachment filename:pdf"


# ----------------------------------------------------------------------------
# Gmail
# ----------------------------------------------------------------------------
def get_gmail_service(login_hint: str = ""):
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except Exception as exc:
                print(f"Token refresh failed ({exc}); signing in again")
        if not refreshed:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(
                port=0,
                login_hint=login_hint,
                authorization_prompt_message="",
                success_message="Signed in. You can close this tab.",
            )
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def list_message_ids(service, query):
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token
        ).execute()
        for m in resp.get("messages", []):
            yield m["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def header(msg, name):
    for h in msg["payload"].get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def iter_attachment_parts(payload):
    body = payload.get("body", {})
    if payload.get("filename") and (body.get("attachmentId") or body.get("data")):
        yield payload
    for part in payload.get("parts", []) or []:
        yield from iter_attachment_parts(part)


def find_assignment_form(msg):
    for part in iter_attachment_parts(msg["payload"]):
        m = FORM_RE.match(part["filename"])
        if m:
            return m.group(1), nice_name(m.group(2).replace("_", " "))
    return None


def nice_name(raw: str) -> str:
    raw = re.sub(r"[._\-]+", " ", raw)
    return " ".join(w.capitalize() for w in raw.split())


def owner_name_from_header(msg):
    display, addr = parseaddr(header(msg, "From"))
    name = display.strip().strip('"')
    if not name:
        name = addr.split("@")[0]
    return nice_name(name)


def is_inline(part) -> bool:
    hdrs = {h["name"].lower(): h["value"] for h in part.get("headers", [])}
    disp = hdrs.get("content-disposition", "").strip().lower()
    if disp.startswith("attachment"):
        return False
    if disp.startswith("inline"):
        return True
    return part.get("mimeType", "").startswith("image/") and "content-id" in hdrs


def download_attachments(service, msg, dest: Path):
    saved = []
    parts = list(iter_attachment_parts(msg["payload"]))
    print(f"  email has {len(parts)} file part(s):")
    for part in parts:
        hdrs = {h["name"].lower(): h["value"] for h in part.get("headers", [])}
        print(f"    - {part['filename']}  [{part.get('mimeType', '?')}]  "
              f"disposition={hdrs.get('content-disposition', '-').split(';')[0]}")
    for part in parts:
        if is_inline(part):
            print(f"  skipped inline image: {part['filename']}")
            continue
        body = part["body"]
        if "attachmentId" in body:
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"]
            ).execute()
            data = att["data"]
        else:
            data = body["data"]
        raw = base64.urlsafe_b64decode(data.encode("utf-8"))

        target = dest / safe_filename(part["filename"])
        target.write_bytes(raw)
        saved.append(target)
        print(f"  saved attachment: {target.name}")
    return saved


# ----------------------------------------------------------------------------
# Filesystem helpers
# ----------------------------------------------------------------------------
def find_claims_folder() -> Path | None:
    if DROPBOX_CLAIMS is not None:
        return DROPBOX_CLAIMS

    roots = []
    for env in ("LOCALAPPDATA", "APPDATA"):
        info = Path(os.environ.get(env, "")) / "Dropbox" / "info.json"
        if info.is_file():
            try:
                data = json.loads(info.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for acct in data.values():
                if isinstance(acct, dict) and acct.get("path"):
                    roots.append(Path(acct["path"]))
    roots.append(Path.home() / "Dropbox")

    for root in roots:
        agency = root / "Agency Data"
        if agency.is_dir():
            return agency / "Claims"

    return None


def is_stale_copy(name: str, keep: set[str], case_id: str) -> bool:
    if name in keep:
        return False
    m = re.match(r"^(.*) \(\d+\)(\.[^.]+)?$", name)
    if m and (m.group(1) + (m.group(2) or "")) in keep:
        return True
    fm = FORM_RE.match(name)
    if fm and fm.group(1) == case_id:
        return True
    stem, suffix = TEMPLATE_DOCX.stem, TEMPLATE_DOCX.suffix
    return bool(re.fullmatch(
        rf".* - {re.escape(stem)}( \(\d+\))?{re.escape(suffix)}", name))


def cleanup_local(folder: Path, keep: set[str], case_id: str):
    for f in folder.iterdir():
        if f.is_file() and is_stale_copy(f.name, keep, case_id):
            try:
                f.unlink()
                print(f"  removed old duplicate: {f.name}")
            except OSError as exc:
                print(f"  could not remove {f.name}: {exc}")


def cleanup_remote(dbx, remote_folder: str, keep: set[str], case_id: str):
    import dropbox
    try:
        res = dbx.files_list_folder(remote_folder)
        entries = list(res.entries)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            entries += res.entries
        for e in entries:
            if (isinstance(e, dropbox.files.FileMetadata)
                    and is_stale_copy(e.name, keep, case_id)):
                dbx.files_delete_v2(f"{remote_folder}/{e.name}")
                print(f"  removed old duplicate on Dropbox: {e.name}")
    except Exception as exc:
        print(f"  (could not clean old duplicates on Dropbox: {exc})")


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "unnamed"


# ----------------------------------------------------------------------------
# Dropbox API upload
# ----------------------------------------------------------------------------
def get_dropbox_client():
    import dropbox
    from dropbox import DropboxOAuth2FlowNoRedirect

    if not DROPBOX_APP_KEY:
        sys.exit(
            "No synced Dropbox folder found on this computer, and DROPBOX_APP_KEY is empty.\n"
            "Either install/sign in to the Dropbox desktop app and sync 'Agency Data',\n"
            "or create a Dropbox app and paste its App key into DROPBOX_APP_KEY."
        )

    if DROPBOX_TOKEN_FILE.exists():
        refresh = json.loads(DROPBOX_TOKEN_FILE.read_text(encoding="utf-8"))["refresh_token"]
    else:
        flow = DropboxOAuth2FlowNoRedirect(
            DROPBOX_APP_KEY, use_pkce=True, token_access_type="offline"
        )
        print("\n1. Open this URL, click Allow:\n  ", flow.start())
        code = input("2. Paste the authorization code here: ").strip()
        result = flow.finish(code)
        refresh = result.refresh_token
        DROPBOX_TOKEN_FILE.write_text(json.dumps({"refresh_token": refresh}), encoding="utf-8")

    dbx = dropbox.Dropbox(oauth2_refresh_token=refresh, app_key=DROPBOX_APP_KEY)
    try:
        root = dbx.users_get_current_account().root_info.root_namespace_id
        dbx = dbx.with_path_root(dropbox.common.PathRoot.root(root))
    except Exception:
        pass
    return dbx


def upload_folder(dbx, local_folder: Path, remote_folder: str):
    import dropbox

    for f in sorted(local_folder.iterdir()):
        if not f.is_file():
            continue
        remote = f"{remote_folder}/{f.name}"
        size = f.stat().st_size
        with open(f, "rb") as fh:
            if size <= 100 * 1024 * 1024:
                dbx.files_upload(fh.read(), remote, mode=dropbox.files.WriteMode.overwrite)
            else:
                chunk = 50 * 1024 * 1024
                sess = dbx.files_upload_session_start(fh.read(chunk))
                cursor = dropbox.files.UploadSessionCursor(sess.session_id, fh.tell())
                commit = dropbox.files.CommitInfo(
                    path=remote, mode=dropbox.files.WriteMode.overwrite)
                while size - fh.tell() > chunk:
                    dbx.files_upload_session_append_v2(fh.read(chunk), cursor)
                    cursor.offset = fh.tell()
                dbx.files_upload_session_finish(fh.read(chunk), cursor, commit)
        print(f"  uploaded to Dropbox: {remote}")


# ----------------------------------------------------------------------------
# Word replacement
# ----------------------------------------------------------------------------
def replace_in_paragraph(paragraph, replacements: dict[str, str]):
    """Replace text even when Word split it across several runs; keeps the
    formatting of the run where the match starts. Replacements are applied in
    dict order."""
    for old, new in replacements.items():
        if not old:
            continue
        search_from = 0
        while True:
            runs = paragraph.runs
            full = "".join(r.text for r in runs)
            idx = full.find(old, search_from)
            if idx == -1:
                break
            end = idx + len(old)
            pos = 0
            first = True
            for r in runs:
                r_start, r_end = pos, pos + len(r.text)
                pos = r_end
                if r_end <= idx or r_start >= end:
                    continue
                s = max(idx, r_start) - r_start
                e = min(end, r_end) - r_start
                if first:
                    r.text = r.text[:s] + new + r.text[e:]
                    first = False
                else:
                    r.text = r.text[:s] + r.text[e:]
            search_from = idx + len(new)


def iter_paragraphs(doc):
    def from_container(container):
        for p in container.paragraphs:
            yield p
        for table in getattr(container, "tables", []):
            for row in table.rows:
                for cell in row.cells:
                    yield from from_container(cell)

    yield from from_container(doc)
    for section in doc.sections:
        for part in (section.header, section.footer,
                     section.first_page_header, section.first_page_footer):
            yield from from_container(part)


def build_letter(template: Path, output: Path, replacements: dict[str, str]):
    doc = Document(str(template))
    for p in iter_paragraphs(doc):
        replace_in_paragraph(p, replacements)
    doc.save(str(output))


# ----------------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------------
def run() -> dict:
    emails, records = load_info_records()
    override = SENDER_EMAIL_OVERRIDE.strip()
    sender_email = override or emails[0]
    search_emails = [sender_email] if override else emails
    print(f"Sender e-mail from info.csv: {', '.join(search_emails)}")

    record = pick_record(records, sender_email)
    letter_values = build_replacements(record)
    print("Letter values from info.csv: " + ", ".join(
        f"{k}={v!r}" for k, v in letter_values.items() if v))

    service = get_gmail_service(login_hint=sender_email)
    try:
        profile = service.users().getProfile(userId="me").execute()
        mailbox = profile.get("emailAddress", "?")
    except Exception:
        mailbox = "?"
    print(f"Connected to Gmail as {mailbox}")

    msg = case_id = fname_owner = None
    query = build_gmail_query(search_emails)
    print(f"Gmail query: {query}")
    for mid in list_message_ids(service, query):
        candidate = service.users().messages().get(
            userId="me", id=mid, format="full"
        ).execute()
        found = find_assignment_form(candidate)
        if found:
            msg = candidate
            case_id, fname_owner = found
            break
    if msg is None:
        sys.exit(
            f"No email from {', '.join(search_emails)} "
            f"with an Assignment_Form_*.pdf attachment"
        )

    owner = fname_owner if NAME_SOURCE == "filename" else owner_name_from_header(msg)
    print(f"Email: {header(msg, 'Subject')!r}  case={case_id}  owner={owner}")

    folder_name = safe_filename(f"{case_id}.{owner}")
    claims_root = find_claims_folder()
    if claims_root is not None:
        folder = claims_root / folder_name
        use_api = False
    else:
        dbx = get_dropbox_client()
        folder = Path(tempfile.gettempdir()) / "MailDropbox" / folder_name
        use_api = True
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Folder: {folder}")

    saved = download_attachments(service, msg, folder)

    replacements = {**letter_values, OLD_NAME: owner}
    output_docx = folder / f"{safe_filename(owner)} - {TEMPLATE_DOCX.name}"
    build_letter(TEMPLATE_DOCX, output_docx, replacements)
    print(f"Letter created: {output_docx.name}")

    keep = {p.name for p in saved} | {output_docx.name}
    cleanup_local(folder, keep, case_id)

    if use_api:
        upload_folder(dbx, folder, f"{DROPBOX_REMOTE_CLAIMS}/{folder_name}")
        cleanup_remote(dbx, f"{DROPBOX_REMOTE_CLAIMS}/{folder_name}", keep, case_id)
        print(f"Done: dropbox.com/home{DROPBOX_REMOTE_CLAIMS}/{folder_name}")

    return {
        "folder": folder,
        "case_id": case_id,
        "owner": owner,
        "sender_email": sender_email,
        "info": record,
        "names": [fname_owner, owner_name_from_header(msg)] + [
            nice_name(m.group(2))
            for part in iter_attachment_parts(msg["payload"])
            for m in [FORM_RE.match(part["filename"])] if m
        ],
        "attachments": saved,
        "letter": output_docx,
    }


def main():
    run()


if __name__ == "__main__":
    main()
