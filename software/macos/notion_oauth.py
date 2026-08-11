"""Notion OAuth (public integration) -- removes 3 of the 5 manual steps in
the token-paste setup (create integration, copy token, share page):
create integration and share page collapse into Notion's own hosted
consent screen, which lets the user pick which page(s) to share right
there. Copying the token by hand is replaced by this exchange.

Mirrors google_client.py's pattern exactly -- same shared-client reasoning
(config.NOTION_CLIENT_ID/SECRET baked in at build time, a one-time
developer-side integration registration, not something each user creates),
same "the running FastAPI server is its own redirect target" technique
(no separate localhost listener for the code exchange).

One real difference from Google: Notion's OAuth token doesn't expire and
there's no refresh flow -- the access token returned here is used as-is,
indefinitely, same as a manually-pasted internal integration token. So
there's no _access_token()/refresh dance to mirror from google_client.py;
the token IS the thing saved to settings.json's "notion_token", the exact
same field the manual token-paste path already writes -- meaning
notion_sync.py needs zero changes to work with an OAuth-obtained token.

The manual token-paste path (see settings.html) stays as a fallback --
this is an additional way to arrive at the same notion_token setting, not
a replacement for it.
"""
import logging
from urllib.parse import urlencode

import requests

import config
import settings

log = logging.getLogger("notion_oauth")

AUTH_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"
API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"


def has_client_credentials() -> bool:
    """False only if the developer never registered a public Notion
    integration and baked its client id/secret into .env -- never a
    per-user setup gap. See config.py's comment (same pattern as Google's
    has_client_credentials)."""
    return bool(config.NOTION_CLIENT_ID and config.NOTION_CLIENT_SECRET)


def redirect_uri(request) -> str:
    """Deliberately NOT request.url_for() -- unlike Google, Notion requires
    an EXACT string match against the redirect URI registered on the
    integration (case-sensitive, host-sensitive), no wildcard/localhost-
    equivalence. This app opens its browser tab at 127.0.0.1 (see
    main_packaged.py), but Notion's own integration UI defaults new
    redirect URIs to "localhost" -- request.url_for() would reflect
    whichever host the browser actually used (127.0.0.1), silently
    mismatching a "localhost"-registered URI and failing with Notion's
    "Missing or invalid redirect_uri" error. Hardcoding this to match
    main_packaged.py's PORT sidesteps the whole class of mismatch --
    whatever's registered on the Notion integration MUST be exactly
    "http://localhost:8000/notion/callback" for this to work."""
    return "http://localhost:8000/notion/callback"


def authorize_url(request, state: str) -> str:
    params = {
        "client_id": config.NOTION_CLIENT_ID,
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "owner": "user",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(request, code: str) -> str:
    """Exchanges the authorization code for an access token, saves it as
    notion_token (same settings.json field the manual paste path uses),
    and returns it. Raises RuntimeError with Notion's error detail on
    failure. Notion's OAuth uses HTTP Basic auth for this exchange
    (client_id:client_secret), unlike Google's form-body credentials."""
    resp = requests.post(
        TOKEN_URL,
        auth=(config.NOTION_CLIENT_ID, config.NOTION_CLIENT_SECRET),
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(request),
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Notion token exchange failed {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Notion token exchange returned no access_token: {data}")
    settings.update(notion_token=token)
    return token


def list_accessible_pages() -> list:
    """Pages (not databases) the just-connected integration can see --
    populates the page picker that replaces manually pasting a page ID.
    Notion's consent screen is what actually grants per-page access (the
    user picks pages/a workspace there); this just lists what came out of
    that choice. Returns [{"id", "title"}, ...], title falling back to
    "(untitled)" for a page with no title property set."""
    token = settings.get_all().get("notion_token")
    if not token:
        return []
    resp = requests.post(
        f"{API_BASE}/search",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json={"filter": {"value": "page", "property": "object"}},
        timeout=15,
    )
    if not resp.ok:
        log.warning("notion page search failed %s: %s", resp.status_code, resp.text[:200])
        return []
    pages = []
    for result in resp.json().get("results", []):
        props = result.get("properties", {}) or {}
        title = "(untitled)"
        for prop in props.values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                if parts:
                    title = "".join(t.get("plain_text", "") for t in parts) or title
                break
        pages.append({"id": result["id"], "title": title})
    return pages
