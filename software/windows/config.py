import os
import sys
from dotenv import load_dotenv

import paths
import settings as _settings

# python-dotenv's default load_dotenv() resolves its search path relative to
# the calling module's __file__, which isn't reliable inside a frozen
# PyInstaller bundle (see clicky.spec's `datas`, which bundles .env
# alongside templates/static specifically so this path exists at runtime).
# An explicit path sidesteps that ambiguity in both dev and packaged modes.
_env_dir = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_env_dir, ".env"))

# Bump on every release (matching the git tag pushed to GITHUB_REPO, e.g.
# "0.1.0" for tag "v0.1.0") -- see update_check.py, which compares this
# against GitHub Releases' latest tag to show the Settings "update
# available" banner. Kept as a plain module constant (not settings.json)
# since it describes the running binary, not user-editable state.
APP_VERSION = "0.5.0"

# Private for now (see this project's distribution-planning conversation --
# no paid Apple Developer ID yet, demo-scale only) -- update_check.py's
# GitHub API calls will 404 against a private repo without auth, which is
# an accepted gap at this stage rather than shipping a token inside a
# distributed binary (extractable by anyone with the .app). Make the repo
# public, or add a read-only token via a real secrets mechanism, before
# relying on this for real distribution.
GITHUB_REPO = "ted91/Clicky"

# Bundled alongside this app (see clicky_windows.spec's `datas`) so a newer
# app release always carries the firmware it should push to a paired
# device once WiFi is reachable -- see update_check.py's firmware-push half.
FIRMWARE_DIR = os.path.join(_env_dir, "firmware")

# Deployment-level defaults, from .env — these are the fallback values used
# until first-run /setup is completed (or forever, for fields /setup never
# touches, like poll interval). User-editable fields below get overlaid by
# whatever's saved in settings.json via reload_settings(), called once at
# import and again after every /setup or /pair save so a running process
# picks up changes without needing a restart.

STT_PROVIDER = os.getenv("STT_PROVIDER", "mistral").lower()
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mistral").lower()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_STT_MODEL = os.getenv("MISTRAL_STT_MODEL", "voxtral-mini-latest")
MISTRAL_LLM_MODEL = os.getenv("MISTRAL_LLM_MODEL", "mistral-small-latest")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "whisper-1")
OPENAI_LLM_MODEL = os.getenv("OPENAI_LLM_MODEL", "gpt-4.1-nano")

# STT-only -- Deepgram has no chat/completion API, so this can only ever be
# STT_PROVIDER, never LLM_PROVIDER (see providers/deepgram_provider.py's
# summarize(), which raises rather than pretending to support it).
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DEEPGRAM_STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_LLM_MODEL = os.getenv("ANTHROPIC_LLM_MODEL", "claude-haiku-4-5")

# A single OAuth client shared by every install of the app -- baked in at
# build time (developer-owned, one-time Google Cloud Console setup), not
# something each end user creates. This belongs to the app, not the user,
# so it isn't in settings.json. Google's own guidance treats an installed-
# app "Desktop" client secret as non-confidential (it can't be kept secret
# in a distributed binary anyway), so embedding it is the standard pattern
# (gcloud CLI, rclone, etc. all do this) -- users just click Connect and
# see Google's consent screen, no console work. See google_client.py.
#
# NOTE: .env is NOT bundled into the packaged .app by clicky.spec (only
# templates/static ship) -- see clicky.spec's comment for how this actually
# gets into the built app.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "base")

# How to reach the device: "wifi" (HTTP, needs the device to have joined
# your network — see device_client.py) or "ble" (GATT, works even if the
# device can't join your WiFi, e.g. a dual-band router it can't negotiate
# 2.4GHz-only with — see ble_device_client.py). Independent of STT/LLM
# provider choice above.
SYNC_TRANSPORT = os.getenv("SYNC_TRANSPORT", "wifi").lower()

DEVICE_BASE_URL = os.getenv("DEVICE_BASE_URL", "http://192.168.1.100").rstrip("/")
BLE_SCAN_TIMEOUT_SECONDS = int(os.getenv("BLE_SCAN_TIMEOUT_SECONDS", "10"))
PAIRED_BLE_ADDRESS = os.getenv("PAIRED_BLE_ADDRESS") or None

# How often to check the device for new recordings. BLE keeps one
# persistent connection alive (see ble_device_client.py) rather than
# reconnecting every cycle, so a short interval here is cheap -- it's just
# a characteristic read on an already-open connection, not a fresh scan+
# connect. WiFi has no persistent-connection concept (plain stateless HTTP)
# but a few-second interval is harmless there too.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "3"))

# How often to retry a recording whose transcription/summarization already
# failed once (bad API key, rate limit, network blip). Deliberately much
# longer than POLL_INTERVAL_SECONDS -- freshly-synced ("pending") recordings
# are always processed immediately regardless of this value; this only
# throttles repeat attempts on things that already failed, so a bad API key
# doesn't get hammered every few seconds. See poller.py's process_once().
PROCESS_RETRY_INTERVAL_SECONDS = int(os.getenv("PROCESS_RETRY_INTERVAL_SECONDS", "30"))

STORAGE_PATH = os.getenv("STORAGE_PATH", paths.STORAGE_PATH)


def reload_settings():
    """Overlays settings.json onto the module-level values above. Other
    modules do `import config; config.SOMETHING` (never `from config import
    SOMETHING`), so mutating these attributes here is immediately visible
    everywhere else without needing a process restart.
    """
    global STT_PROVIDER, LLM_PROVIDER
    global MISTRAL_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, DEEPGRAM_API_KEY
    global SYNC_TRANSPORT, PAIRED_BLE_ADDRESS

    saved = _settings.get_all()
    STT_PROVIDER = saved.get("stt_provider", STT_PROVIDER)
    LLM_PROVIDER = saved.get("llm_provider", LLM_PROVIDER)
    MISTRAL_API_KEY = saved.get("mistral_api_key", MISTRAL_API_KEY)
    OPENAI_API_KEY = saved.get("openai_api_key", OPENAI_API_KEY)
    ANTHROPIC_API_KEY = saved.get("anthropic_api_key", ANTHROPIC_API_KEY)
    DEEPGRAM_API_KEY = saved.get("deepgram_api_key", DEEPGRAM_API_KEY)
    SYNC_TRANSPORT = saved.get("sync_transport", SYNC_TRANSPORT)
    PAIRED_BLE_ADDRESS = saved.get("paired_ble_address", PAIRED_BLE_ADDRESS)


reload_settings()
