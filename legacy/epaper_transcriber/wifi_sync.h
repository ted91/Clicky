#ifndef WIFI_SYNC_H
#define WIFI_SYNC_H

// Joins the configured WiFi network and starts an HTTP server exposing the
// SD card's recordings so a phone/laptop on the same network can pull them:
//   GET /                 -> simple HTML list of recordings with links
//   GET /list             -> JSON array of {"name","size"} for scripting
//   GET /rec?name=<file>  -> raw WAV bytes for a given recording
//   DELETE /rec?name=<file> -> remove a recording once it's been synced
//
// Call once from setup() after sdcard_init(). Non-blocking: WiFi connects
// in the background and the server simply won't serve anything until then.
void wifi_sync_init();

// Call frequently from loop()/a task; handles incoming HTTP requests.
void wifi_sync_tick();

bool wifi_sync_is_connected();

// True while a GET /rec transfer is actively streaming data out.
bool wifi_sync_is_transferring();

#endif
