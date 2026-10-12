"""Microsoft Graph OAuth2 (auth code flow) + Teams meeting/transcript REST
calls. Hand-rolled with `requests`, same reasoning as google_client.py and
notion_sync.py's raw-REST approach -- avoids msal/msgraph-sdk's dependency
weight in the PyInstaller bundle for a handful of endpoint calls.

Unlike Google, this can't be a single app-wide shared OAuth client: Azure AD
app registrations are tenant-scoped, so each org deploying Clicky needs its
own registration (see config.py's comment above TEAMS_CLIENT_ID for the
exact steps/permissions). Token storage is per-user in settings.json, same
as google_token.

Meeting transcript access via Graph requires the signed-in user to be the
meeting organizer, or an attendee the organizer's tenant policy allows --
a meeting "shared" with the user (they were invited, didn't organize it)
is visible via list_meetings() either way (it's just a calendar event),
but get_meeting_transcript() can come back empty/403 for it depending on
the organizer's org settings. That's a tenant policy limit, not a bug here.
"""
import logging
import re
import time
from urllib.parse import urlencode, quote

import requests

import config
import settings

log = logging.getLogger("teams_client")

GRAPH_API = "https://graph.microsoft.com/v1.0"

SCOPES = " ".join([
    "offline_access",
    "User.Read",
    "Calendars.Read",
    "OnlineMeetings.Read",
    "OnlineMeetingTranscript.Read.All",
])


def _tenant() -> str:
    return config.TEAMS_TENANT_ID or "common"


def _auth_url() -> str:
    return f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/authorize"


def _token_url() -> str:
    return f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/token"


def redirect_uri(request) -> str:
    """The running server is its own OAuth redirect target -- same pattern
    as google_client.redirect_uri. Must exactly match the redirect URI
    registered on the Azure AD app, scheme/host/port included."""
    return str(request.url_for("teams_callback"))


def is_connected() -> bool:
    token = settings.get_all().get("teams_token")
    return bool(token and token.get("refresh_token"))


def has_client_credentials() -> bool:
    """False only if the developer/IT admin never baked in
    TEAMS_CLIENT_ID/SECRET at build time -- never a per-user setup gap."""
    return bool(config.TEAMS_CLIENT_ID and config.TEAMS_CLIENT_SECRET)


def authorize_url(request, state: str) -> str:
    params = {
        "client_id": config.TEAMS_CLIENT_ID,
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
    }
    return f"{_auth_url()}?{urlencode(params)}"


def exchange_code(request, code: str):
    """Exchanges the authorization code for tokens and saves them. Raises
    RuntimeError with Microsoft's error detail on failure."""
    resp = requests.post(_token_url(), data={
        "client_id": config.TEAMS_CLIENT_ID,
        "client_secret": config.TEAMS_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(request),
        "scope": SCOPES,
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Microsoft token exchange failed {resp.status_code}: {resp.text[:300]}")
    _save_token(resp.json())


def _save_token(data: dict):
    existing = settings.get_all().get("teams_token") or {}
    token = {
        "access_token": data.get("access_token", existing.get("access_token")),
        # Microsoft returns a (possibly rotated) refresh_token on every
        # successful grant, but fall back to the existing one just in case.
        "refresh_token": data.get("refresh_token", existing.get("refresh_token")),
        "expires_at": time.time() + data.get("expires_in", 3600) - 60,  # 60s safety margin
    }
    settings.update(teams_token=token)


def _access_token() -> str:
    """Returns a valid access token, refreshing via the stored refresh_token
    if expired. Raises RuntimeError if not connected."""
    token = settings.get_all().get("teams_token")
    if not token or not token.get("refresh_token"):
        raise RuntimeError("Microsoft Teams isn't connected — visit /integrations and click Connect.")
    if token.get("access_token") and time.time() < token.get("expires_at", 0):
        return token["access_token"]

    resp = requests.post(_token_url(), data={
        "client_id": config.TEAMS_CLIENT_ID,
        "client_secret": config.TEAMS_CLIENT_SECRET,
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
        "scope": SCOPES,
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Microsoft token refresh failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _save_token(data)
    return data["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}"}


def disconnect():
    settings.delete("teams_token")


# --- Meeting listing ---------------------------------------------------

def list_meetings(days_back: int = 30, days_forward: int = 7) -> list:
    """Calendar events (organized by, or shared/invited to, the signed-in
    user -- /me/calendarView returns both) that are online meetings, across
    [-days_back, +days_forward] days from now. Returns
    [{"id", "title", "start", "end", "organizer", "is_organizer",
      "attendees":[{"name","email"}], "join_url"}], newest first.
    This is just calendar metadata -- whether a transcript actually exists
    for one of these is checked separately by get_meeting_transcript(),
    since Teams only transcribes a meeting if someone started transcription
    during it."""
    time_min = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_back * 86400))
    time_max = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days_forward * 86400))
    resp = requests.get(f"{GRAPH_API}/me/calendarView", headers=_headers(), params={
        "startDateTime": time_min,
        "endDateTime": time_max,
        "$orderby": "start/dateTime desc",
        "$top": 100,
        "$select": "id,subject,start,end,isOnlineMeeting,onlineMeeting,organizer,attendees",
    }, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"Teams calendar lookup failed {resp.status_code}: {resp.text[:300]}")

    my_email = _me_email()
    out = []
    for event in resp.json().get("value", []):
        if not event.get("isOnlineMeeting"):
            continue
        join_url = (event.get("onlineMeeting") or {}).get("joinUrl", "")
        if not join_url:
            continue
        organizer_email = (event.get("organizer") or {}).get("emailAddress", {}).get("address", "")
        attendees = [
            {"name": a.get("emailAddress", {}).get("name", ""), "email": a.get("emailAddress", {}).get("address", "")}
            for a in event.get("attendees", []) if a.get("emailAddress", {}).get("address")
        ]
        out.append({
            "id": event["id"],
            "title": event.get("subject", "Untitled meeting"),
            "start": event.get("start", {}).get("dateTime", ""),
            "end": event.get("end", {}).get("dateTime", ""),
            "organizer": organizer_email,
            "is_organizer": bool(my_email and organizer_email.lower() == my_email.lower()),
            "attendees": attendees,
            "join_url": join_url,
        })
    return out


def _me_email() -> str:
    try:
        resp = requests.get(f"{GRAPH_API}/me", headers=_headers(), params={"$select": "mail,userPrincipalName"}, timeout=10)
        if resp.ok:
            data = resp.json()
            return data.get("mail") or data.get("userPrincipalName") or ""
    except (requests.RequestException, RuntimeError):
        pass
    return ""


# --- Transcript retrieval -----------------------------------------------

def _resolve_online_meeting_id(join_url: str) -> str:
    """A calendar event only carries the meeting's join URL -- the
    onlineMeeting resource (and its /transcripts) is looked up by that URL
    via this filter, same two-step Graph calls on first access would need
    either way."""
    resp = requests.get(f"{GRAPH_API}/me/onlineMeetings", headers=_headers(), params={
        "$filter": f"JoinWebUrl eq '{join_url}'",
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Teams onlineMeeting lookup failed {resp.status_code}: {resp.text[:300]}")
    items = resp.json().get("value", [])
    if not items:
        raise RuntimeError("No online meeting found for this event (it may not have been a Teams call, or you weren't the organizer).")
    return items[0]["id"]


def list_transcripts(online_meeting_id: str) -> list:
    """[{"id", "created"}], newest first. Empty if no one ever started
    transcription during the meeting."""
    resp = requests.get(f"{GRAPH_API}/me/onlineMeetings/{online_meeting_id}/transcripts",
                         headers=_headers(), timeout=15)
    if resp.status_code == 404:
        return []
    if not resp.ok:
        raise RuntimeError(f"Teams transcript list failed {resp.status_code}: {resp.text[:300]}")
    items = resp.json().get("value", [])
    return [{"id": t["id"], "created": t.get("createdDateTime", "")} for t in items]


_VTT_CUE_RE = re.compile(
    r"<v ([^>]+)>(.*?)</v>", re.DOTALL
)


def _parse_vtt(vtt_text: str) -> str:
    """Teams transcript VTT cues look like:
    00:00:03.000 --> 00:00:07.000
    <v Jane Doe>Hey everyone, thanks for joining.</v>
    Collapses this into "Speaker: text" lines, one per cue, dropping cue
    timestamps/ids -- summarize() only needs readable transcript text, not
    exact timing."""
    lines = []
    for block in vtt_text.split("\n\n"):
        m = _VTT_CUE_RE.search(block)
        if m:
            speaker, text = m.group(1).strip(), m.group(2).strip()
            text = re.sub(r"\s+", " ", text)
            if text:
                lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def get_transcript_text(online_meeting_id: str, transcript_id: str) -> str:
    """Downloads one transcript's content as WebVTT and returns it as plain
    speaker-tagged text (see _parse_vtt)."""
    resp = requests.get(
        f"{GRAPH_API}/me/onlineMeetings/{online_meeting_id}/transcripts/{transcript_id}/content",
        headers=_headers(), params={"$format": "text/vtt"}, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Teams transcript download failed {resp.status_code}: {resp.text[:300]}")
    return _parse_vtt(resp.text)


def get_meeting_transcript(meeting: dict) -> str:
    """High-level convenience: resolves a list_meetings() entry straight to
    its latest transcript's plain text. Returns "" if the meeting was never
    transcribed (not an error -- that's the common case for most meetings).
    Raises RuntimeError for actual API failures (auth, permissions, etc)."""
    online_meeting_id = _resolve_online_meeting_id(meeting["join_url"])
    transcripts = list_transcripts(online_meeting_id)
    if not transcripts:
        return ""
    transcripts.sort(key=lambda t: t["created"], reverse=True)
    return get_transcript_text(online_meeting_id, transcripts[0]["id"])
