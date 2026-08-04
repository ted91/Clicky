#ifndef FW_VERSION_H
#define FW_VERSION_H

// Bump this on every firmware change that ships in a release. Compared
// against the version string the paired pipeline app has bundled with it
// (see wifi_sync.cpp's /version route and /ota endpoint) -- the app only
// pushes a new firmware image when this is older than its own bundled one.
#define FW_VERSION "0.7.4"

#endif
