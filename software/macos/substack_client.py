"""Substack has NO official public API, ever -- this wraps the unofficial,
reverse-engineered `substack-api` PyPI package, which drives Substack's own
internal/private endpoints (the same ones its web app uses) via a copied
browser session cookie. This is explicitly an unsupported, ToS-risky
integration, not a sanctioned one: it can break silently on any Substack
frontend change with no notice, no SLA, no versioning guarantee, and
automated use of internal endpoints likely violates Substack's Terms of
Service -- including account-level risk (rate-limiting/suspension) if usage
looks bot-like. The user explicitly chose to proceed with this despite the
risk (see the battery/social-publishing planning conversation); every
/integrations UI surface for this must repeat the warning, not just this
docstring -- silently hiding the risk after the user's one-time consent
would be misleading.

Auth: the user's own Substack session cookie, pasted manually at
/integrations (no OAuth exists) and stored in settings.json like any other
credential. No refresh mechanism -- a session cookie expiring means
publish attempts fail with a clear "reconnect your Substack session" error,
surfaced the same way an expired-token failure would be for the other
platforms.
"""
import logging

import settings

log = logging.getLogger("substack_client")


def has_credentials() -> bool:
    return bool(settings.get_all().get("substack_session_cookie") and settings.get_all().get("substack_publication_url"))


def is_connected() -> bool:
    return has_credentials()


def disconnect():
    settings.update(substack_session_cookie=None, substack_publication_url=None)


def post(title: str, body: str, publish_at=None) -> str:
    """Publishes (or schedules, if the library's version supports it) a
    Substack post. Raises RuntimeError with a clear message on any
    failure -- including an ImportError if `substack-api` isn't installed,
    since it's an optional dependency (see requirements.txt's commented-out
    line), not a hard one, given the ToS/breakage risk it carries.
    """
    creds = settings.get_all()
    cookie = creds.get("substack_session_cookie")
    pub_url = creds.get("substack_publication_url")
    if not cookie or not pub_url:
        raise RuntimeError("Substack isn't configured — enter your session cookie and publication URL in /integrations.")

    try:
        from substack_api import Publication  # unofficial, optional dependency -- see module docstring
    except ImportError:
        raise RuntimeError(
            "The unofficial `substack-api` package isn't installed. Install it with "
            "`pip install substack-api` to enable Substack publishing (unsupported/ToS-risk, see substack_client.py)."
        )

    try:
        publication = Publication(pub_url, cookies={"substack.sid": cookie})
        result = publication.post(title=title, body_html=body, publish_at=publish_at)
    except Exception as e:
        # Deliberately not narrowed to a specific exception type -- this
        # library wraps undocumented internal endpoints, so any failure
        # mode (auth expired, endpoint shape changed, rate-limited) is
        # equally "the unofficial integration broke," not a case we can
        # meaningfully distinguish and handle differently today.
        raise RuntimeError(f"Substack publish failed (unofficial API): {e}")

    url = getattr(result, "url", None) or (result.get("url") if isinstance(result, dict) else None)
    if not url:
        raise RuntimeError("Substack publish returned no post URL — check your Substack dashboard to confirm it went through.")
    return url
