"""
Gmail -> Dropbox -> Word pipeline

1. Connect to Gmail (kayhanboraisteps@gmail.com)
2. Find the LAST email from SENDER_EMAIL that has a PDF attachment named
   Assignment_Form_<CASE_ID>_<first>_<last>.pdf
   (e.g. Assignment_Form_533439_olga_kagan.pdf -> CASE_ID = 533439)
3. Create folder  Agency Data\\Claims\\<CASE_ID>.<Firstname Lastname>
4. Download ALL attachments of that email into the folder
5. Copy letter_template.docx into the folder, replacing "XXX" with the
   sender's first name and last name.

Install:
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2 python-docx

Setup:
    - Gmail API enabled in Google Cloud Console, OAuth client (Desktop app)
      saved as credentials.json.
    - First run opens a browser for consent and stores token.json
      (next to this script). If you change SCOPES, delete token.json.
"""

import base64
import json
import os
import re
import shutil
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
SENDER_EMAIL = "kayhanboraisteps@gmail.com"

# Local folder synced with https://www.dropbox.com/home/Agency%20Data/Claims
# Leave as None to auto-detect from the Dropbox desktop app on THIS computer.
# Or set it by hand, e.g. Path(r"C:\Users\cagri\Dropbox\Agency Data\Claims")
DROPBOX_CLAIMS: Path | None = None

TEMPLATE_DOCX = Path(r"C:\Coding\APPLY_AIRLINE\MailDropbox\letter_template.docx")
CREDENTIALS_FILE = Path(r"C:\Coding\APPLY_AIRLINE\MailDropbox\credentials.json")
TOKEN_FILE = Path(__file__).resolve().parent / "token.json"

# Text in the Word template to replace with "Firstname Lastname"
OLD_NAME = "XXX"

# Optional extra replacements, e.g. {"A Airport-IataCode": "Istanbul Airport-IST"}
EXTRA_REPLACEMENTS: dict[str, str] = {}

# Name source for folder + letter:
#   "header"   -> From: display name of the email (fallback: file name)
#   "filename" -> name inside the attachment file name (olga_kagan -> Olga Kagan)
NAME_SOURCE = "header"

# Attachment file name pattern: Assignment_Form_<case id>_<name parts>.pdf
FORM_RE = re.compile(r"^Assignment_Form_(\d+)_(.+)\.pdf$", re.IGNORECASE)

# --- Dropbox over the internet (used when there is no synced folder) --------
# One-time setup: https://www.dropbox.com/developers/apps -> Create app ->
#   "Scoped access" -> "Full Dropbox" -> Permissions tab: tick
#   files.content.write + files.content.read (+ sharing.read if needed) -> Submit.
# Copy the "App key" here (no secret needed). First run asks you to approve once.
DROPBOX_APP_KEY = "94aa13slcrac8zt"
DROPBOX_REMOTE_CLAIMS = "/Agency Data/Claims"      # = dropbox.com/home/Agency Data/Claims
DROPBOX_TOKEN_FILE = Path(__file__).resolve().parent / "dropbox_token.json"

# Narrow Gmail search; exact filename check is done in code
GMAIL_QUERY = f"from:{SENDER_EMAIL} has:attachment filename:pdf"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ----------------------------------------------------------------------------
# Gmail
# ----------------------------------------------------------------------------
def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except Exception as exc:  # revoked/expired refresh token
                print(f"Token refresh failed ({exc}); signing in again")
        if not refreshed:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            # login_hint pre-selects SENDER_EMAIL, so Google skips the
            # "choose an account" screen. prompt="consent" is not forced, so
            # after the first sign-in nothing is asked again.
            creds = flow.run_local_server(
                port=0,
                login_hint=SENDER_EMAIL,
                authorization_prompt_message="",
                success_message="Signed in. You can close this tab.",
            )
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def list_message_ids(service, query):
    """Message ids, newest first (Gmail's default order)."""
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
    """Return (case_id, name_from_filename) if the message has the form PDF."""
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


def download_attachments(service, msg, dest: Path):
    saved = []
    for part in iter_attachment_parts(msg["payload"]):
        body = part["body"]
        if "attachmentId" in body:
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"]
            ).execute()
            data = att["data"]
        else:
            data = body["data"]
        raw = base64.urlsafe_b64decode(data.encode("utf-8"))

        target = unique_path(dest / safe_filename(part["filename"]))
        target.write_bytes(raw)
        saved.append(target)
        print(f"  saved attachment: {target.name}")
    return saved


# ----------------------------------------------------------------------------
# Filesystem helpers
# ----------------------------------------------------------------------------
def find_claims_folder() -> Path | None:
    """Locate '<Dropbox>\\Agency Data\\Claims' on this computer, or None."""
    if DROPBOX_CLAIMS is not None:
        return DROPBOX_CLAIMS

    roots = []
    # The Dropbox desktop app records its sync folders (personal + business)
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
    roots.append(Path.home() / "Dropbox")  # default location

    for root in roots:
        agency = root / "Agency Data"
        if agency.is_dir():
            return agency / "Claims"   # created below if missing

    return None  # no synced Dropbox folder on this computer -> use the API


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "unnamed"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


# ----------------------------------------------------------------------------
# Dropbox API upload
# ----------------------------------------------------------------------------
def get_dropbox_client():
    import dropbox  # pip install dropbox
    from dropbox import DropboxOAuth2FlowNoRedirect

    if not DROPBOX_APP_KEY:
        sys.exit(
            "No synced Dropbox folder found on this computer, and DROPBOX_APP_KEY is empty.\n"
            "Either install/sign in to the Dropbox desktop app and sync 'Agency Data',\n"
            "or create a Dropbox app and paste its App key into DROPBOX_APP_KEY (see script top)."
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
    # Make paths relative to the Dropbox root (needed for team accounts)
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
                dbx.files_upload(fh.read(), remote, mode=dropbox.files.WriteMode.add, autorename=True)
            else:  # large files: chunked upload
                chunk = 50 * 1024 * 1024
                sess = dbx.files_upload_session_start(fh.read(chunk))
                cursor = dropbox.files.UploadSessionCursor(sess.session_id, fh.tell())
                commit = dropbox.files.CommitInfo(path=remote, autorename=True)
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
    formatting of the run where the match starts."""
    for old, new in replacements.items():
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
            search_from = idx + len(new)  # never re-scan the inserted text


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
    """Gmail -> local/Dropbox folder -> letter. Returns info for the next stage."""
    service = get_gmail_service()
    print(f"Connected to Gmail as {SENDER_EMAIL}")

    # Newest first -> first message with a matching Assignment_Form PDF wins
    msg = case_id = fname_owner = None
    for mid in list_message_ids(service, GMAIL_QUERY):
        candidate = service.users().messages().get(
            userId="me", id=mid, format="full"
        ).execute()
        found = find_assignment_form(candidate)
        if found:
            msg = candidate
            case_id, fname_owner = found
            break
    if msg is None:
        sys.exit(f"No email from {SENDER_EMAIL} with an Assignment_Form_*.pdf attachment")

    owner = fname_owner if NAME_SOURCE == "filename" else owner_name_from_header(msg)
    print(f"Email: {header(msg, 'Subject')!r}  case={case_id}  owner={owner}")

    folder_name = safe_filename(f"{case_id}.{owner}")
    claims_root = find_claims_folder()
    if claims_root is not None:
        folder = claims_root / folder_name          # synced Dropbox folder
        use_api = False
    else:
        dbx = get_dropbox_client()                  # upload over the internet
        folder = Path(tempfile.gettempdir()) / "MailDropbox" / folder_name
        use_api = True
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Folder: {folder}")

    saved = download_attachments(service, msg, folder)

    replacements = {OLD_NAME: owner, **EXTRA_REPLACEMENTS}
    output_docx = unique_path(folder / f"{safe_filename(owner)} - {TEMPLATE_DOCX.name}")
    build_letter(TEMPLATE_DOCX, output_docx, replacements)
    print(f"Letter created: {output_docx.name}")

    if use_api:
        upload_folder(dbx, folder, f"{DROPBOX_REMOTE_CLAIMS}/{folder_name}")
        print(f"Done: dropbox.com/home{DROPBOX_REMOTE_CLAIMS}/{folder_name}")

    return {
        "folder": folder,                       # local folder holding everything
        "case_id": case_id,
        "owner": owner,
        "names": [fname_owner, owner_name_from_header(msg)],  # for CSV matching
        "attachments": saved,                   # downloaded attachment files
        "letter": output_docx,                  # generated letter (.docx)
    }


def main():
    run()


if __name__ == "__main__":
    main()
