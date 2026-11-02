"""
Gmail -> Dropbox -> Word pipeline

1. Find emails from a specific person in Gmail
2. Create a folder under the Dropbox directory:  "<CASE_ID>.<Name Surname>"
3. Download all attachments into that folder
4. Copy the Word template into the folder and replace the old client
   name(s) with the mail owner's name and surname.

Install:
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2 python-docx

Setup:
    - Enable the Gmail API in Google Cloud Console, create an OAuth client
      (Desktop app) and save it as credentials.json next to this script.
    - First run opens a browser for consent and stores token.json.
"""

import base64
import re
import shutil
import sys
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
SENDER_EMAIL = "kayhanboraisteps@gmail.com"          # the specific person's email address
CASE_ID = "533439"                           # folder prefix -> "533439.Name Surname"
DROPBOX_ROOT = Path(r"C:\Users\Kayhan\Dropbox\Cases")   # locally synced Dropbox folder
TEMPLATE_DOCX = Path(r"C:\Coding\APPLY_AIRLINE\MailDropbox\letter_template.docx")

# Text currently inside the Word template that must be replaced
OLD_NAME = "XXX and YYY"                     # exactly as it appears in the document

# Optional: also replace airport text, e.g. {"A Airport-IataCode": "Istanbul Airport-IST"}
EXTRA_REPLACEMENTS: dict[str, str] = {}

# Gmail search: only mails that have attachments
GMAIL_QUERY = f"from:{SENDER_EMAIL} has:attachment"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(r"C:\Coding\APPLY_AIRLINE\MailDropbox\credentials.json")
TOKEN_FILE = Path("token.json")


# ----------------------------------------------------------------------------
# Gmail
# ----------------------------------------------------------------------------
def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def find_messages(service, query):
    ids, page_token = [], None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def header(msg, name):
    for h in msg["payload"].get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def owner_name_from_message(msg):
    """Display name from the From header; falls back to the email local part."""
    display, addr = parseaddr(header(msg, "From"))
    name = display.strip().strip('"')
    if not name:
        name = re.sub(r"[._\-]+", " ", addr.split("@")[0])
    return " ".join(w.capitalize() for w in name.split())


def iter_attachment_parts(payload):
    if payload.get("filename") and (
        payload.get("body", {}).get("attachmentId") or payload.get("body", {}).get("data")
    ):
        yield payload
    for part in payload.get("parts", []) or []:
        yield from iter_attachment_parts(part)


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
# Word replacement
# ----------------------------------------------------------------------------
def replace_in_paragraph(paragraph, replacements: dict[str, str]):
    """Replace text even when Word split it across several runs; keeps the
    formatting of the run where the match starts."""
    for old, new in replacements.items():
        while True:
            runs = paragraph.runs
            full = "".join(r.text for r in runs)
            idx = full.find(old)
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
def main():
    service = get_gmail_service()

    ids = find_messages(service, GMAIL_QUERY)
    if not ids:
        sys.exit(f"No emails found for query: {GMAIL_QUERY}")
    print(f"Found {len(ids)} email(s) from {SENDER_EMAIL}")

    messages = [
        service.users().messages().get(userId="me", id=i, format="full").execute()
        for i in ids
    ]

    owner = owner_name_from_message(messages[0])
    folder = DROPBOX_ROOT / safe_filename(f"{CASE_ID}.{owner}")
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Folder: {folder}")

    for msg in messages:
        print(f"Message: {header(msg, 'Subject')!r}")
        download_attachments(service, msg, folder)

    replacements = {OLD_NAME: owner, **EXTRA_REPLACEMENTS}
    output_docx = folder / f"{safe_filename(owner)} - {TEMPLATE_DOCX.name}"
    build_letter(TEMPLATE_DOCX, unique_path(output_docx), replacements)
    print(f"Letter created: {output_docx.name}")


if __name__ == "__main__":
    main()