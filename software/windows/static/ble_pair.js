/* Shared BLE pairing widget.
 *
 * ONE implementation, used by both the standalone /pair page and the inline
 * Bluetooth card in Settings. The markup is built here rather than sitting in
 * two templates on purpose: a Jinja copy and a JS copy of the same UI drifting
 * apart is a bug this project has already paid for more than once (the
 * "text appears then vanishes" class of failure). Adding a third place to
 * mount this later means calling initBlePairing() on a container -- nothing
 * else.
 *
 * Why it scans in a loop instead of once: the firmware duty-cycles BLE hard to
 * save battery. It light-sleeps ~5s after going idle and pauses advertising
 * while asleep, waking on a timer every 5 min (recordings pending) or 10 min
 * (nothing pending) and advertising for only ~8 seconds. That is roughly a 3%
 * duty cycle, so a single scan misses far more often than it hits, and the old
 * one-shot page reported a healthy device as missing. Each /pair/scan request
 * blocks server-side for a full scan, so this loop is effectively back-to-back
 * scanning rather than polling with gaps the device can hide in.
 */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtElapsed(ms) {
    var s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  /* container      element to render into (emptied first)
   * opts.currentAddress   address already paired, pre-selected and labelled
   * opts.autoStart        begin scanning on init (default true)
   * opts.initialDevices   devices from a server-rendered first scan
   * opts.onPaired(addr)   called after a successful pair; return false to
   *                       suppress the built-in confirmation line
   */
  function initBlePairing(container, opts) {
    opts = opts || {};
    var currentAddress = opts.currentAddress || "";

    container.innerHTML =
      '<div class="blep-status">' +
        '<span class="blep-spinner"></span>' +
        '<span class="blep-headline"></span> ' +
        '<span class="blep-elapsed"></span>' +
      "</div>" +
      '<div class="blep-why">' +
        "The device advertises over Bluetooth in short bursts to save battery — " +
        "about <strong>8 seconds every 5 minutes</strong> while asleep — so it can " +
        "take a few minutes to appear. Scanning continues until it does." +
        "<br>To make it appear right away, <strong>press the BOOT button</strong> on " +
        "the device: that wakes it and turns advertising straight back on." +
      "</div>" +
      '<div class="blep-list"></div>' +
      '<div class="blep-actions">' +
        '<button type="button" class="secondary blep-toggle"></button>' +
        '<button type="button" class="blep-pair" style="display:none">Pair selected device</button>' +
      "</div>" +
      '<div class="blep-result"></div>';

    var el = {
      spinner: container.querySelector(".blep-spinner"),
      headline: container.querySelector(".blep-headline"),
      elapsed: container.querySelector(".blep-elapsed"),
      list: container.querySelector(".blep-list"),
      toggle: container.querySelector(".blep-toggle"),
      pair: container.querySelector(".blep-pair"),
      result: container.querySelector(".blep-result"),
    };

    var scanning = false;
    var startedAt = 0;
    var ticker = null;
    var seen = {};

    function setStatus(text, busy) {
      el.headline.textContent = text;
      el.spinner.style.display = busy ? "" : "none";
      if (!busy) el.elapsed.textContent = "";
      el.toggle.textContent = busy ? "Stop scanning" : "Scan again";
    }

    function addDevices(devices) {
      var added = 0;
      (devices || []).forEach(function (d) {
        if (!d.address || seen[d.address]) return;
        seen[d.address] = true;
        added++;
        var isCurrent = d.address === currentAddress;
        var label = document.createElement("label");
        label.className = "blep-device";
        label.innerHTML =
          '<input type="radio" name="blep-address" value="' + esc(d.address) + '"' +
            (isCurrent || Object.keys(seen).length === 1 ? " checked" : "") + ">" +
          "<span>" + esc(d.name || "(unnamed)") +
            (isCurrent ? ' <span class="blep-current">currently paired</span>' : "") +
            '<span class="blep-addr">' + esc(d.address) + "</span>" +
          "</span>";
        el.list.appendChild(label);
      });
      if (added) {
        el.pair.style.display = "";
        // Found it. Stop -- there is no reason to keep the radio busy, and
        // the sync poller wants the scanner back for its own passes.
        stop("Found your device. Select it and pair.");
      }
    }

    function stop(message) {
      scanning = false;
      if (ticker) { clearInterval(ticker); ticker = null; }
      setStatus(message || "Scanning stopped.", false);
    }

    function start() {
      if (scanning) return;
      scanning = true;
      startedAt = Date.now();
      setStatus("Scanning for your device…", true);
      if (!ticker) {
        ticker = setInterval(function () {
          if (scanning) el.elapsed.textContent = "(" + fmtElapsed(Date.now() - startedAt) + " elapsed)";
        }, 1000);
      }
      loop();
    }

    async function loop() {
      while (scanning) {
        try {
          var res = await fetch("/pair/scan", { headers: { Accept: "application/json" } });
          if (!res.ok) throw new Error("scan request failed (" + res.status + ")");
          var data = await res.json();
          if (!scanning) return;
          if (data.error) { stop("Scan failed: " + data.error); return; }
          addDevices(data.devices);
        } catch (e) {
          if (!scanning) return;
          stop("Scan failed: " + e.message);
          return;
        }
      }
    }

    el.toggle.addEventListener("click", function () {
      if (scanning) stop("Scanning stopped.");
      else start();
    });

    el.pair.addEventListener("click", async function () {
      var chosen = container.querySelector('input[name="blep-address"]:checked');
      if (!chosen) { el.result.textContent = "Select a device first."; return; }
      el.pair.disabled = true;
      el.result.textContent = "Pairing…";
      try {
        var res = await fetch("/pair/select", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ address: chosen.value }),
        });
        var data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || "pairing failed");
        currentAddress = data.address;
        var handled = opts.onPaired && opts.onPaired(data.address) === false;
        if (!handled) el.result.innerHTML = "&check; Paired with " + esc(data.address);
      } catch (e) {
        el.result.textContent = "Could not pair: " + e.message;
      } finally {
        el.pair.disabled = false;
      }
    });

    addDevices(opts.initialDevices || []);
    if (!Object.keys(seen).length) {
      if (opts.autoStart !== false) {
        start();
      } else {
        // Never-scanned state: "Scan again" would be a lie, and "Not
        // scanning" reads like an error rather than an invitation.
        setStatus("Not scanning.", false);
        el.toggle.textContent = "Scan for a device";
      }
    }

    return {
      start: start,
      stop: stop,
      isScanning: function () { return scanning; },
    };
  }

  global.initBlePairing = initBlePairing;
})(window);
