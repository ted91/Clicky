"""X (Twitter) API v2 OAuth2 with PKCE + posting. Same shape as
google_client.py/linkedin_client.py, with PKCE added since X requires it
for user-context OAuth2 (public client, no reliably-kept-secret app
credential). App registration is self-serve/instant (no partner review),
but as of Feb 2026 X charges per post (~$0.015, ~$0.20 with a link) --
there is no free posting tier. client_id/secret (X still issues a
confidential-client secret alongside PKCE for a "Web App" type) live in
settings.json, entered at /integrations, same reasoning as LinkedIn's.

No native scheduling -- poller.py's check_social_publish_once() fires
post() at the scheduled time.
"""
import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode

import requests

import settings

log = logging.getLogger("x_client")

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
TWEETS_URL = "https://api.twitter.com/2/tweets"

SCOPES = "tweet.read tweet.write users.read offline.access"

# PKCE code_verifier is generated per authorize_url() call and must survive
# until exchange_code() -- there's no session store to thread it through
# app.py's redirect round-trip, so it's stashed in settings.json keyed by
# the same `state` value passed through the OAuth redirect, and consumed
# (deleted) on exchange. Short-lived by nature of the OAuth flow itself.


def has_client_credentials() -> bool:
    creds = settings.get_all()
    return bool(creds.get("x_client_id"))


def is_connected() -> bool:
    token = settings.get_all().get("x_token")
    return bool(token and token.get("refresh_token"))


def redirect_uri(request) -> str:
    return str(request.url_for("x_callback"))


def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(request, state: str) -> str:
    verifier, challenge = _pkce_pair()
    pending = settings.get_all().get("x_pending_pkce") or {}
    pending[state] = verifier
    settings.update(x_pending_pkce=pending)

    creds = settings.get_all()
    params = {
        "response_type": "code",
        "client_id": creds.get("x_client_id", ""),
        "redirect_uri": redirect_uri(request),
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(request, code: str, state: str):
    pending = settings.get_all().get("x_pending_pkce") or {}
    verifier = pending.pop(state, None)
    settings.update(x_pending_pkce=pending)
    if not verifier:
        raise RuntimeError("X OAuth state mismatch or expired — try connecting again from /integrations.")

    creds = settings.get_all()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(request),
        "client_id": creds.get("x_client_id", ""),
        "code_verifier": verifier,
    }, auth=(creds.get("x_client_id", ""), creds.get("x_client_secret", "")), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"X token exchange failed {resp.status_code}: {resp.text[:300]}")
    _save_token(resp.json())


def _save_token(data: dict):
    existing = settings.get_all().get("x_token") or {}
    token = {
        "access_token": data.get("access_token", existing.get("access_token")),
        "refresh_token": data.get("refresh_token", existing.get("refresh_token")),
        "expires_at": time.time() + data.get("expires_in", 7200) - 60,
    }
    settings.update(x_token=token)


def _access_token() -> str:
    token = settings.get_all().get("x_token")
    if not token or not token.get("refresh_token"):
        raise RuntimeError("X isn't connected — visit /integrations and click Connect.")
    if token.get("access_token") and time.time() < token.get("expires_at", 0):
        return token["access_token"]

    creds = settings.get_all()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
        "client_id": creds.get("x_client_id", ""),
    }, auth=(creds.get("x_client_id", ""), creds.get("x_client_secret", "")), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"X token refresh failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _save_token(data)
    return data["access_token"]


def disconnect():
    settings.update(x_token=None)


def post(text: str, link: str = None) -> str:
    """Publishes a tweet immediately. `link` is simply appended to the
    text (X's v2 API has no separate link-attachment field for a plain
    tweet -- a bare URL in the text body auto-unfurls into a card).
    Returns the tweet's public URL. Costs ~$0.015 per post (~$0.20 if it
    contains a link) as of X's Feb 2026 pricing -- no free tier."""
    body = text if not link else f"{text}\n\n{link}"
    resp = requests.post(
        TWEETS_URL,
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"},
        json={"text": body}, timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"X post failed {resp.status_code}: {resp.text[:300]}")
    tweet_id = resp.json().get("data", {}).get("id")
    if not tweet_id:
        return "https://x.com/home"
    return f"https://x.com/i/web/status/{tweet_id}"
