"""GSuite API client for Gmail, Calendar, and Drive."""

import json
import os
import base64
from email.mime.text import MIMEText
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from centaur_sdk.tool_sdk import secret


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    # Needed for creating folders and uploading into arbitrary Shared Drive parents.
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/bigquery.readonly",
]


_current_account: str | None = None


def set_account(account: str | None) -> None:
    """Set the current account to use for API calls."""
    global _current_account
    _current_account = account


def _get_credentials_from_secrets() -> Credentials | None:
    """Try to load credentials from GOOGLE_TOKEN_JSON secret (for sandbox/server use)."""
    try:
        token_json = secret("GOOGLE_TOKEN_JSON", None)
    except (KeyError, Exception):
        token_json = None
    if not token_json:
        return None

    creds = Credentials.from_authorized_user_info(json.loads(token_json))
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds if creds and creds.valid else None


def get_credentials() -> Credentials:
    """Get or refresh Google OAuth credentials.

    Resolution order:
    1. GOOGLE_TOKEN_JSON secret (sandbox/server mode — from 1Password)
    2. Per-account token: ~/.config/gsuite/tokens/<account>.json (if account is set)
    3. Default token: ~/.config/gsuite/token.json (fallback)
    4. ~/.config/gsuite/credentials.json (OAuth client credentials for new auth)

    Returns:
        Valid Google credentials
    """
    # 1. Try loading from secrets (works inside Docker sandboxes)
    creds = _get_credentials_from_secrets()
    if creds:
        return creds

    # 2. Fall back to filesystem token files (local dev)
    global _current_account
    config_dir = Path.home() / ".config" / "gsuite"
    tokens_dir = config_dir / "tokens"
    credentials_path = config_dir / "credentials.json"

    # Determine token path based on account
    if _current_account:
        token_path = tokens_dir / f"{_current_account}.json"
    else:
        # Try default.json in tokens dir, fall back to legacy token.json
        if (tokens_dir / "default.json").exists():
            token_path = tokens_dir / "default.json"
        else:
            token_path = config_dir / "token.json"

    creds = None

    if token_path.exists():
        # Load without forcing scopes - use whatever scopes the token was created with
        creds = Credentials.from_authorized_user_file(str(token_path))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Token refreshed - no need to save (original token file still valid)
        else:
            if not credentials_path.exists():
                raise RuntimeError(
                    f"OAuth credentials not found.\n"
                    f"1. Go to https://console.cloud.google.com/apis/credentials\n"
                    f"2. Create OAuth 2.0 Client ID (Desktop app or Web)\n"
                    f"3. Download JSON and save to: {credentials_path}\n"
                    f"4. Run 'gsuite auth --account <email>' to authenticate"
                )

            with open(credentials_path) as f:
                cred_data = json.load(f)

            if "web" in cred_data:
                # Web client - use fixed port 8085 (must be configured in GCP as http://localhost:8085)
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path),
                    SCOPES,
                    redirect_uri="http://localhost:8085",
                )
                creds = flow.run_local_server(
                    port=8085,
                    open_browser=True,
                    success_message="Authentication successful! You can close this window.",
                )
            else:
                # Desktop client - use any available port
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
                try:
                    creds = flow.run_local_server(port=0, open_browser=True)
                except Exception:
                    creds = flow.run_console()

            # Save newly created credentials (only after fresh auth, not after refresh)
            if _current_account:
                tokens_dir.mkdir(parents=True, exist_ok=True)
                with open(token_path, "w") as token:
                    token.write(creds.to_json())
            else:
                config_dir.mkdir(parents=True, exist_ok=True)
                with open(token_path, "w") as token:
                    token.write(creds.to_json())

    return creds


def get_gmail_service():
    """Get authenticated Gmail service."""
    return build("gmail", "v1", credentials=get_credentials())


def get_calendar_service():
    """Get authenticated Calendar service."""
    return build("calendar", "v3", credentials=get_credentials())


def get_drive_service():
    """Get authenticated Drive service."""
    return build("drive", "v3", credentials=get_credentials())


# Gmail functions


def gmail_search(query: str, max_results: int = 20) -> list[dict]:
    """Search Gmail messages.

    Args:
        query: Gmail search query (same syntax as Gmail web)
        max_results: Maximum number of results

    Returns:
        List of message dicts with id, subject, from, date, snippet
    """
    service = get_gmail_service()

    results = (
        service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    )

    messages = []
    for msg in results.get("messages", []):
        detail = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            )
            .execute()
        )

        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        messages.append(
            {
                "id": msg["id"],
                "thread_id": detail.get("threadId", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
                "label_ids": detail.get("labelIds", []),
            }
        )

    return messages


def gmail_read(message_id: str) -> dict:
    """Read a Gmail message.

    Args:
        message_id: The message ID

    Returns:
        Dict with id, subject, from, to, date, body (plain text)
    """
    service = get_gmail_service()

    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    def get_body(payload):
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")
        if payload.get("parts"):
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain":
                    if part.get("body", {}).get("data"):
                        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
                body = get_body(part)
                if body:
                    return body
        return ""

    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "cc": headers.get("Cc", ""),
        "date": headers.get("Date", ""),
        "body": get_body(msg.get("payload", {})),
        "label_ids": msg.get("labelIds", []),
    }


def gmail_send(to: str, subject: str, body: str, cc: str | None = None) -> dict:
    """Send an email.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body (plain text)
        cc: Optional CC recipients

    Returns:
        Dict with id, thread_id
    """
    service = get_gmail_service()

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    if cc:
        message["cc"] = cc

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()

    return {
        "id": result.get("id", ""),
        "thread_id": result.get("threadId", ""),
    }


def gmail_labels() -> list[dict]:
    """List Gmail labels.

    Returns:
        List of label dicts with id, name, type
    """
    service = get_gmail_service()

    results = service.users().labels().list(userId="me").execute()

    return [
        {
            "id": label["id"],
            "name": label["name"],
            "type": label.get("type", "user"),
        }
        for label in results.get("labels", [])
    ]


def gmail_archive(message_ids: list[str]) -> dict:
    """Archive Gmail messages (remove from INBOX).

    Args:
        message_ids: List of message IDs to archive

    Returns:
        Dict with count of archived messages
    """
    service = get_gmail_service()

    for msg_id in message_ids:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["INBOX"]},
        ).execute()

    return {"archived": len(message_ids)}


def gmail_delete(message_ids: list[str]) -> dict:
    """Delete Gmail messages (move to trash).

    Args:
        message_ids: List of message IDs to delete

    Returns:
        Dict with count of deleted messages
    """
    service = get_gmail_service()

    for msg_id in message_ids:
        service.users().messages().trash(userId="me", id=msg_id).execute()

    return {"deleted": len(message_ids)}


def gmail_reply(
    message_id: str,
    body: str,
    attachments: list[str] | None = None,
) -> dict:
    """Reply to a Gmail message.

    Args:
        message_id: The message ID to reply to
        body: Reply body (plain text)
        attachments: Optional list of file paths to attach

    Returns:
        Dict with id, thread_id
    """
    import mimetypes
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders

    service = get_gmail_service()

    original = gmail_read(message_id)

    reply_to = original["from"]
    if "<" in reply_to:
        reply_to = reply_to.split("<")[1].rstrip(">")

    subject = original["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    if attachments:
        message = MIMEMultipart()
        message.attach(MIMEText(body))

        for file_path in attachments:
            path = Path(file_path)
            if not path.exists():
                raise RuntimeError(f"Attachment not found: {file_path}")

            content_type, _ = mimetypes.guess_type(file_path)
            if content_type is None:
                content_type = "application/octet-stream"
            main_type, sub_type = content_type.split("/", 1)

            with open(file_path, "rb") as f:
                attachment = MIMEBase(main_type, sub_type)
                attachment.set_payload(f.read())
                encoders.encode_base64(attachment)
                attachment.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=path.name,
                )
                message.attach(attachment)
    else:
        message = MIMEText(body)

    message["to"] = reply_to
    message["subject"] = subject
    message["In-Reply-To"] = message_id
    message["References"] = message_id

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    result = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw, "threadId": original["thread_id"]})
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "thread_id": result.get("threadId", ""),
    }


# Calendar functions


def calendar_list() -> list[dict]:
    """List all calendars.

    Returns:
        List of calendar dicts with id, summary, primary, access_role, time_zone
    """
    service = get_calendar_service()

    results = service.calendarList().list().execute()

    return [
        {
            "id": cal["id"],
            "summary": cal.get("summary", ""),
            "primary": cal.get("primary", False),
            "access_role": cal.get("accessRole", ""),
            "time_zone": cal.get("timeZone", ""),
        }
        for cal in results.get("items", [])
    ]


def calendar_get_timezone(calendar_id: str = "primary") -> str:
    """Get the timezone of a calendar.

    Args:
        calendar_id: Calendar ID (default: primary)

    Returns:
        Timezone string (e.g., 'America/Los_Angeles', 'America/New_York')
    """
    service = get_calendar_service()
    calendar = service.calendarList().get(calendarId=calendar_id).execute()
    return calendar.get("timeZone", "UTC")


def calendar_events(
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 50,
    query: str | None = None,
) -> list[dict]:
    """List calendar events.

    Args:
        calendar_id: Calendar ID (default: primary)
        time_min: Start time in RFC3339 format (e.g., 2024-01-01T00:00:00Z)
        time_max: End time in RFC3339 format
        max_results: Maximum number of events
        query: Search query

    Returns:
        List of event dicts with id, summary, start, end, location, attendees
    """
    from datetime import datetime, timezone

    service = get_calendar_service()

    kwargs = {
        "calendarId": calendar_id,
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
    }

    if time_min:
        kwargs["timeMin"] = time_min
    else:
        kwargs["timeMin"] = datetime.now(timezone.utc).isoformat()

    if time_max:
        kwargs["timeMax"] = time_max
    if query:
        kwargs["q"] = query

    results = service.events().list(**kwargs).execute()

    events = []
    for event in results.get("items", []):
        start = event.get("start", {})
        end = event.get("end", {})

        # Determine if we have visibility into this event's details
        # Events with visibility="private" or from calendars where we only have
        # freeBusyReader access will have no summary, location, or attendees
        has_visibility = event.get("summary") is not None or event.get("visibility") != "private"

        if event.get("summary"):
            summary = event["summary"]
        elif event.get("visibility") == "private":
            summary = "[Private event]"
        else:
            # No summary and not marked private = we likely only have free/busy access
            summary = "[Busy - details not visible]"

        events.append(
            {
                "id": event["id"],
                "summary": summary,
                "start": start.get("dateTime") or start.get("date", ""),
                "end": end.get("dateTime") or end.get("date", ""),
                "location": event.get("location", ""),
                "description": event.get("description", ""),
                "attendees": [a.get("email", "") for a in event.get("attendees", [])],
                "html_link": event.get("htmlLink", ""),
                "color_id": event.get("colorId", ""),
                "has_visibility": has_visibility,
            }
        )

    return events


def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
) -> dict:
    """Create a calendar event.

    Args:
        summary: Event title
        start: Start time in RFC3339 format or date (YYYY-MM-DD)
        end: End time in RFC3339 format or date
        calendar_id: Calendar ID (default: primary)
        description: Event description
        location: Event location
        attendees: List of attendee emails

    Returns:
        Dict with id, html_link
    """
    service = get_calendar_service()

    event = {
        "summary": summary,
    }

    if "T" in start:
        event["start"] = {"dateTime": start, "timeZone": "UTC"}
        event["end"] = {"dateTime": end, "timeZone": "UTC"}
    else:
        event["start"] = {"date": start}
        event["end"] = {"date": end}

    if description:
        event["description"] = description
    if location:
        event["location"] = location
    if attendees:
        event["attendees"] = [{"email": email} for email in attendees]

    result = (
        service.events()
        .insert(
            calendarId=calendar_id,
            body=event,
            sendUpdates="all" if attendees else "none",
        )
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "html_link": result.get("htmlLink", ""),
    }


def calendar_update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    add_attendees: list[str] | None = None,
) -> dict:
    """Update a calendar event.

    Args:
        event_id: Event ID to update
        calendar_id: Calendar ID (default: primary)
        summary: New event title
        start: New start time in RFC3339 format or date
        end: New end time
        description: New description
        location: New location
        add_attendees: List of attendee emails to add

    Returns:
        Dict with id, html_link
    """
    service = get_calendar_service()

    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    if summary:
        event["summary"] = summary
    if description:
        event["description"] = description
    if location:
        event["location"] = location

    if start:
        if "T" in start:
            event["start"] = {"dateTime": start, "timeZone": "UTC"}
        else:
            event["start"] = {"date": start}
    if end:
        if "T" in end:
            event["end"] = {"dateTime": end, "timeZone": "UTC"}
        else:
            event["end"] = {"date": end}

    if add_attendees:
        existing = event.get("attendees", [])
        existing_emails = {a.get("email", "").lower() for a in existing}
        for email in add_attendees:
            if email.lower() not in existing_emails:
                existing.append({"email": email})
        event["attendees"] = existing

    result = (
        service.events()
        .update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event,
            sendUpdates="all" if add_attendees else "none",
        )
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "html_link": result.get("htmlLink", ""),
    }


def calendar_rsvp(
    event_id: str,
    response: str,
    calendar_id: str = "primary",
) -> dict:
    """RSVP to a calendar event.

    Args:
        event_id: Event ID
        response: One of 'accepted', 'declined', 'tentative'
        calendar_id: Calendar ID (default: primary)

    Returns:
        Dict with id, status
    """
    service = get_calendar_service()

    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    my_email = None
    for attendee in event.get("attendees", []):
        if attendee.get("self"):
            my_email = attendee.get("email")
            attendee["responseStatus"] = response
            break

    if not my_email:
        attendees = event.get("attendees", [])
        attendees.append({"email": "me", "responseStatus": response, "self": True})
        event["attendees"] = attendees

    result = (
        service.events()
        .update(calendarId=calendar_id, eventId=event_id, body=event, sendUpdates="all")
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "status": response,
    }


# Drive functions


def drive_list(
    query: str | None = None,
    folder_id: str | None = None,
    max_results: int = 50,
    file_type: str | None = None,
) -> list[dict]:
    """List files in Google Drive.

    Args:
        query: Search query (Drive query syntax)
        folder_id: Folder ID to list contents
        max_results: Maximum number of results
        file_type: Filter by MIME type prefix (e.g., "image/", "application/pdf")

    Returns:
        List of file dicts with id, name, mimeType, size, modifiedTime, webViewLink
    """
    service = get_drive_service()

    q_parts = []
    if query:
        q_parts.append(f"name contains '{query}'")
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    if file_type:
        if file_type.endswith("/"):
            q_parts.append(f"mimeType contains '{file_type}'")
        else:
            q_parts.append(f"mimeType = '{file_type}'")

    q_parts.append("trashed = false")

    kwargs = {
        "pageSize": max_results,
        "fields": "files(id, name, mimeType, size, modifiedTime, webViewLink, parents)",
        "q": " and ".join(q_parts) if q_parts else None,
        "includeItemsFromAllDrives": True,
        "supportsAllDrives": True,
    }

    results = service.files().list(**kwargs).execute()

    return [
        {
            "id": f["id"],
            "name": f["name"],
            "mime_type": f.get("mimeType", ""),
            "size": int(f.get("size", 0)),
            "modified_time": f.get("modifiedTime", ""),
            "web_view_link": f.get("webViewLink", ""),
            "parent_ids": f.get("parents", []),
        }
        for f in results.get("files", [])
    ]


def drive_download(file_id: str, output_path: str) -> str:
    """Download a file from Google Drive.

    Args:
        file_id: The file ID
        output_path: Local path to save the file

    Returns:
        The output path
    """
    import io

    service = get_drive_service()

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    with open(output_path, "wb") as f:
        f.write(fh.getvalue())

    return output_path


def drive_upload(
    file_path: str,
    name: str | None = None,
    folder_id: str | None = None,
    mime_type: str | None = None,
    convert_to_sheets: bool = False,
) -> dict:
    """Upload a file to Google Drive.

    Args:
        file_path: Local path to the file
        name: File name in Drive (defaults to local filename)
        folder_id: Parent folder ID
        mime_type: MIME type (auto-detected if not provided)
        convert_to_sheets: If True, convert the file to a Google Sheet (useful for CSV files)

    Returns:
        Dict with id, name, web_view_link
    """
    import mimetypes

    service = get_drive_service()

    path = Path(file_path)
    if not path.exists():
        raise RuntimeError(f"File not found: {file_path}")

    file_name = name or path.name
    content_type = mime_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    metadata = {"name": file_name}
    if folder_id:
        metadata["parents"] = [folder_id]
    if convert_to_sheets:
        # Tell Google Drive to convert the uploaded file to a native Google Sheet
        metadata["mimeType"] = "application/vnd.google-apps.spreadsheet"

    media = MediaFileUpload(file_path, mimetype=content_type, resumable=True)

    result = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "name": result.get("name", ""),
        "web_view_link": result.get("webViewLink", ""),
    }


def drive_create_folder(name: str, parent_id: str | None = None) -> dict:
    """Create a Google Drive folder.

    Args:
        name: Folder name
        parent_id: Optional parent folder ID

    Returns:
        Dict with id, name, web_view_link, parent_ids
    """
    service = get_drive_service()

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    result = (
        service.files()
        .create(
            body=metadata,
            fields="id, name, webViewLink, parents",
            supportsAllDrives=True,
        )
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "name": result.get("name", ""),
        "web_view_link": result.get("webViewLink", ""),
        "parent_ids": result.get("parents", []),
    }


def drive_get(file_id: str) -> dict:
    """Get file metadata from Google Drive.

    Args:
        file_id: The file ID

    Returns:
        Dict with file metadata
    """
    service = get_drive_service()

    result = (
        service.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, size, modifiedTime, webViewLink, parents, owners",
            supportsAllDrives=True,
        )
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "name": result.get("name", ""),
        "mime_type": result.get("mimeType", ""),
        "size": int(result.get("size", 0)),
        "modified_time": result.get("modifiedTime", ""),
        "web_view_link": result.get("webViewLink", ""),
        "parent_ids": result.get("parents", []),
        "owners": [o.get("emailAddress", "") for o in result.get("owners", [])],
    }


def drive_list_permissions(file_id: str) -> list[dict]:
    """List permissions on a Google Drive file.

    Args:
        file_id: The file ID

    Returns:
        List of permission dicts with id, type, role, emailAddress
    """
    service = get_drive_service()

    result = (
        service.permissions()
        .list(
            fileId=file_id,
            fields="permissions(id, type, role, emailAddress, displayName)",
            supportsAllDrives=True,
        )
        .execute()
    )

    return [
        {
            "id": p.get("id", ""),
            "type": p.get("type", ""),
            "role": p.get("role", ""),
            "email": p.get("emailAddress", ""),
            "display_name": p.get("displayName", ""),
        }
        for p in result.get("permissions", [])
    ]


def drive_share(
    file_id: str,
    email: str,
    role: str = "writer",
    send_notification: bool = False,
) -> dict:
    """Share a Google Drive file with a user.

    Args:
        file_id: The file ID
        email: Email address of the user to share with
        role: Permission role ('reader', 'writer', 'commenter')
        send_notification: Whether to send email notification

    Returns:
        Dict with permission id and role
    """
    service = get_drive_service()

    permission = {
        "type": "user",
        "role": role,
        "emailAddress": email,
    }

    result = (
        service.permissions()
        .create(
            fileId=file_id,
            body=permission,
            sendNotificationEmail=send_notification,
            supportsAllDrives=True,
        )
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "role": result.get("role", ""),
        "email": email,
    }


def drive_transfer_ownership(file_id: str, new_owner_email: str) -> dict:
    """Transfer ownership of a Google Drive file to another user.

    Note: Both users must be in the same Google Workspace domain.
    The new owner must already have access to the file (add them first with drive_share).

    Args:
        file_id: The file ID
        new_owner_email: Email address of the new owner

    Returns:
        Dict with permission id and new role
    """
    service = get_drive_service()

    # First, find the permission ID for the new owner
    permissions = drive_list_permissions(file_id)
    permission_id = None
    for p in permissions:
        if p.get("email", "").lower() == new_owner_email.lower():
            permission_id = p.get("id")
            break

    if not permission_id:
        # User doesn't have access yet, add them first
        share_result = drive_share(file_id, new_owner_email, role="writer")
        permission_id = share_result.get("id")

    # Now transfer ownership
    result = (
        service.permissions()
        .update(
            fileId=file_id,
            permissionId=permission_id,
            body={"role": "owner"},
            transferOwnership=True,
            supportsAllDrives=True,
        )
        .execute()
    )

    return {
        "id": result.get("id", ""),
        "role": result.get("role", ""),
        "email": new_owner_email,
    }


def drive_remove_permission(file_id: str, email: str) -> bool:
    """Remove a user's permission from a Google Drive file.

    Args:
        file_id: The file ID
        email: Email address of the user to remove

    Returns:
        True if permission was removed, False if user had no permission
    """
    service = get_drive_service()

    # Find the permission ID for the user
    permissions = drive_list_permissions(file_id)
    permission_id = None
    for p in permissions:
        if p.get("email", "").lower() == email.lower():
            permission_id = p.get("id")
            break

    if not permission_id:
        return False

    service.permissions().delete(
        fileId=file_id,
        permissionId=permission_id,
        supportsAllDrives=True,
    ).execute()

    return True


def drive_add_label(file_id: str, label_id: str) -> dict:
    """Apply a label to a Google Drive file.

    Uses the Drive API files.modifyLabels endpoint to apply a label.
    To apply a label with no fields (e.g., a simple badge label like "confidential"),
    provide only the label_id with no field modifications.

    Args:
        file_id: The file ID
        label_id: The label ID to apply

    Returns:
        Dict with applied label info
    """
    service = get_drive_service()

    label_modification = {"labelId": label_id}
    body = {"labelModifications": [label_modification]}

    result = service.files().modifyLabels(fileId=file_id, body=body).execute()

    modified = result.get("modifiedLabels", [])
    return {
        "file_id": file_id,
        "label_id": label_id,
        "applied": len(modified) > 0,
        "modified_labels": modified,
    }


def drive_remove_label(file_id: str, label_id: str) -> dict:
    """Remove a label from a Google Drive file.

    Args:
        file_id: The file ID
        label_id: The label ID to remove

    Returns:
        Dict with removal info
    """
    service = get_drive_service()

    label_modification = {"labelId": label_id, "removeLabel": True}
    body = {"labelModifications": [label_modification]}

    result = service.files().modifyLabels(fileId=file_id, body=body).execute()

    modified = result.get("modifiedLabels", [])
    return {
        "file_id": file_id,
        "label_id": label_id,
        "removed": True,
        "modified_labels": modified,
    }


def drive_label_folder(
    folder_id: str,
    label_id: str,
    recursive: bool = True,
) -> dict:
    """Apply a label to all files in a folder or Shared Drive.

    Recursively walks through all files in the given folder (or Shared Drive)
    and applies the specified label to each file.

    Args:
        folder_id: The folder ID or Shared Drive ID
        label_id: The label ID to apply (e.g., confidential label)
        recursive: Whether to recurse into subfolders (default: True)

    Returns:
        Dict with labeled (count), failed (count), errors (list), files (list of names)
    """
    service = get_drive_service()

    labeled = []
    failed = []
    folder_mime = "application/vnd.google-apps.folder"

    def process_folder(fid: str) -> None:
        page_token = None
        while True:
            results = (
                service.files()
                .list(
                    q=f"'{fid}' in parents and trashed = false",
                    pageSize=100,
                    fields="nextPageToken, files(id, name, mimeType)",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                )
                .execute()
            )

            for f in results.get("files", []):
                if f["mimeType"] == folder_mime:
                    if recursive:
                        process_folder(f["id"])
                    continue

                try:
                    drive_add_label(f["id"], label_id)
                    labeled.append({"id": f["id"], "name": f["name"]})
                except Exception as e:
                    failed.append({"id": f["id"], "name": f["name"], "error": str(e)})

            page_token = results.get("nextPageToken")
            if not page_token:
                break

    process_folder(folder_id)

    return {
        "labeled": len(labeled),
        "failed": len(failed),
        "files": labeled,
        "errors": failed,
    }


def drive_setup_channel_permissions(
    file_id: str,
    channel_member_emails: list[str],
    requester_email: str,
) -> dict:
    """Set up file permissions for Slack channel members and transfer ownership.

    This function:
    1. Shares the file with all channel members (writer role)
    2. Transfers ownership to the requester

    Note: The original owner (service account) is automatically downgraded to
    editor by Google Drive when ownership is transferred, and retains access.
    An Okta Workflows configuration removes the service account's editor role
    permissions after 7 days.

    Args:
        file_id: The Google Drive file ID
        channel_member_emails: List of email addresses for channel members
            (obtained from Slack via get_channel_members_with_emails)
        requester_email: Email of the person who requested the file (new owner)

    Returns:
        Dict with results: shared_with, new_owner, errors
    """
    results = {
        "shared_with": [],
        "share_errors": [],
        "new_owner": None,
        "ownership_error": None,
    }

    # 1. Share with all channel members
    for email in channel_member_emails:
        try:
            drive_share(file_id, email, role="writer", send_notification=False)
            results["shared_with"].append(email)
        except Exception as e:
            results["share_errors"].append({"email": email, "error": str(e)})

    # 2. Transfer ownership to requester
    # Note: Original owner (service account) is automatically downgraded to editor
    if requester_email:
        try:
            drive_transfer_ownership(file_id, requester_email)
            results["new_owner"] = requester_email
        except Exception as e:
            results["ownership_error"] = str(e)

    return results


def drive_export(file_id: str, export_format: str = "txt", output_path: str | None = None) -> str:
    """Export a Google Docs/Sheets/Slides file to a specific format.

    Args:
        file_id: The file ID
        export_format: Export format (txt, pdf, docx, html, csv, xlsx, pptx)
        output_path: Optional output path (defaults to temp file)

    Returns:
        The output path
    """
    import io
    import tempfile

    service = get_drive_service()

    # Map format to MIME type
    format_map = {
        "txt": "text/plain",
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "html": "text/html",
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "md": "text/markdown",
    }

    mime_type = format_map.get(export_format)
    if not mime_type:
        raise ValueError(
            f"Unsupported export format: {export_format}. Supported: {list(format_map.keys())}"
        )

    # Determine output path
    if not output_path:
        output_path = tempfile.mktemp(suffix=f".{export_format}")

    # Export the file
    request = service.files().export_media(fileId=file_id, mimeType=mime_type)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    with open(output_path, "wb") as f:
        f.write(fh.getvalue())

    return output_path


# Docs functions


def get_docs_service():
    """Get authenticated Docs service."""
    return build("docs", "v1", credentials=get_credentials())


def docs_get(document_id: str, include_tabs: bool = True) -> dict:
    """Get a Google Doc.

    Args:
        document_id: The document ID
        include_tabs: Whether to include all tabs content

    Returns:
        Dict with document metadata and content
    """
    service = get_docs_service()

    result = (
        service.documents().get(documentId=document_id, includeTabsContent=include_tabs).execute()
    )

    return result


def docs_get_text(document_id: str) -> str:
    """Get plain text content from a Google Doc.

    Args:
        document_id: The document ID

    Returns:
        Plain text content of the document
    """
    doc = docs_get(document_id)

    def extract_text_from_content(content: list) -> str:
        text_parts = []
        for element in content:
            if "paragraph" in element:
                for para_element in element["paragraph"].get("elements", []):
                    if "textRun" in para_element:
                        text_parts.append(para_element["textRun"].get("content", ""))
            elif "table" in element:
                for row in element["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        cell_content = cell.get("content", [])
                        text_parts.append(extract_text_from_content(cell_content))
        return "".join(text_parts)

    if doc.get("tabs"):
        all_text = []
        for tab in doc["tabs"]:
            doc_tab = tab.get("documentTab", {})
            body = doc_tab.get("body", {})
            content = body.get("content", [])
            all_text.append(extract_text_from_content(content))
        return "\n".join(all_text)
    else:
        body = doc.get("body", {})
        content = body.get("content", [])
        return extract_text_from_content(content)


def docs_append(document_id: str, text: str, tab_id: str | None = None) -> dict:
    """Append text to a Google Doc.

    Args:
        document_id: The document ID
        text: Text to append
        tab_id: Optional tab ID to append to

    Returns:
        Dict with document ID
    """
    service = get_docs_service()

    requests = [{"insertText": {"location": {"index": 1}, "text": text}}]

    if tab_id:
        requests[0]["insertText"]["location"]["tabId"] = tab_id

    result = (
        service.documents()
        .batchUpdate(documentId=document_id, body={"requests": requests})
        .execute()
    )

    return {"document_id": result.get("documentId", "")}


def docs_insert_page_break(document_id: str, index: int = 1) -> dict:
    """Insert a page break in a Google Doc.

    Args:
        document_id: The document ID
        index: Position to insert (1 = beginning)

    Returns:
        Dict with document ID
    """
    service = get_docs_service()

    requests = [{"insertPageBreak": {"location": {"index": index}}}]

    result = (
        service.documents()
        .batchUpdate(documentId=document_id, body={"requests": requests})
        .execute()
    )

    return {"document_id": result.get("documentId", "")}


def docs_batch_update(document_id: str, requests: list) -> dict:
    """Execute batch update on a Google Doc.

    Args:
        document_id: The document ID
        requests: List of request objects

    Returns:
        Dict with document ID and replies
    """
    service = get_docs_service()

    result = (
        service.documents()
        .batchUpdate(documentId=document_id, body={"requests": requests})
        .execute()
    )

    return {
        "document_id": result.get("documentId", ""),
        "replies": result.get("replies", []),
    }


def docs_replace(document_id: str, old_text: str, new_text: str) -> dict:
    """Find and replace text in a Google Doc.

    Args:
        document_id: The document ID
        old_text: Text to find
        new_text: Text to replace with

    Returns:
        Dict with document ID and occurrences replaced
    """
    service = get_docs_service()

    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": old_text, "matchCase": True},
                "replaceText": new_text,
            }
        }
    ]

    result = (
        service.documents()
        .batchUpdate(documentId=document_id, body={"requests": requests})
        .execute()
    )

    occurrences = 0
    for reply in result.get("replies", []):
        if "replaceAllText" in reply:
            occurrences = reply["replaceAllText"].get("occurrencesChanged", 0)

    return {
        "document_id": result.get("documentId", ""),
        "occurrences_replaced": occurrences,
    }


def docs_insert(document_id: str, text: str, index: int) -> dict:
    """Insert text at a specific position in a Google Doc.

    Args:
        document_id: The document ID
        text: Text to insert
        index: Position to insert at (1 = beginning)

    Returns:
        Dict with document ID
    """
    service = get_docs_service()

    requests = [{"insertText": {"location": {"index": index}, "text": text}}]

    result = (
        service.documents()
        .batchUpdate(documentId=document_id, body={"requests": requests})
        .execute()
    )

    return {"document_id": result.get("documentId", "")}


def docs_create(title: str, content: str | None = None) -> dict:
    """Create a new Google Doc.

    Args:
        title: Document title
        content: Optional initial content to add

    Returns:
        Dict with document_id, title, and url
    """
    service = get_docs_service()

    doc = service.documents().create(body={"title": title}).execute()
    document_id = doc.get("documentId", "")

    if content:
        requests = [{"insertText": {"location": {"index": 1}, "text": content}}]
        service.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()

    return {
        "document_id": document_id,
        "title": doc.get("title", ""),
        "url": f"https://docs.google.com/document/d/{document_id}/edit",
    }


# Sheets functions


def get_sheets_service():
    """Get authenticated Sheets service."""
    return build("sheets", "v4", credentials=get_credentials())


def sheets_create(title: str, content: list[list[str]] | None = None) -> dict:
    """Create a new Google Sheet.

    Args:
        title: Spreadsheet title
        content: Optional 2D array of initial data (rows x cols)

    Returns:
        Dict with spreadsheet_id, title, and url
    """
    service = get_sheets_service()

    spreadsheet = service.spreadsheets().create(body={"properties": {"title": title}}).execute()
    spreadsheet_id = spreadsheet.get("spreadsheetId", "")

    if content:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="A1",
            valueInputOption="RAW",
            body={"values": content},
        ).execute()

    return {
        "spreadsheet_id": spreadsheet_id,
        "title": spreadsheet.get("properties", {}).get("title", ""),
        "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
    }


def sheets_read(spreadsheet_id: str, range_notation: str = "A1:Z1000") -> dict:
    """Read data from a Google Sheet.

    Args:
        spreadsheet_id: The spreadsheet ID (from URL)
        range_notation: A1 notation range (e.g., "Sheet1!A1:D10" or "A1:Z1000")

    Returns:
        Dict with spreadsheet_id, range, headers, and rows (list of dicts)
    """
    service = get_sheets_service()

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_notation)
        .execute()
    )

    values = result.get("values", [])

    if not values:
        return {
            "spreadsheet_id": spreadsheet_id,
            "range": range_notation,
            "headers": [],
            "rows": [],
            "raw_values": [],
        }

    headers = values[0] if values else []
    rows = []
    for row_values in values[1:]:
        row_dict = {}
        for i, header in enumerate(headers):
            row_dict[header] = row_values[i] if i < len(row_values) else ""
        rows.append(row_dict)

    return {
        "spreadsheet_id": spreadsheet_id,
        "range": result.get("range", range_notation),
        "headers": headers,
        "rows": rows,
        "raw_values": values,
    }


def sheets_update(
    spreadsheet_id: str,
    range_notation: str,
    values: list[list[str]],
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Update data in a Google Sheet.

    Args:
        spreadsheet_id: The spreadsheet ID (from URL)
        range_notation: A1 notation range (e.g., "Sheet1!A1:D10" or "B5")
        values: 2D array of values to write
        value_input_option: How to interpret input ("RAW" or "USER_ENTERED")

    Returns:
        Dict with updated_range, updated_rows, updated_columns, updated_cells
    """
    service = get_sheets_service()

    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_notation,
            valueInputOption=value_input_option,
            body={"values": values},
        )
        .execute()
    )

    return {
        "spreadsheet_id": spreadsheet_id,
        "updated_range": result.get("updatedRange", ""),
        "updated_rows": result.get("updatedRows", 0),
        "updated_columns": result.get("updatedColumns", 0),
        "updated_cells": result.get("updatedCells", 0),
    }


def sheets_batch_update(
    spreadsheet_id: str,
    updates: list[dict],
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """Batch update multiple ranges in a Google Sheet.

    Args:
        spreadsheet_id: The spreadsheet ID (from URL)
        updates: List of dicts with "range" and "values" keys
        value_input_option: How to interpret input ("RAW" or "USER_ENTERED")

    Returns:
        Dict with total_updated_cells and responses
    """
    service = get_sheets_service()

    data = [{"range": u["range"], "values": u["values"]} for u in updates]

    result = (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": value_input_option, "data": data},
        )
        .execute()
    )

    return {
        "spreadsheet_id": spreadsheet_id,
        "total_updated_cells": result.get("totalUpdatedCells", 0),
        "total_updated_rows": result.get("totalUpdatedRows", 0),
        "total_updated_columns": result.get("totalUpdatedColumns", 0),
        "responses": result.get("responses", []),
    }


# Slides functions


def get_slides_service():
    """Get authenticated Slides service."""
    return build("slides", "v1", credentials=get_credentials())


def slides_create(title: str) -> dict:
    """Create a new Google Slides presentation.

    Args:
        title: Presentation title

    Returns:
        Dict with presentation_id, title, and url
    """
    service = get_slides_service()

    presentation = service.presentations().create(body={"title": title}).execute()
    presentation_id = presentation.get("presentationId", "")

    return {
        "presentation_id": presentation_id,
        "title": presentation.get("title", ""),
        "url": f"https://docs.google.com/presentation/d/{presentation_id}/edit",
    }


# Google Analytics functions

_analytics_property_id: str | None = None


def set_analytics_property(property_id: str | None) -> None:
    """Set the GA4 property ID to use."""
    global _analytics_property_id
    _analytics_property_id = property_id


def get_analytics_property_id() -> str:
    """Get the current GA4 property ID."""
    if _analytics_property_id:
        return _analytics_property_id
    env_prop = os.environ.get("GA_PROPERTY_ID")  # noqa: TID251
    if env_prop:
        return env_prop
    raise RuntimeError(
        "No GA4 property ID set. Use --site or --property flag, or set GA_PROPERTY_ID env var.\n"
        "Find your property ID in Google Analytics: Admin > Property Settings"
    )


def get_analytics_client():
    """Get authenticated Analytics Data API client."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    creds = get_credentials()
    return BetaAnalyticsDataClient(credentials=creds)


def analytics_run_report(
    dimensions: list[str],
    metrics: list[str],
    start_date: str = "30daysAgo",
    end_date: str = "today",
    limit: int = 100,
    order_by: list | None = None,
) -> dict:
    """Run a report on the GA4 property.

    Args:
        dimensions: List of dimension names (e.g., ['country', 'deviceCategory'])
        metrics: List of metric names (e.g., ['activeUsers', 'sessions'])
        start_date: Start date (YYYY-MM-DD, 'today', 'yesterday', 'NdaysAgo')
        end_date: End date
        limit: Max rows to return
        order_by: Optional ordering

    Returns:
        Dict with headers and rows
    """
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )

    client = get_analytics_client()
    property_id = get_analytics_property_id()

    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=limit,
        order_bys=order_by or [],
    )

    response = client.run_report(request)

    headers = {
        "dimensions": [d.name for d in response.dimension_headers],
        "metrics": [m.name for m in response.metric_headers],
    }

    rows = []
    for row in response.rows:
        row_data = {
            "dimensions": {
                headers["dimensions"][i]: v.value for i, v in enumerate(row.dimension_values)
            },
            "metrics": {headers["metrics"][i]: v.value for i, v in enumerate(row.metric_values)},
        }
        rows.append(row_data)

    return {
        "headers": headers,
        "rows": rows,
        "row_count": response.row_count,
    }


def analytics_run_realtime_report(
    dimensions: list[str],
    metrics: list[str],
    limit: int = 100,
) -> dict:
    """Run a realtime report (last 30 minutes).

    Args:
        dimensions: List of dimension names
        metrics: List of metric names
        limit: Max rows to return

    Returns:
        Dict with headers and rows
    """
    from google.analytics.data_v1beta.types import (
        Dimension,
        Metric,
        RunRealtimeReportRequest,
    )

    client = get_analytics_client()
    property_id = get_analytics_property_id()

    request = RunRealtimeReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
        limit=limit,
    )

    response = client.run_realtime_report(request)

    headers = {
        "dimensions": [d.name for d in response.dimension_headers],
        "metrics": [m.name for m in response.metric_headers],
    }

    rows = []
    for row in response.rows:
        row_data = {
            "dimensions": {
                headers["dimensions"][i]: v.value for i, v in enumerate(row.dimension_values)
            },
            "metrics": {headers["metrics"][i]: v.value for i, v in enumerate(row.metric_values)},
        }
        rows.append(row_data)

    return {
        "headers": headers,
        "rows": rows,
        "row_count": response.row_count,
    }


def analytics_get_summary(
    start_date: str = "30daysAgo",
    end_date: str = "today",
) -> dict:
    """Get summary metrics for the GA4 property.

    Returns:
        Dict with key metrics
    """
    result = analytics_run_report(
        dimensions=[],
        metrics=[
            "activeUsers",
            "newUsers",
            "sessions",
            "screenPageViews",
            "bounceRate",
            "averageSessionDuration",
            "engagementRate",
        ],
        start_date=start_date,
        end_date=end_date,
        limit=1,
    )

    if result["rows"]:
        return result["rows"][0]["metrics"]
    return {}


def analytics_get_traffic_by_source(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    limit: int = 20,
) -> dict:
    """Get traffic breakdown by source/medium."""
    from google.analytics.data_v1beta.types import OrderBy

    return analytics_run_report(
        dimensions=["sessionSourceMedium"],
        metrics=["sessions", "activeUsers", "bounceRate", "averageSessionDuration"],
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        order_by=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                desc=True,
            )
        ],
    )


def analytics_get_traffic_by_channel(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    limit: int = 20,
) -> dict:
    """Get traffic breakdown by default channel grouping."""
    from google.analytics.data_v1beta.types import OrderBy

    return analytics_run_report(
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["sessions", "activeUsers", "newUsers", "engagedSessions"],
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        order_by=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                desc=True,
            )
        ],
    )


def analytics_get_top_pages(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    limit: int = 20,
) -> dict:
    """Get top pages by views."""
    from google.analytics.data_v1beta.types import OrderBy

    return analytics_run_report(
        dimensions=["pagePath"],
        metrics=["screenPageViews", "activeUsers", "averageSessionDuration"],
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        order_by=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"),
                desc=True,
            )
        ],
    )


def analytics_get_traffic_by_device(
    start_date: str = "30daysAgo",
    end_date: str = "today",
) -> dict:
    """Get traffic breakdown by device category."""
    from google.analytics.data_v1beta.types import OrderBy

    return analytics_run_report(
        dimensions=["deviceCategory"],
        metrics=["sessions", "activeUsers", "bounceRate"],
        start_date=start_date,
        end_date=end_date,
        limit=10,
        order_by=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                desc=True,
            )
        ],
    )


def analytics_get_traffic_by_country(
    start_date: str = "30daysAgo",
    end_date: str = "today",
    limit: int = 20,
) -> dict:
    """Get traffic breakdown by country."""
    from google.analytics.data_v1beta.types import OrderBy

    return analytics_run_report(
        dimensions=["country"],
        metrics=["sessions", "activeUsers", "newUsers"],
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        order_by=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                desc=True,
            )
        ],
    )


def analytics_get_daily_users(
    start_date: str = "30daysAgo",
    end_date: str = "today",
) -> dict:
    """Get daily active users over time."""
    from google.analytics.data_v1beta.types import OrderBy

    return analytics_run_report(
        dimensions=["date"],
        metrics=["activeUsers", "newUsers", "sessions"],
        start_date=start_date,
        end_date=end_date,
        limit=366,
        order_by=[
            OrderBy(
                dimension=OrderBy.DimensionOrderBy(dimension_name="date"),
                desc=False,
            )
        ],
    )


class GSuiteClient:
    """GSuite API client wrapping Gmail, Calendar, Drive, Docs, Sheets, Slides, and Analytics."""

    # --- Gmail ---

    def gmail_search(self, query: str, max_results: int = 20) -> list[dict]:
        """Search Gmail messages.

        Args:
            query: Gmail search query (same syntax as Gmail web)
            max_results: Maximum number of results

        Returns:
            List of message dicts with id, subject, from, date, snippet
        """
        return gmail_search(query, max_results=max_results)

    def gmail_get(self, message_id: str) -> dict:
        """Read a Gmail message.

        Args:
            message_id: The message ID

        Returns:
            Dict with id, subject, from, to, date, body (plain text)
        """
        return gmail_read(message_id)

    def gmail_send(
        self, to: str, subject: str, body: str, cc: str | None = None
    ) -> dict:
        """Send an email.

        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body (plain text)
            cc: Optional CC recipients

        Returns:
            Dict with id, thread_id
        """
        return gmail_send(to, subject, body, cc=cc)

    def gmail_labels(self) -> list[dict]:
        """List Gmail labels.

        Returns:
            List of label dicts with id, name, type
        """
        return gmail_labels()

    def gmail_archive(self, message_ids: list[str]) -> dict:
        """Archive Gmail messages (remove from INBOX).

        Args:
            message_ids: List of message IDs to archive

        Returns:
            Dict with count of archived messages
        """
        return gmail_archive(message_ids)

    def gmail_delete(self, message_ids: list[str]) -> dict:
        """Delete Gmail messages (move to trash).

        Args:
            message_ids: List of message IDs to delete

        Returns:
            Dict with count of deleted messages
        """
        return gmail_delete(message_ids)

    def gmail_reply(
        self,
        message_id: str,
        body: str,
        attachments: list[str] | None = None,
    ) -> dict:
        """Reply to a Gmail message.

        Args:
            message_id: The message ID to reply to
            body: Reply body (plain text)
            attachments: Optional list of file paths to attach

        Returns:
            Dict with id, thread_id
        """
        return gmail_reply(message_id, body, attachments=attachments)

    # --- Calendar ---

    def calendar_list(self) -> list[dict]:
        """List all calendars.

        Returns:
            List of calendar dicts with id, summary, primary, access_role, time_zone
        """
        return calendar_list()

    def calendar_get_timezone(self, calendar_id: str = "primary") -> str:
        """Get the timezone of a calendar.

        Args:
            calendar_id: Calendar ID (default: primary)

        Returns:
            Timezone string (e.g., 'America/Los_Angeles', 'America/New_York')
        """
        return calendar_get_timezone(calendar_id)

    def calendar_events(
        self,
        calendar_id: str = "primary",
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 50,
        query: str | None = None,
    ) -> list[dict]:
        """List calendar events.

        Args:
            calendar_id: Calendar ID (default: primary)
            time_min: Start time in RFC3339 format (e.g., 2024-01-01T00:00:00Z)
            time_max: End time in RFC3339 format
            max_results: Maximum number of events
            query: Search query

        Returns:
            List of event dicts with id, summary, start, end, location, attendees
        """
        return calendar_events(
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            query=query,
        )

    def calendar_create_event(
        self,
        summary: str,
        start: str,
        end: str,
        calendar_id: str = "primary",
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict:
        """Create a calendar event.

        Args:
            summary: Event title
            start: Start time in RFC3339 format or date (YYYY-MM-DD)
            end: End time in RFC3339 format or date
            calendar_id: Calendar ID (default: primary)
            description: Event description
            location: Event location
            attendees: List of attendee emails

        Returns:
            Dict with id, html_link
        """
        return calendar_create_event(
            summary,
            start,
            end,
            calendar_id=calendar_id,
            description=description,
            location=location,
            attendees=attendees,
        )

    def calendar_update_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        add_attendees: list[str] | None = None,
    ) -> dict:
        """Update a calendar event.

        Args:
            event_id: Event ID to update
            calendar_id: Calendar ID (default: primary)
            summary: New event title
            start: New start time in RFC3339 format or date
            end: New end time
            description: New description
            location: New location
            add_attendees: List of attendee emails to add

        Returns:
            Dict with id, html_link
        """
        return calendar_update_event(
            event_id,
            calendar_id=calendar_id,
            summary=summary,
            start=start,
            end=end,
            description=description,
            location=location,
            add_attendees=add_attendees,
        )

    def calendar_rsvp(
        self,
        event_id: str,
        response: str,
        calendar_id: str = "primary",
    ) -> dict:
        """RSVP to a calendar event.

        Args:
            event_id: Event ID
            response: One of 'accepted', 'declined', 'tentative'
            calendar_id: Calendar ID (default: primary)

        Returns:
            Dict with id, status
        """
        return calendar_rsvp(event_id, response, calendar_id=calendar_id)

    # --- Drive ---

    def drive_list(
        self,
        query: str | None = None,
        folder_id: str | None = None,
        max_results: int = 50,
        file_type: str | None = None,
    ) -> list[dict]:
        """List files in Google Drive.

        Args:
            query: Search query (Drive query syntax)
            folder_id: Folder ID to list contents
            max_results: Maximum number of results
            file_type: Filter by MIME type prefix (e.g., "image/", "application/pdf")

        Returns:
            List of file dicts with id, name, mimeType, size, modifiedTime, webViewLink
        """
        return drive_list(
            query=query,
            folder_id=folder_id,
            max_results=max_results,
            file_type=file_type,
        )

    def drive_search(self, query: str, max_results: int = 50) -> list[dict]:
        """Search files in Google Drive by name.

        Args:
            query: Search query (matches file names)
            max_results: Maximum number of results

        Returns:
            List of file dicts with id, name, mimeType, size, modifiedTime, webViewLink
        """
        return drive_list(query=query, max_results=max_results)

    def drive_get(self, file_id: str) -> dict:
        """Get file metadata from Google Drive.

        Args:
            file_id: The file ID

        Returns:
            Dict with file metadata
        """
        return drive_get(file_id)

    def drive_download(self, file_id: str, output_path: str) -> str:
        """Download a file from Google Drive.

        Args:
            file_id: The file ID
            output_path: Local path to save the file

        Returns:
            The output path
        """
        return drive_download(file_id, output_path)

    def drive_upload(
        self,
        file_path: str,
        name: str | None = None,
        folder_id: str | None = None,
        mime_type: str | None = None,
        convert_to_sheets: bool = False,
    ) -> dict:
        """Upload a file to Google Drive.

        Args:
            file_path: Local path to the file
            name: File name in Drive (defaults to local filename)
            folder_id: Parent folder ID
            mime_type: MIME type (auto-detected if not provided)
            convert_to_sheets: If True, convert the file to a Google Sheet (useful for CSV files)

        Returns:
            Dict with id, name, web_view_link
        """
        return drive_upload(
            file_path,
            name=name,
            folder_id=folder_id,
            mime_type=mime_type,
            convert_to_sheets=convert_to_sheets,
        )

    def drive_create_folder(self, name: str, parent_id: str | None = None) -> dict:
        """Create a Google Drive folder.

        Args:
            name: Folder name
            parent_id: Optional parent folder ID

        Returns:
            Dict with id, name, web_view_link, parent_ids
        """
        return drive_create_folder(name, parent_id=parent_id)

    def drive_list_permissions(self, file_id: str) -> list[dict]:
        """List permissions on a Google Drive file.

        Args:
            file_id: The file ID

        Returns:
            List of permission dicts with id, type, role, emailAddress
        """
        return drive_list_permissions(file_id)

    def drive_share(
        self,
        file_id: str,
        email: str,
        role: str = "writer",
        send_notification: bool = False,
    ) -> dict:
        """Share a Google Drive file with a user.

        Args:
            file_id: The file ID
            email: Email address of the user to share with
            role: Permission role ('reader', 'writer', 'commenter')
            send_notification: Whether to send email notification

        Returns:
            Dict with permission id and role
        """
        return drive_share(file_id, email, role=role, send_notification=send_notification)

    def drive_transfer_ownership(self, file_id: str, new_owner_email: str) -> dict:
        """Transfer ownership of a Google Drive file to another user.

        Note: Both users must be in the same Google Workspace domain.
        The new owner must already have access to the file (add them first with drive_share).

        Args:
            file_id: The file ID
            new_owner_email: Email address of the new owner

        Returns:
            Dict with permission id and new role
        """
        return drive_transfer_ownership(file_id, new_owner_email)

    def drive_remove_permission(self, file_id: str, email: str) -> bool:
        """Remove a user's permission from a Google Drive file.

        Args:
            file_id: The file ID
            email: Email address of the user to remove

        Returns:
            True if permission was removed, False if user had no permission
        """
        return drive_remove_permission(file_id, email)

    def drive_add_label(self, file_id: str, label_id: str) -> dict:
        """Apply a label to a Google Drive file.

        Args:
            file_id: The file ID
            label_id: The label ID to apply

        Returns:
            Dict with applied label info
        """
        return drive_add_label(file_id, label_id)

    def drive_remove_label(self, file_id: str, label_id: str) -> dict:
        """Remove a label from a Google Drive file.

        Args:
            file_id: The file ID
            label_id: The label ID to remove

        Returns:
            Dict with removal info
        """
        return drive_remove_label(file_id, label_id)

    def drive_label_folder(
        self,
        folder_id: str,
        label_id: str,
        recursive: bool = True,
    ) -> dict:
        """Apply a label to all files in a folder or Shared Drive.

        Args:
            folder_id: The folder ID or Shared Drive ID
            label_id: The label ID to apply (e.g., confidential label)
            recursive: Whether to recurse into subfolders (default: True)

        Returns:
            Dict with labeled (count), failed (count), errors (list), files (list of names)
        """
        return drive_label_folder(folder_id, label_id, recursive=recursive)

    def drive_setup_channel_permissions(
        self,
        file_id: str,
        channel_member_emails: list[str],
        requester_email: str,
    ) -> dict:
        """Set up file permissions for Slack channel members and transfer ownership.

        Args:
            file_id: The Google Drive file ID
            channel_member_emails: List of email addresses for channel members
            requester_email: Email of the person who requested the file (new owner)

        Returns:
            Dict with results: shared_with, new_owner, errors
        """
        return drive_setup_channel_permissions(file_id, channel_member_emails, requester_email)

    def drive_export(
        self, file_id: str, export_format: str = "txt", output_path: str | None = None
    ) -> str:
        """Export a Google Docs/Sheets/Slides file to a specific format.

        Args:
            file_id: The file ID
            export_format: Export format (txt, pdf, docx, html, csv, xlsx, pptx)
            output_path: Optional output path (defaults to temp file)

        Returns:
            The output path
        """
        return drive_export(file_id, export_format=export_format, output_path=output_path)

    # --- Docs ---

    def docs_get(self, document_id: str, include_tabs: bool = True) -> dict:
        """Get a Google Doc.

        Args:
            document_id: The document ID
            include_tabs: Whether to include all tabs content

        Returns:
            Dict with document metadata and content
        """
        return docs_get(document_id, include_tabs=include_tabs)

    def docs_get_text(self, document_id: str) -> str:
        """Get plain text content from a Google Doc.

        Args:
            document_id: The document ID

        Returns:
            Plain text content of the document
        """
        return docs_get_text(document_id)

    def docs_append(self, document_id: str, text: str, tab_id: str | None = None) -> dict:
        """Append text to a Google Doc.

        Args:
            document_id: The document ID
            text: Text to append
            tab_id: Optional tab ID to append to

        Returns:
            Dict with document ID
        """
        return docs_append(document_id, text, tab_id=tab_id)

    def docs_insert_page_break(self, document_id: str, index: int = 1) -> dict:
        """Insert a page break in a Google Doc.

        Args:
            document_id: The document ID
            index: Position to insert (1 = beginning)

        Returns:
            Dict with document ID
        """
        return docs_insert_page_break(document_id, index=index)

    def docs_batch_update(self, document_id: str, requests: list) -> dict:
        """Execute batch update on a Google Doc.

        Args:
            document_id: The document ID
            requests: List of request objects

        Returns:
            Dict with document ID and replies
        """
        return docs_batch_update(document_id, requests)

    def docs_replace(self, document_id: str, old_text: str, new_text: str) -> dict:
        """Find and replace text in a Google Doc.

        Args:
            document_id: The document ID
            old_text: Text to find
            new_text: Text to replace with

        Returns:
            Dict with document ID and occurrences replaced
        """
        return docs_replace(document_id, old_text, new_text)

    def docs_insert(self, document_id: str, text: str, index: int) -> dict:
        """Insert text at a specific position in a Google Doc.

        Args:
            document_id: The document ID
            text: Text to insert
            index: Position to insert at (1 = beginning)

        Returns:
            Dict with document ID
        """
        return docs_insert(document_id, text, index)

    def docs_create(self, title: str, content: str | None = None) -> dict:
        """Create a new Google Doc.

        Args:
            title: Document title
            content: Optional initial content to add

        Returns:
            Dict with document_id, title, and url
        """
        return docs_create(title, content=content)

    # --- Sheets ---

    def sheets_create(self, title: str, content: list[list[str]] | None = None) -> dict:
        """Create a new Google Sheet.

        Args:
            title: Spreadsheet title
            content: Optional 2D array of initial data (rows x cols)

        Returns:
            Dict with spreadsheet_id, title, and url
        """
        return sheets_create(title, content=content)

    def sheets_read(self, spreadsheet_id: str, range_notation: str = "A1:Z1000") -> dict:
        """Read data from a Google Sheet.

        Args:
            spreadsheet_id: The spreadsheet ID (from URL)
            range_notation: A1 notation range (e.g., "Sheet1!A1:D10" or "A1:Z1000")

        Returns:
            Dict with spreadsheet_id, range, headers, and rows (list of dicts)
        """
        return sheets_read(spreadsheet_id, range_notation=range_notation)

    def sheets_update(
        self,
        spreadsheet_id: str,
        range_notation: str,
        values: list[list[str]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        """Update data in a Google Sheet.

        Args:
            spreadsheet_id: The spreadsheet ID (from URL)
            range_notation: A1 notation range (e.g., "Sheet1!A1:D10" or "B5")
            values: 2D array of values to write
            value_input_option: How to interpret input ("RAW" or "USER_ENTERED")

        Returns:
            Dict with updated_range, updated_rows, updated_columns, updated_cells
        """
        return sheets_update(
            spreadsheet_id,
            range_notation,
            values,
            value_input_option=value_input_option,
        )

    def sheets_batch_update(
        self,
        spreadsheet_id: str,
        updates: list[dict],
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        """Batch update multiple ranges in a Google Sheet.

        Args:
            spreadsheet_id: The spreadsheet ID (from URL)
            updates: List of dicts with "range" and "values" keys
            value_input_option: How to interpret input ("RAW" or "USER_ENTERED")

        Returns:
            Dict with total_updated_cells and responses
        """
        return sheets_batch_update(
            spreadsheet_id, updates, value_input_option=value_input_option
        )

    # --- Slides ---

    def slides_create(self, title: str) -> dict:
        """Create a new Google Slides presentation.

        Args:
            title: Presentation title

        Returns:
            Dict with presentation_id, title, and url
        """
        return slides_create(title)

    # --- Analytics ---

    def analytics_run_report(
        self,
        dimensions: list[str],
        metrics: list[str],
        start_date: str = "30daysAgo",
        end_date: str = "today",
        limit: int = 100,
        order_by: list | None = None,
    ) -> dict:
        """Run a report on the GA4 property.

        Args:
            dimensions: List of dimension names (e.g., ['country', 'deviceCategory'])
            metrics: List of metric names (e.g., ['activeUsers', 'sessions'])
            start_date: Start date (YYYY-MM-DD, 'today', 'yesterday', 'NdaysAgo')
            end_date: End date
            limit: Max rows to return
            order_by: Optional ordering

        Returns:
            Dict with headers and rows
        """
        return analytics_run_report(
            dimensions,
            metrics,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            order_by=order_by,
        )

    def analytics_run_realtime_report(
        self,
        dimensions: list[str],
        metrics: list[str],
        limit: int = 100,
    ) -> dict:
        """Run a realtime report (last 30 minutes).

        Args:
            dimensions: List of dimension names
            metrics: List of metric names
            limit: Max rows to return

        Returns:
            Dict with headers and rows
        """
        return analytics_run_realtime_report(dimensions, metrics, limit=limit)

    def analytics_get_summary(
        self,
        start_date: str = "30daysAgo",
        end_date: str = "today",
    ) -> dict:
        """Get summary metrics for the GA4 property.

        Returns:
            Dict with key metrics
        """
        return analytics_get_summary(start_date=start_date, end_date=end_date)

    def analytics_get_traffic_by_source(
        self,
        start_date: str = "30daysAgo",
        end_date: str = "today",
        limit: int = 20,
    ) -> dict:
        """Get traffic breakdown by source/medium."""
        return analytics_get_traffic_by_source(
            start_date=start_date, end_date=end_date, limit=limit
        )

    def analytics_get_traffic_by_channel(
        self,
        start_date: str = "30daysAgo",
        end_date: str = "today",
        limit: int = 20,
    ) -> dict:
        """Get traffic breakdown by default channel grouping."""
        return analytics_get_traffic_by_channel(
            start_date=start_date, end_date=end_date, limit=limit
        )

    def analytics_get_top_pages(
        self,
        start_date: str = "30daysAgo",
        end_date: str = "today",
        limit: int = 20,
    ) -> dict:
        """Get top pages by views."""
        return analytics_get_top_pages(
            start_date=start_date, end_date=end_date, limit=limit
        )

    def analytics_get_traffic_by_device(
        self,
        start_date: str = "30daysAgo",
        end_date: str = "today",
    ) -> dict:
        """Get traffic breakdown by device category."""
        return analytics_get_traffic_by_device(start_date=start_date, end_date=end_date)

    def analytics_get_traffic_by_country(
        self,
        start_date: str = "30daysAgo",
        end_date: str = "today",
        limit: int = 20,
    ) -> dict:
        """Get traffic breakdown by country."""
        return analytics_get_traffic_by_country(
            start_date=start_date, end_date=end_date, limit=limit
        )

    def analytics_get_daily_users(
        self,
        start_date: str = "30daysAgo",
        end_date: str = "today",
    ) -> dict:
        """Get daily active users over time."""
        return analytics_get_daily_users(start_date=start_date, end_date=end_date)


def _client() -> GSuiteClient:
    return GSuiteClient()
