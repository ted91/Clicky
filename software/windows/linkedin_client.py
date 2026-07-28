"""LinkedIn OAuth2 (personal-profile posting) + Posts API. Same hand-rolled
`requests` shape as google_client.py, with one difference: unlike Google's
single shared OAuth client baked into the app (config.GOOGLE_CLIENT_ID),
LinkedIn requires each user to register their own app in the LinkedIn
Developer portal (linkedin.com/developers/apps) and grant it the
`w_member_social`/`openid`/`profile` scopes on their own personal profile --
posting-as-yourself is self-serve (no partner-program review needed), but
the client id/secret can't be shared across installs the way Google's is.
So client_id/secret live in settings.json (entered at /integrations), not
config.py constants.

No native scheduling exists on LinkedIn's API -- every post publishes
immediately. Scheduling is handled entirely by poller.py's
check_social_publish_once() calling post() at the due time.
"""
import logging
import time
from urllib.parse import urlencode

import requests

import settings

log = logging.getLogger("linkedin_client")

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
LINKEDIN_VERSION = "202601"  # LinkedIn requires a versioned header on REST endpoints

SCOPES = "openid profile w_member_social"


def has_client_credentials() -> bool:
    creds = settings.get_all()
    return bool(creds.get("linkedin_client_id") and creds.get("linkedin_client_secret"))


def is_connected() -> bool:
    token = settings.get_all().get("linkedin_token")
    return bool(token and token.get("refresh_token"))


def redirect_uri(request) -> str:
    return str(request.url_for("linkedin_callback"))


def authorize_url(request, state: str) -> str:
    creds = settings.get_all()
    params = {
        "response_type": "code",
        "client_id": creds.get("linkedin_client_id", ""),
        "redirect_uri": redirect_uri(request),
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(request, code: str):
    creds = settings.get_all()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(request),
        "client_id": creds.get("linkedin_client_id", ""),
        "client_secret": creds.get("linkedin_client_secret", ""),
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"LinkedIn token exchange failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _save_token(data)
    _fetch_and_save_person_urn()


def _save_token(data: dict):
    existing = settings.get_all().get("linkedin_token") or {}
    token = {
        "access_token": data.get("access_token", existing.get("access_token")),
        # LinkedIn only issues a refresh_token if the app has the
        # r_liteprofile... no -- practically, LinkedIn's default 60-day
        # access tokens have no refresh_token at all for most consumer
        # apps; if absent, re-auth is required once it expires (surfaced
        # via is_connected()'s refresh_token check going false at that
        # point -- not silently broken, the /integrations page will show
        # "Connect" again).
        "refresh_token": data.get("refresh_token", existing.get("refresh_token")),
        "expires_at": time.time() + data.get("expires_in", 5184000) - 60,
    }
    settings.update(linkedin_token=token)


def _access_token() -> str:
    token = settings.get_all().get("linkedin_token")
    if not token or not token.get("access_token"):
        raise RuntimeError("LinkedIn isn't connected — visit /integrations and click Connect.")
    if time.time() < token.get("expires_at", 0):
        return token["access_token"]
    if not token.get("refresh_token"):
        raise RuntimeError("LinkedIn access token expired and no refresh token is available — reconnect at /integrations.")
    creds = settings.get_all()
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
        "client_id": creds.get("linkedin_client_id", ""),
        "client_secret": creds.get("linkedin_client_secret", ""),
    }, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"LinkedIn token refresh failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _save_token(data)
    return data["access_token"]


def _fetch_and_save_person_urn():
    """The Posts API needs the author as a urn:li:person:{id} -- fetched
    once at connect time via the OIDC userinfo endpoint (needs the
    `openid`/`profile` scopes, included above) and cached, since it never
    changes for a given LinkedIn account."""
    resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"LinkedIn userinfo fetch failed {resp.status_code}: {resp.text[:300]}")
    sub = resp.json().get("sub")
    if sub:
        settings.update(linkedin_person_urn=f"urn:li:person:{sub}")


def disconnect():
    settings.update(linkedin_token=None, linkedin_person_urn=None)


def post(text: str, link: str = None) -> str:
    """Publishes a post to the connected LinkedIn profile immediately.
    Returns the created post's public URL. `link` becomes an ARTICLE-type
    share (LinkedIn generates its own link preview) if given, otherwise a
    plain text post."""
    person_urn = settings.get_all().get("linkedin_person_urn")
    if not person_urn:
        raise RuntimeError("LinkedIn person URN not found — reconnect at /integrations.")

    payload = {
        "author": person_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if link:
        payload["content"] = {"article": {"source": link}}

    resp = requests.post(
        POSTS_URL,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "LinkedIn-Version": LINKEDIN_VERSION,
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json=payload, timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"LinkedIn post failed {resp.status_code}: {resp.text[:300]}")

    # The created post's URN comes back in the x-restli-id / x-linkedin-id
    # response header, not the body -- convert to a public-facing URL.
    post_urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id")
    if not post_urn:
        return "https://www.linkedin.com/feed/"  # published, but URL unknown -- not fatal
    return f"https://www.linkedin.com/feed/update/{post_urn}/"
