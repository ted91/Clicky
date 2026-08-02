// Live, on-device Deepgram Voice Agent client for Jarvis BOOT commands.
//
// UNVERIFIED FIRST PASS -- see voice_agent.h's top comment. The wire
// protocol here (message shapes/event names) was confirmed from Deepgram's
// published docs during this session, not from a live connection. Expect
// to need real hardware + a real Deepgram API key to shake out mismatches
// before this actually works end to end.
//
// CONFIRMED LIVE, FIRST HARDWARE TEST: this hung the device -- audio played
// back late and the board stopped responding to button input entirely,
// needing a power cycle to recover. Root cause not yet confirmed (no serial
// monitor was attached during the test), but the prime suspect is the
// blocking HTTPClient calls (forwardActionToMac/forwardSearchToMac/
// fetchMemoryFacts, each with multi-second timeouts, and the TLS handshake
// in beginSSL()) all running synchronously inside code paths the
// WebSocketsClient library itself drives via s_ws.loop() -- if any of
// those stall, nothing yields long enough for anything else on this task
// (or WDT-sensitive neighboring tasks) to make progress. DO NOT re-enable
// voice_agent_live_enabled() by default until this has been root-caused
// with a real serial monitor attached and a fix verified live -- see
// voice_agent_live_enabled()'s doc in voice_agent.h.
//
// Design (see the session's plan doc for the full rationale):
//   - Only Jarvis commands use this -- regular memo/journal recording
//     (recorder.cpp's recordToSd/recordToRam) is completely untouched.
//   - Whenever WiFi is reachable, the ESP32 itself holds the Deepgram
//     Voice Agent WebSocket connection (not the Mac) -- this is what lets
//     plain questions get answered live, independent of whether the
//     Mac/Windows app is even running. Answering a question has no side
//     effects and needs no Mac-only app/credentials.
//   - Deepgram's own function-calling decides in real time whether the
//     command is a question (answered directly via its own TTS turn, see
//     onWsEvent's Audio handling) or an action (calendar_event/reminder/
//     email_draft/social_post/code_task/save_snippet -- see
//     buildAgentFunctionsJson()). An action's decided fields get forwarded
//     to the Mac's /jarvis/execute-decision endpoint for real execution,
//     since only the Mac has Notion/Obsidian/AppleScript automation.
//   - If WiFi isn't reachable, or the Deepgram connection can't be
//     established, or the forward-to-Mac call fails, this falls back to
//     today's behavior: recorder_start(true), recording the command to
//     SD/RAM for poller.py to pick up (and execute, batched, on
//     reconnect) later -- see software/*/poller.py's is_command_batch
//     logic from this same session.
#include "voice_agent.h"
#include "recorder.h"
#include "audio_bsp.h"
#include "wifi_sync.h"
#include "power_mgr.h"
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const uint32_t SAMPLE_RATE = 16000;
static const size_t CHUNK_BYTES = 2048; // same chunking as recorder.cpp, ~64ms of stereo 16-bit audio per read

static Preferences s_vaPrefs;
static bool s_vaPrefsLoaded = false;

static void ensureVaPrefsLoaded() {
    if (s_vaPrefsLoaded) return;
    s_vaPrefsLoaded = true;
    s_vaPrefs.begin("jarvis_va", true); // read-only open for getters; set_config reopens read-write
}

String voice_agent_deepgram_api_key() {
    ensureVaPrefsLoaded();
    return s_vaPrefs.getString("dgKey", "");
}

String voice_agent_llm_api_key() {
    ensureVaPrefsLoaded();
    return s_vaPrefs.getString("llmKey", "");
}

String voice_agent_mac_base_url() {
    ensureVaPrefsLoaded();
    return s_vaPrefs.getString("macUrl", "");
}

String voice_agent_mac_device_key() {
    ensureVaPrefsLoaded();
    return s_vaPrefs.getString("macKey", "");
}

void voice_agent_set_config(const char *deepgramKey, const char *llmKey, const char *macBaseUrl, const char *macDeviceKey) {
    s_vaPrefs.end();
    s_vaPrefs.begin("jarvis_va", false);
    s_vaPrefs.putString("dgKey", deepgramKey);
    s_vaPrefs.putString("llmKey", llmKey);
    s_vaPrefs.putString("macUrl", macBaseUrl);
    s_vaPrefs.putString("macKey", macDeviceKey);
    s_vaPrefs.end();
    s_vaPrefsLoaded = false;
}

bool voice_agent_live_enabled() {
    ensureVaPrefsLoaded();
    return s_vaPrefs.getBool("liveOn", false); // off by default -- see this file's top comment
}

void voice_agent_set_live_enabled(bool enabled) {
    s_vaPrefs.end();
    s_vaPrefs.begin("jarvis_va", false);
    s_vaPrefs.putBool("liveOn", enabled);
    s_vaPrefs.end();
    s_vaPrefsLoaded = false;
}

static TaskHandle_t s_vaTask = nullptr;
static volatile bool s_active = false;
static volatile bool s_stopRequested = false;
static volatile bool s_usedRecorderFallback = false;
static WebSocketsClient s_ws;
static volatile bool s_wsConnected = false;
static volatile bool s_settingsApplied = false;
static String s_memoryFacts; // fetched once per session before beginSSL(), read from onWsEvent's WStype_CONNECTED case

// Accumulates a text control message across possibly-fragmented WS frames.
// The links2004/WebSockets library delivers a complete text frame per
// WStype_TEXT callback in practice (no manual fragment reassembly needed
// for the frame sizes Deepgram's control messages use), but kept as a
// String buffer rather than assuming a single fixed max size.
static String s_textBuffer;

// action_type -> {name, description, JSON schema for "parameters"} sent to
// Deepgram as agent.think.functions. Field names deliberately match
// jarvis.py's decide_action() decision dict keys 1:1 so the forwarded JSON
// can go straight into execute_decided_action() with no translation layer
// on the Mac side.
static String buildAgentFunctionsJson() {
    // Built as a raw JSON string (rather than via ArduinoJson's DynamicJsonDocument
    // for this static, never-changing part) since it's fixed at compile time.
    return String(
        "["
        "{\"name\":\"open_app\",\"description\":\"Launch a named application on the user's computer.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{\"app_name\":{\"type\":\"string\"}},\"required\":[\"app_name\"]}},"
        "{\"name\":\"calendar_event\",\"description\":\"Add an event to the user's calendar.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{"
        "\"title\":{\"type\":\"string\"},\"date\":{\"type\":\"string\",\"description\":\"YYYY-MM-DD\"},"
        "\"time\":{\"type\":\"string\",\"description\":\"HH:MM 24-hour\"}},\"required\":[\"title\"]}},"
        "{\"name\":\"reminder\",\"description\":\"Add a reminder for the user.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{\"title\":{\"type\":\"string\"}},\"required\":[\"title\"]}},"
        "{\"name\":\"email_draft\",\"description\":\"Draft an email. recipient_name may be a literal email address or a person's name.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{"
        "\"recipient_name\":{\"type\":\"string\"},\"query\":{\"type\":\"string\",\"description\":\"what the email should say\"}},"
        "\"required\":[\"recipient_name\",\"query\"]}},"
        "{\"name\":\"social_post\",\"description\":\"Draft a social/blog post based on the user's journal or a past conversation.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{"
        "\"referenced_person\":{\"type\":\"string\"},\"referenced_topic\":{\"type\":\"string\"},"
        "\"referenced_time_range\":{\"type\":\"string\"},\"query\":{\"type\":\"string\"}},\"required\":[]}},"
        "{\"name\":\"code_task\",\"description\":\"A software engineering request against the user's configured code repo.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{\"query\":{\"type\":\"string\"}},\"required\":[\"query\"]}},"
        "{\"name\":\"save_snippet\",\"description\":\"Save/remember a specific piece of content just said or referenced -- only for an explicit 'save this'/'remember that'/'save that snippet' instruction.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{\"snippet_text\":{\"type\":\"string\"}},\"required\":[\"snippet_text\"]}},"
        "{\"name\":\"search_memory\",\"description\":\"Search the user's past recordings/notes for something relevant to answer a question correctly -- call this when you need real information you don't already have (a fact, a date, what was discussed with someone) rather than guessing. Only call it when actually needed; a plain question with no dependency on past context should just be answered directly.\","
        "\"parameters\":{\"type\":\"object\",\"properties\":{"
        "\"query\":{\"type\":\"string\",\"description\":\"what to search for, in natural language\"},"
        "\"date_start\":{\"type\":\"string\",\"description\":\"YYYY-MM-DD, optional\"},"
        "\"date_end\":{\"type\":\"string\",\"description\":\"YYYY-MM-DD, optional\"},"
        "\"speaker\":{\"type\":\"string\",\"description\":\"optional -- restrict to a specific speaker\"}},"
        "\"required\":[\"query\"]}}"
        "]"
    );
}

static const char *AGENT_INSTRUCTIONS_BASE =
    "You are Jarvis, a terse voice assistant on a small e-paper device. For a plain question with no side "
    "effects, answer directly yourself in the fewest words possible -- a fact/number gets just the value and "
    "unit (e.g. '18 degrees'), never a full sentence, no pleasantries, no follow-up offers unless asked for "
    "detail. Call search_memory when answering correctly depends on something you don't already know (a past "
    "conversation, a fact about the user, a date) -- never guess when you could look it up. For anything that "
    "maps to one of your other available functions (opening an app, a calendar event, a reminder, an email, a "
    "social post, a coding task, or an explicit 'save this'/'remember that' instruction), call that function "
    "with its fields filled in as best you can from what was said -- do not ask clarifying questions first, "
    "act on the best interpretation available.";

// Fetched once per session from the Mac before streaming starts (see
// voiceAgentTask) -- memory_store.py's small, always-known facts list
// (distinct from search_memory's on-demand retrieval), so the agent has
// them without needing a function call for things it should just already
// know (e.g. a stated preference). Empty string if unreachable/unconfigured
// -- the agent still works, just without that context.
static String fetchMemoryFacts() {
    String macUrl = voice_agent_mac_base_url();
    String macKey = voice_agent_mac_device_key();
    if (macUrl.length() == 0 || macKey.length() == 0) return "";

    HTTPClient http;
    http.begin(macUrl + "/jarvis/memory-facts");
    http.addHeader("X-Jarvis-Key", macKey);
    http.setTimeout(5000);
    int status = http.GET();
    String result = "";
    if (status == 200) {
        result = http.getString();
    } else {
        Serial.printf("voice_agent: fetchMemoryFacts failed, HTTP %d\n", status);
    }
    http.end();
    return result;
}

// Sent once, immediately after the WS connection opens -- see Deepgram
// Voice Agent protocol: Settings message configures audio in/out format,
// listen/think/speak providers, and available functions.
static String buildSettingsJson(const String &memoryFacts) {
    JsonDocument doc; // ArduinoJson v7: no template size argument, grows as needed
    doc["type"] = "Settings";
    doc["audio"]["input"]["encoding"] = "linear16";
    doc["audio"]["input"]["sample_rate"] = SAMPLE_RATE;
    doc["audio"]["output"]["encoding"] = "linear16";
    doc["audio"]["output"]["sample_rate"] = SAMPLE_RATE;
    doc["audio"]["output"]["container"] = "none";

    doc["agent"]["listen"]["provider"]["type"] = "deepgram";
    doc["agent"]["listen"]["provider"]["model"] = "nova-3";

    // Groq by explicit request (cheapest/free tier) -- see this session's
    // plan doc. Swap model/type here if Groq's free tier proves too rate
    // limited in practice; nothing else in this file depends on which
    // provider is configured.
    doc["agent"]["think"]["provider"]["type"] = "groq";
    doc["agent"]["think"]["provider"]["model"] = "llama-3.1-8b-instant";
    String instructions = String(AGENT_INSTRUCTIONS_BASE);
    if (memoryFacts.length() > 0) {
        instructions += "\n\n" + memoryFacts;
    }
    doc["agent"]["think"]["prompt"] = instructions;

    doc["agent"]["speak"]["provider"]["type"] = "deepgram";
    doc["agent"]["speak"]["provider"]["model"] = "aura-asteria-en";

    String out;
    serializeJson(doc, out);

    // agent.think.functions is appended as raw JSON (built separately,
    // above) rather than round-tripped through ArduinoJson a second time --
    // simplest way to splice a pre-built JSON array into the "think" object
    // without fighting ArduinoJson's API for raw-JSON insertion.
    String functionsJson = buildAgentFunctionsJson();
    int thinkPos = out.indexOf("\"think\":{");
    if (thinkPos >= 0) {
        int insertPos = thinkPos + strlen("\"think\":{");
        out = out.substring(0, insertPos) + "\"functions\":" + functionsJson + "," + out.substring(insertPos);
    }
    return out;
}

// Forwards a Deepgram-decided action to the Mac for real execution (Notion/
// Obsidian/AppleScript automation only exists there). Builds the same
// decision-dict shape jarvis.py's decide_action() produces, so
// execute_decided_action() on the Mac needs no separate parsing path.
static bool forwardActionToMac(const String &actionType, JsonDocument &args) {
    String macUrl = voice_agent_mac_base_url();
    String macKey = voice_agent_mac_device_key();
    if (macUrl.length() == 0 || macKey.length() == 0) {
        Serial.println("voice_agent: Mac base URL / device key not configured, cannot forward action");
        return false;
    }

    JsonDocument decision;
    decision["action_type"] = actionType;
    decision["continues_session"] = false;
    decision["app_name"] = args["app_name"] | (const char *)nullptr;
    decision["title"] = args["title"] | (const char *)nullptr;
    decision["date"] = args["date"] | (const char *)nullptr;
    decision["time"] = args["time"] | (const char *)nullptr;
    decision["recipient_name"] = args["recipient_name"] | (const char *)nullptr;
    decision["referenced_person"] = args["referenced_person"] | (const char *)nullptr;
    decision["referenced_topic"] = args["referenced_topic"] | (const char *)nullptr;
    decision["referenced_time_range"] = args["referenced_time_range"] | (const char *)nullptr;
    decision["target_app"] = (const char *)nullptr;
    decision["query"] = args["query"] | (const char *)nullptr;
    decision["snippet_text"] = args["snippet_text"] | (const char *)nullptr;

    String body;
    serializeJson(decision, body);

    HTTPClient http;
    String url = macUrl + "/jarvis/execute-decision";
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Jarvis-Key", macKey);
    http.setTimeout(15000);
    int status = http.POST(body);
    bool ok = (status >= 200 && status < 300);
    if (!ok) {
        Serial.printf("voice_agent: forward to Mac failed, HTTP %d: %s\n", status, http.getString().c_str());
    }
    http.end();
    return ok;
}

// search_memory is a real information lookup -- unlike an action (fire,
// forget, maybe confirm later), the agent's live turn needs the result
// back to actually answer the question, so this sends a proper
// FunctionCallResponse over the WS (id/name/content, per Deepgram's
// protocol) instead of the fire-and-forget path actions use below.
static String forwardSearchToMac(JsonDocument &args) {
    String macUrl = voice_agent_mac_base_url();
    String macKey = voice_agent_mac_device_key();
    if (macUrl.length() == 0 || macKey.length() == 0) {
        return "Search unavailable -- Mac not configured.";
    }

    JsonDocument body;
    body["query"] = args["query"] | (const char *)nullptr;
    body["date_start"] = args["date_start"] | (const char *)nullptr;
    body["date_end"] = args["date_end"] | (const char *)nullptr;
    body["speaker"] = args["speaker"] | (const char *)nullptr;
    String bodyStr;
    serializeJson(body, bodyStr);

    HTTPClient http;
    http.begin(macUrl + "/jarvis/search-memory");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Jarvis-Key", macKey);
    http.setTimeout(15000);
    int status = http.POST(bodyStr);
    String result;
    if (status == 200) {
        result = http.getString(); // Mac returns {"result": "<text or 'nothing found'>"}
    } else {
        Serial.printf("voice_agent: search_memory forward failed, HTTP %d\n", status);
        result = "{\"result\":\"Search failed.\"}";
    }
    http.end();

    JsonDocument parsed;
    if (!deserializeJson(parsed, result)) {
        return parsed["result"] | "Nothing found.";
    }
    return "Nothing found.";
}

static void handleFunctionCallRequest(JsonDocument &msg) {
    JsonArray functions = msg["functions"].as<JsonArray>();
    for (JsonObject fn : functions) {
        String name = fn["name"] | "";
        String id = fn["id"] | "";
        String argsRaw = fn["arguments"] | "{}"; // Deepgram sends arguments as a JSON *string*, per protocol
        JsonDocument args;
        DeserializationError err = deserializeJson(args, argsRaw);
        if (err) {
            Serial.printf("voice_agent: could not parse function arguments for %s: %s\n", name.c_str(), err.c_str());
            continue;
        }
        Serial.printf("voice_agent: Deepgram called function=%s\n", name.c_str());

        if (name == "search_memory") {
            String resultText = forwardSearchToMac(args);
            JsonDocument response;
            response["type"] = "FunctionCallResponse";
            response["id"] = id;
            response["name"] = name;
            response["content"] = resultText;
            String responseStr;
            serializeJson(response, responseStr);
            s_ws.sendTXT(responseStr);
            continue;
        }

        bool ok = forwardActionToMac(name, args);
        // No client-side FunctionCallResponse is sent back here for actions
        // (Deepgram's protocol expects one so the agent's own turn can
        // react/speak a result) -- deferred: this first pass just executes
        // the action and lets the agent's own default turn continue, since
        // an action has no "answer" to report back the way search_memory
        // does. ok is logged for now, not yet surfaced back to Deepgram.
        (void)ok;
    }
}

static void onWsEvent(WStype_t type, uint8_t *payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED: {
            Serial.println("voice_agent: WS connected, sending Settings");
            String settings = buildSettingsJson(s_memoryFacts);
            s_ws.sendTXT(settings);
            s_wsConnected = true;
            break;
        }
        case WStype_DISCONNECTED:
            Serial.println("voice_agent: WS disconnected");
            s_wsConnected = false;
            s_settingsApplied = false;
            break;
        case WStype_TEXT: {
            JsonDocument msg;
            DeserializationError err = deserializeJson(msg, payload, length);
            if (err) {
                Serial.printf("voice_agent: could not parse control message: %s\n", err.c_str());
                break;
            }
            String msgType = msg["type"] | "";
            if (msgType == "SettingsApplied") {
                s_settingsApplied = true;
                Serial.println("voice_agent: SettingsApplied");
            } else if (msgType == "FunctionCallRequest") {
                handleFunctionCallRequest(msg);
            } else if (msgType == "Warning" || msgType == "Error") {
                Serial.printf("voice_agent: Deepgram %s: %s\n", msgType.c_str(), (const char *)(msg["description"] | ""));
            }
            // ConversationText/AgentThinking/AgentStartedSpeaking/AgentAudioDone/
            // History/LatencyReport are informational only -- nothing to act on.
            break;
        }
        case WStype_BIN:
            // Deepgram's synthesized speech (linear16/16kHz, matches
            // audio.output.encoding/sample_rate in Settings) -- play it back
            // immediately through the same speaker path recorder_play_wav
            // uses, minus the WAV header (there isn't one on this stream).
            audio_playback_write(payload, length);
            break;
        default:
            break;
    }
}

static void voiceAgentTask(void *arg) {
    audio_bsp_power_up();

    String dgKey = voice_agent_deepgram_api_key();
    if (dgKey.length() == 0) {
        Serial.println("voice_agent: no Deepgram API key configured, falling back to SD recording");
        s_usedRecorderFallback = true;
        recorder_start(true);
        s_active = false;
        s_vaTask = nullptr;
        vTaskDelete(NULL);
        return;
    }

    s_wsConnected = false;
    s_settingsApplied = false;
    s_memoryFacts = fetchMemoryFacts();
    s_ws.beginSSL("agent.deepgram.com", 443, "/v1/agent/converse");
    String authHeader = String("Authorization: Token ") + dgKey;
    s_ws.setExtraHeaders(authHeader.c_str());
    s_ws.onEvent(onWsEvent);

    // Wait briefly for the connection + Settings handshake before starting
    // to stream audio -- sending audio before SettingsApplied would arrive
    // against a session with no configured input format yet.
    uint32_t waitStart = millis();
    while (!s_settingsApplied && (millis() - waitStart) < 5000 && !s_stopRequested) {
        s_ws.loop();
        vTaskDelay(pdMS_TO_TICKS(20));
    }

    if (!s_settingsApplied) {
        Serial.println("voice_agent: Deepgram session did not confirm Settings in time, falling back to SD recording");
        s_ws.disconnect();
        s_usedRecorderFallback = true;
        recorder_start(true);
        s_active = false;
        s_vaTask = nullptr;
        vTaskDelete(NULL);
        return;
    }

    uint8_t *chunkBuf = (uint8_t *)heap_caps_malloc(CHUNK_BYTES, MALLOC_CAP_SPIRAM);
    Serial.println("voice_agent: streaming live to Deepgram Voice Agent");
    while (!s_stopRequested && s_wsConnected) {
        audio_playback_read(chunkBuf, CHUNK_BYTES);
        s_ws.sendBIN(chunkBuf, CHUNK_BYTES);
        s_ws.loop();
    }
    heap_caps_free(chunkBuf);

    if (!s_wsConnected && !s_stopRequested) {
        // Connection dropped mid-command (not a user-initiated stop) --
        // nothing was ever recorded to SD/RAM for this command, so unlike
        // every other fallback path here there's no partial audio to
        // salvage. Logged only; the user will need to repeat the command.
        Serial.println("voice_agent: connection lost mid-command, nothing to fall back to (no local recording was kept)");
    }

    s_ws.disconnect();
    if (!power_mgr_on_external_power()) audio_bsp_power_down();
    s_active = false;
    s_vaTask = nullptr;
    vTaskDelete(NULL);
}

void voice_agent_start_command() {
    if (s_active) return;
    s_stopRequested = false;
    s_usedRecorderFallback = false;
    s_active = true;
    xTaskCreatePinnedToCore(voiceAgentTask, "voiceAgent", 8 * 1024, NULL, 4, &s_vaTask, 1);
}

void voice_agent_stop() {
    s_stopRequested = true;
}

bool voice_agent_is_active() {
    return s_active;
}

bool voice_agent_used_recorder_fallback() {
    return s_usedRecorderFallback;
}
