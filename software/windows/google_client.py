"""Google OAuth2 (installed-app loopback flow) + Calendar/Gmail/Tasks REST
calls. Hand-rolled with `requests` -- same reasoning as notion_sync.py's
raw-REST approach: avoids google-api-python-client's heavy dependency tree
in the PyInstaller bundle for what's a handful of endpoint calls.

Uses a single shared OAuth client baked into the app (config.GOOGLE_CLIENT_ID/
SECRET) rather than making each user create their own in Google Cloud
Console -- see config.py's comment for why embedding it is fine here. A
user just clicks Connect and sees Google's consent screen directly. The
running FastAPI server itself is the loopback redirect target
(GET /google/callback), so no separate localhost listener is needed for
the auth code exchange.

Token storage: a single blob in settings.json (access/refresh/expiry) --
this part IS per-user, unlike the client id/secret above.

FUTURE FEATURE (not yet built) -- pre-meeting prep notes:
Before an upcoming meeting starts, look up past recordings involving the
same attendees (via Notion People's "Related Note" relations, or locally
by matching record["meeting"]["attendees"] emails across storage.json) and/
or a similar meeting title, and have the LLM synthesize a short "context +
still-open action items" note -- delivered as an email draft (reuse the
existing drafts/approve flow in poller.generate_drafts_once /
app.py's approve route) or a dashboard card, some N minutes before the
meeting. Needs a "look-ahead" calendar check (current_or_next_event()
already supports a window, but poller.check_meeting_auto_start_once() only
acts once a meeting has *started* -- this needs a separate earlier trigger,
e.g. 15-30 min out, that doesn't also try to start recording).
Main open question: matching "same topic" reliably across recordings, not
just same attendees, without generating noise for a first-time meeting
with someone (no history yet -- should just skip, not fabricate context).
Tracked here so the idea isn't lost, not started yet.
"""
import base64
import logging
import time
from email.message import EmailMessage
from urllib.parse import urlencode

import requests

import config
import settings

log = logging.getLogger("google_client")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
TASKS_API = "https://tasks.googleapis.com/tasks/v1"

SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # readonly added for the AI-pager notification feed (unread-inbox
    # polling in notifications.py). Existing installs consented before this
    # scope existed -- their tokens keep working for the old scopes, Gmail
    # reads just 403 until the user reconnects at /google/connect
    # (prompt=consent above forces the full new grant).
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/tasks",
])

# Meeting-link patterns recognized in a calendar event's location/description,
# independent of which calendar backend supplied the event -- an org can use
# Google Calendar for scheduling while actually meeting over Teams, so this
# intentionally isn't limited to Meet links. See poller's meeting auto-detect.
import re
MEETING_URL_PATTERNS = [
    re.compile(r"https://meet\.google\.com/[a-z0-9-]+", re.IGNORECASE),
    re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"https://teams\.live\.com/meet/[^\s\"'<>]+", re.IGNORECASE),
]


def redirect_uri(request) -> str:
    """The running server is its own OAuth redirect target -- no separate
    localhost listener needed. Uses the request's own host so this works
    whether the app is reached as 127.0.0.1 or localhost."""
    return str(request.url_for("google_callback"))


def is_connected() -> bool:
    token = settings.get_all().get("google_token")
    return bool(token and token.get("refresh_token"))


def has_client_credentials() -> bool:
    """False only if the developer never baked in GOOGLE_CLIENT_ID/SECRET
    at build time -- never a per-user setup gap."""
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def authorize_url(request, state: str) -> str:
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # forces a refresh_token even on a re-auth
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(request, code: str):
    """Exchanges the authorization code for tokens and saves them. Raises
    RuntimeError with Google's error detail on failure."""
    resp = requests.post(TOKEN_URL, data={
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(request),
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Google token exchange failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _save_token(data)


def _save_token(data: dict):
    existing = settings.get_all().get("google_token") or {}
    token = {
        "access_token": data.get("access_token", existing.get("access_token")),
        # Google only returns refresh_token on the *first* consent (or when
        # prompt=consent forces re-issue) -- preserve the existing one on a
        # bare refresh response, which omits it.
        "refresh_token": data.get("refresh_token", existing.get("refresh_token")),
        "expires_at": time.time() + data.get("expires_in", 3600) - 60,  # 60s safety margin
    }
    settings.update(google_token=token)


def _access_token() -> str:
    """Returns a valid access token, refreshing via the stored refresh_token
    if expired. Raises RuntimeError if not connected."""
    token = settings.get_all().get("google_token")
    if not token or not token.get("refresh_token"):
        raise RuntimeError("Google isn't connected — visit /integrations and click Connect.")
    if token.get("access_token") and time.time() < token.get("expires_at", 0):
        return token["access_token"]

    resp = requests.post(TOKEN_URL, data={
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Google token refresh failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _save_token(data)
    return data["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}"}


def disconnect():
    settings.delete("google_token")


# --- Calendar --------------------------------------------------------------

def _extract_meeting_url(event: dict) -> str:
    """Checks conferenceData first (Google's own structured field), then
    falls back to pattern-matching the location/description text -- this
    is what catches a Teams link on an event scheduled via Google Calendar."""
    conf = event.get("conferenceData", {})
    for ep in conf.get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]

    haystack = " ".join(filter(None, [event.get("location", ""), event.get("description", "")]))
    for pattern in MEETING_URL_PATTERNS:
        m = pattern.search(haystack)
        if m:
            return m.group(0)
    return ""


def current_or_next_event(window_min: int = 15):
    """Returns the calendar event currently in progress, or the next one
    starting within `window_min` minutes, as
    {title, start, end, meeting_url, attendees:[{name, email}]} -- or None
    if nothing qualifies or Google isn't connected. Failures here are
    meant to be non-fatal to the caller (meeting metadata is a nice-to-have,
    never a blocker for recording itself)."""
    if not is_connected():
        return None
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        time_max = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + window_min * 60))
        time_min = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 60))
        resp = requests.get(f"{CALENDAR_API}/calendars/primary/events", headers=_headers(), params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 10,
        }, timeout=15)
        if not resp.ok:
            log.warning("calendar lookup failed %s: %s", resp.status_code, resp.text[:200])
            return None
        items = resp.json().get("items", [])
    except (requests.RequestException, RuntimeError) as e:
        log.warning("calendar lookup failed: %s", e)
        return None

    for event in items:
        start = event.get("start", {}).get("dateTime")
        end = event.get("end", {}).get("dateTime")
        if not start or not end:
            continue  # all-day event, no meeting to join
        attendees = [
            {"name": a.get("displayName") or a.get("email", "").split("@")[0], "email": a.get("email", "")}
            for a in event.get("attendees", []) if a.get("email")
        ]
        return {
            "title": event.get("summary", "Untitled meeting"),
            "start": start,
            "end": end,
            "meeting_url": _extract_meeting_url(event),
            "attendees": attendees,
        }
    return None


# --- Gmail -------------------------------------------------------------

def send_email(to: list, subject: str, body: str):
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    resp = requests.post(f"{GMAIL_API}/users/me/messages/send", headers=_headers(),
                          json={"raw": raw}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Gmail send failed {resp.status_code}: {resp.text[:300]}")


def list_unread_messages(newer_than: str = "1h", max_results: int = 10) -> list:
    """Recent unread inbox messages for the AI-pager feed: returns
    [{"id", "from", "subject"}]. Needs the gmail.readonly scope -- accounts
    connected before that scope existed get a 403 here, surfaced as a
    RuntimeError telling the user to reconnect (notifications.py logs it
    once rather than crashing the poll loop)."""
    resp = requests.get(f"{GMAIL_API}/users/me/messages", headers=_headers(),
                        params={"q": f"is:unread in:inbox newer_than:{newer_than}",
                                "maxResults": max_results}, timeout=15)
    if resp.status_code == 403:
        raise RuntimeError("Gmail read access not granted -- reconnect Google at /google/connect "
                           "to approve the new read permission")
    if not resp.ok:
        raise RuntimeError(f"Gmail list failed {resp.status_code}: {resp.text[:300]}")
    out = []
    for stub in resp.json().get("messages", []) or []:
        detail = requests.get(f"{GMAIL_API}/users/me/messages/{stub['id']}", headers=_headers(),
                              params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
                              timeout=15)
        if not detail.ok:
            continue
        headers = {h["name"].lower(): h["value"]
                   for h in detail.json().get("payload", {}).get("headers", [])}
        # "Jane Doe <jane@x.com>" -> "Jane Doe" (the pager screen is tiny)
        sender = headers.get("from", "")
        if "<" in sender:
            sender = sender.split("<")[0].strip().strip('"') or sender
        out.append({"id": stub["id"], "from": sender, "subject": headers.get("subject", "(no subject)")})
    return out


# --- Tasks ---------------------------------------------------------------

def create_task(title: str, notes: str = "", due: str = None):
    payload = {"title": title, "notes": notes}
    if due:
        payload["due"] = f"{due}T00:00:00.000Z"
    resp = requests.post(f"{TASKS_API}/lists/@default/tasks", headers=_headers(), json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Google Tasks create failed {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# --- Calendar event creation ---------------------------------------------

def create_event(title: str, start: str, end: str, attendees: list = None):
    payload = {
        "summary": title,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "attendees": [{"email": a} for a in (attendees or [])],
    }
    resp = requests.post(f"{CALENDAR_API}/calendars/primary/events", headers=_headers(),
                          json=payload, params={"sendUpdates": "all"}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Google Calendar event create failed {resp.status_code}: {resp.text[:300]}")
    return resp.json()
