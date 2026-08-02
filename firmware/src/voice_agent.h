#ifndef VOICE_AGENT_H
#define VOICE_AGENT_H

#include <Arduino.h>

// Live, on-device Deepgram Voice Agent client for Jarvis BOOT commands only
// -- streams mic audio to Deepgram in real time (instead of recording a
// clip to SD and uploading it later) so plain questions get answered
// immediately, independent of whether the Mac/Windows app is reachable at
// all. Action-type commands (calendar/reminder/email/etc.) still get
// forwarded to the Mac for real execution -- see voice_agent.cpp's top
// comment for the full design and why.
//
// UNVERIFIED FIRST PASS: this has not been exercised against a live
// Deepgram Voice Agent session or real hardware. Treat every detail of the
// wire protocol (message shapes, event names) as needing verification
// against Deepgram's actual behavior before relying on it -- see the
// session's plan doc for the list of open risks.

// Starts a live session for one Jarvis command (BOOT press) -- call this
// instead of recorder_start(true) when WiFi is reachable. Spawns its own
// task and returns immediately. Falls back to recorder_start(true)
// internally (synchronously recording the rest of the command to SD/RAM
// instead) if the Deepgram connection can't be established, or if
// mid-session connectivity is lost.
void voice_agent_start_command();

// Same "finish speaking" signal as recorder_stop() -- call on the second
// BOOT click. No-op if no live session is running.
void voice_agent_stop();

// True while a live session (or its SD/RAM fallback) is in progress --
// main.cpp's state machine polls this the same way it polls
// recorder_is_recording().
bool voice_agent_is_active();

// True if the most recently started session fell back to recorder_start(true)
// internally (no Deepgram key configured, or the connection/handshake
// failed) -- main.cpp's syncWatchTask needs this to know whether to trust
// recorder_last_was_sd()/recorder_was_cancelled() (only meaningful if
// recorder.cpp actually ran this cycle) or skip them entirely (a
// successful live session never touches recorder.cpp at all, so those
// getters would otherwise still reflect whatever the PREVIOUS recording
// was). Valid only after voice_agent_is_active() has gone back to false.
bool voice_agent_used_recorder_fallback();

// One-time config, read from NVS ("jarvis_va" namespace) -- no provisioning
// UI exists yet (BLE pairing only currently sets WiFi credentials, see
// ble_sync.cpp's SETWIFI command); until one is added, these must be set by
// hand (e.g. a temporary serial command, or a short one-off sketch) before
// voice_agent_start_command() can do anything useful. All getters return ""
// if unset -- callers must treat that as "not configured" and skip
// straight to the SD-recording fallback rather than attempting a connection
// with empty credentials.
String voice_agent_deepgram_api_key();
String voice_agent_llm_api_key();      // Groq (or fallback provider) key for agent.think
String voice_agent_mac_base_url();     // e.g. "http://192.168.1.50:8000" -- same-network only, no Tailscale/Funnel yet
String voice_agent_mac_device_key();   // shared secret, must match settings.get_or_create_jarvis_device_api_key() on the Mac
void voice_agent_set_config(const char *deepgramKey, const char *llmKey, const char *macBaseUrl, const char *macDeviceKey);

// Off by default (NVS-persisted) -- a real hang was reproduced on hardware
// during this feature's first bring-up (blocking HTTPClient/TLS calls
// inside the WebSocket library's own callback context, most likely). main.cpp
// only calls voice_agent_start_command() when this is true, so BOOT always
// falls back to the proven recorder_start(true) path until this is properly
// debugged with a real serial monitor attached and re-enabled deliberately
// -- see voice_agent.cpp's top comment for the open investigation.
bool voice_agent_live_enabled();
void voice_agent_set_live_enabled(bool enabled);

#endif
