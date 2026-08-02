"""One-shot creation of the linked Notion database set notion_sync.py
expects (Notes, Tasks, People, Calendar + their relations + the Speaker
1..6 slot properties on Notes) -- codifies the API calls that were
originally run by hand, one at a time, to set this up for the first
workspace. Meant to be driven from /integrations' "Auto-setup" form so a
new user doesn't have to replicate that manual process.

Deliberately self-contained (doesn't import notion_sync or read from
settings) -- this runs *before* a token/database IDs exist in settings,
using whatever the setup form was just submitted with. Caller (app.py) is
responsible for saving the returned IDs into settings on success.

Does NOT create a Notion teamspace or page -- the public Notion API has no
endpoint for that (confirmed against current docs; it's sidebar-only, a
manual one-time click). This only creates databases *under* a page the
user already made and shared with their integration.

Not idempotent -- running it twice against the same parent page creates a
second, duplicate set of databases. It's meant to be run once per
workspace; the caller should warn against re-running it.
"""
import requests

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
SPEAKER_SLOT_COUNT = 6


class NotionSetupError(RuntimeError):
    pass


def _headers(token: str):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _create_database(token: str, parent_page_id: str, title: str, properties: dict) -> dict:
    resp = requests.post(
        f"{API_BASE}/databases", headers=_headers(token),
        json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": {"Name": {"title": {}}},  # only the title survives creation-time properties (API quirk); rest added below
        },
        timeout=15,
    )
    if not resp.ok:
        # Notion's own message here ("Can't create databases parented by a
        # database") is accurate but easy to misread as a generic failure --
        # the actual, actionable cause is almost always that the pasted ID
        # is a *database* (e.g. re-pasting the "Notes" database from a
        # previous run) rather than the blank *page* auto-setup needs to
        # create databases inside. Notion only allows page-parented
        # databases, never database-parented ones.
        if "parented by a database" in resp.text:
            raise NotionSetupError(
                "The ID you pasted for \"Page ID\" points to a Notion database, not a page. "
                "Auto-setup needs the blank page you created (e.g. \"Clicky\"), not a database inside it -- "
                "go back to that page in Notion, copy its URL/ID from there, and try again."
            )
        raise NotionSetupError(f"Failed to create '{title}' database: {resp.status_code} {resp.text[:300]}")
    db = resp.json()

    if properties:
        ds_id = db["data_sources"][0]["id"]
        patch = requests.patch(
            f"{API_BASE}/data_sources/{ds_id}", headers=_headers(token),
            json={"properties": properties}, timeout=15,
        )
        if not patch.ok:
            raise NotionSetupError(f"Failed to add properties to '{title}': {patch.status_code} {patch.text[:300]}")
    return db


def _data_source_id(token: str, database_id: str) -> str:
    resp = requests.get(f"{API_BASE}/databases/{database_id}", headers=_headers(token), timeout=15)
    if not resp.ok:
        raise NotionSetupError(f"Failed to look up database {database_id}: {resp.status_code} {resp.text[:300]}")
    return resp.json()["data_sources"][0]["id"]


def _add_relation(token: str, child_database_id: str, target_data_source_id: str, property_name: str):
    """Adds a two-way (dual_property) relation property on the child
    database, pointing at whatever data source is given -- Notion
    automatically creates the mirrored property back on the target side,
    no separate write needed there. Generalized from what used to be
    Notes-only (_add_relation_to_notes) so the same call also wires up
    Tasks/Calendar -> People (see notion_sync.py's per-person Task/
    Calendar entry tracking)."""
    ds_id = _data_source_id(token, child_database_id)
    resp = requests.patch(
        f"{API_BASE}/data_sources/{ds_id}", headers=_headers(token),
        json={"properties": {property_name: {
            "relation": {"data_source_id": target_data_source_id, "type": "dual_property", "dual_property": {}}
        }}},
        timeout=15,
    )
    if not resp.ok:
        raise NotionSetupError(f"Failed to add '{property_name}' relation on database {child_database_id}: {resp.status_code} {resp.text[:300]}")


def _add_relation_to_notes(token: str, child_database_id: str, notes_data_source_id: str):
    """Adds a "Related Note" relation property on the child database,
    pointing at Notes. See _add_relation."""
    _add_relation(token, child_database_id, notes_data_source_id, "Related Note")


def create_workspace(token: str, parent_page_id: str) -> dict:
    """Creates Notes/Tasks/People/Calendar under parent_page_id, wires up
    their relations, and adds the Speaker 1..N properties to Notes.
    Returns {"notion_database_id", "notion_tasks_database_id",
    "notion_people_database_id", "notion_events_database_id"} for the
    caller to persist. Raises NotionSetupError with a specific message on
    any failure -- whatever got created before the failure is left in
    place (no rollback), since partial setup is easier to diagnose/finish
    by hand than to silently undo."""
    notes_db = _create_database(token, parent_page_id, "Notes", {})
    notes_ds_id = notes_db["data_sources"][0]["id"]

    speaker_props = {f"Speaker {i}": {"rich_text": {}} for i in range(1, SPEAKER_SLOT_COUNT + 1)}
    speaker_props["Date"] = {"date": {}}
    # Checkbox that triggers poller.check_social_post_generation_triggers_once()
    # to generate social posts for this specific recording on demand -- see
    # notion_sync.ensure_generate_social_trigger_property (same property
    # name/shape, added here too so new workspaces get it from creation
    # instead of waiting for the runtime migration to add it lazily).
    speaker_props["Generate Social Media"] = {"checkbox": {}}
    # Deepgram Audio Intelligence, added here too so new workspaces get it
    # from creation instead of waiting for notion_sync.ensure_insight_properties'
    # runtime migration to add it lazily.
    speaker_props["Topics"] = {"multi_select": {}}
    speaker_props["Intents"] = {"multi_select": {}}
    speaker_props["Deepgram Summary"] = {"rich_text": {}}
    patch = requests.patch(
        f"{API_BASE}/data_sources/{notes_ds_id}", headers=_headers(token),
        json={"properties": speaker_props}, timeout=15,
    )
    if not patch.ok:
        raise NotionSetupError(f"Failed to add Date/Speaker properties to Notes: {patch.status_code} {patch.text[:300]}")

    tasks_db = _create_database(token, parent_page_id, "Tasks", {
        "Status": {"status": {}},
        "Due Date": {"date": {}},
        "Assignee": {"people": {}},
    })
    people_db = _create_database(token, parent_page_id, "People", {
        "Note": {"rich_text": {}},
        # Populated from calendar attendee matching (see notion_sync.push_people)
        # so all recordings involving a given person are findable by email --
        # a native Notion `email` property, not `people`, since most mentioned
        # names aren't Notion workspace accounts.
        "Email": {"email": {}},
        # Manually entered from the dashboard when a speaker/stakeholder has
        # no calendar-derived email (see notion_sync.update_person_contact)
        # -- a second, independent identity signal alongside Email, used to
        # link the same real person across recordings even when their name
        # is spelled differently each time (see
        # notion_sync._find_person_by_linkedin).
        "LinkedIn": {"url": {}},
    })
    calendar_db = _create_database(token, parent_page_id, "Calendar", {
        "Date": {"date": {}},
    })

    for db in (tasks_db, people_db, calendar_db):
        _add_relation_to_notes(token, db["id"], notes_ds_id)

    # Related Person: lets a Task or Calendar entry point at the specific
    # person it's about (task owner, email draft recipient, meeting
    # attendee), and -- since this is dual_property -- lets a People page
    # show every Task/Calendar entry involving them in return, which is
    # the actual point (see notion_sync.py's _resolve_person_for_relation).
    people_ds_id = people_db["data_sources"][0]["id"]
    _add_relation(token, tasks_db["id"], people_ds_id, "Related Person")
    _add_relation(token, calendar_db["id"], people_ds_id, "Related Person")

    return {
        "notion_database_id": notes_db["id"],
        "notion_tasks_database_id": tasks_db["id"],
        "notion_people_database_id": people_db["id"],
        "notion_events_database_id": calendar_db["id"],
    }


PLATFORM_LABELS = {"substack": "Substack", "medium": "Medium", "linkedin": "LinkedIn", "x": "X"}


def create_publications_database(token: str, parent_page_id: str, notes_database_id: str) -> dict:
    """One-off setup for the social-publishing feature, separate from
    create_workspace() since it's added after the fact to existing
    workspaces (see app.py's /integrations/setup-publications) rather than
    always created fresh. One Publications page ends up representing one
    whole recording (sectioned per platform in its body), not one page per
    platform -- so each platform gets its own prefixed property set
    (f"Approve {label}" etc., matching notion_sync.ensure_publication_properties)
    for independent approval/scheduling/status tracking, and there's no
    self-relation between sibling posts (there are no siblings anymore --
    just sections on one page). "Source Recording" still points back to the
    originating Notes page."""
    properties = {}
    for label in PLATFORM_LABELS.values():
        properties[f"Approve {label}"] = {"checkbox": {}}
        properties[f"{label} Scheduled At"] = {"date": {}}
        properties[f"{label} Status"] = {"select": {"options": [
            {"name": "Draft"}, {"name": "Scheduled"}, {"name": "Published"}, {"name": "Failed"},
        ]}}
        properties[f"{label} Post URL"] = {"url": {}}
    pubs_db = _create_database(token, parent_page_id, "Publications", properties)

    notes_ds_id = _data_source_id(token, notes_database_id)
    _add_relation(token, pubs_db["id"], notes_ds_id, "Source Recording")

    return {"notion_publications_database_id": pubs_db["id"]}


def create_jarvis_database(token: str, parent_page_id: str) -> dict:
    """One-off setup for Jarvis voice commands, same after-the-fact pattern
    as create_publications_database -- Jarvis commands previously had no
    Notion presence at all (see notion_sync.push_command), just an inline
    card on the main dashboard alongside regular recordings. This gives them
    their own database, structured the same way Notes/Publications are:
    one page per command, named by its action type + date so the database
    reads like a log, not a pile of identical "New page" titles."""
    jarvis_db = _create_database(token, parent_page_id, "Jarvis", {
        "Date": {"date": {}},
        "Action Type": {"select": {"options": [
            {"name": "open_app"}, {"name": "calendar_event"}, {"name": "reminder"},
            {"name": "email_draft"}, {"name": "social_post"}, {"name": "qa"},
            {"name": "code_task"}, {"name": "save_snippet"}, {"name": "unknown"},
        ]}},
        "Heard": {"rich_text": {}},
        "Replied": {"rich_text": {}},
        "OK": {"checkbox": {}},
        "Done": {"checkbox": {}},
    })
    return {"notion_jarvis_database_id": jarvis_db["id"]}
