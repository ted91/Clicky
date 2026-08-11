// Polls /api/recordings and re-renders the list so new memos show up
// without a manual page reload. No build step, no framework — the server
// already renders the initial page via Jinja2; this just keeps it fresh.

const REFRESH_MS = 5000;
let lastSignature = "";

// Selected-for-bulk-delete hashes. refresh() replaces #recordings'
// innerHTML wholesale every REFRESH_MS -- a plain per-checkbox "checked"
// property would silently reset on every poll, so selection lives here
// instead and gets reapplied to the fresh checkboxes after each render.
const selectedHashes = new Set();

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function deleteButton(r) {
  // Two distinct actions: plain Delete only removes the local copy (and
  // permanently excludes this recording from ever re-syncing again, even
  // though the device keeps its own SD copy forever by design); "Delete
  // from device" additionally erases the file from the SD card itself --
  // a real, irreversible action, kept visually separate (danger styling)
  // so it's not clicked by mistake in place of the routine one.
  return `<button class="delete-btn" data-hash="${escapeHtml(r.content_hash)}" data-name="${escapeHtml(r.name)}">Delete</button>` +
    `<button class="delete-device-btn danger" data-hash="${escapeHtml(r.content_hash)}" data-name="${escapeHtml(r.name)}">Delete from device</button>`;
}

function checkbox(r) {
  return `<input type="checkbox" class="recording-select" data-hash="${escapeHtml(r.content_hash)}">`;
}

function renderRecording(r) {
  const audio = `<audio controls preload="none" src="/audio/${escapeHtml(r.content_hash)}.wav"></audio>`;
  const downloadBtn = `<a class="download-audio-btn" href="/audio/${escapeHtml(r.content_hash)}.wav?download=1" download title="Download original recording">Download</a>`;
  const audioRow = `<div class="audio-row">${audio}${downloadBtn}</div>`;

  if (r.status !== "done") {
    const badge = r.status === "pending"
      ? '<span class="badge pending">Processing&hellip;</span>'
      : '<span class="badge failed">Processing failed</span>';
    const body = r.status === "failed"
      ? `<p class="error-text">${escapeHtml(r.error)}</p><p class="providers">Will retry automatically next sync cycle — audio is already saved, no need to re-record.</p>`
      : `<p class="providers">Audio synced, waiting to be transcribed and summarized&hellip;</p>`;
    return `
      <div class="recording">
        <div class="recording-header">
          <div class="recording-title">${checkbox(r)}<h3>${escapeHtml(r.name)} ${badge}</h3></div>
          <div class="recording-actions">${deleteButton(r)}</div>
        </div>
        <div class="timestamp">${escapeHtml(r.created_at)}</div>
        ${audioRow}
        ${body}
      </div>
    `;
  }

  if (r.kind === "command") {
    // Jarvis voice commands skip the memo summarize() pipeline entirely
    // (see poller.process_once) -- r.summary stays null, so this must be
    // handled before any r.summary.* access below runs, or a Jarvis
    // recording would throw on every poll-refresh.
    const jr = r.jarvis_result || {};
    return `
      <div class="recording">
        <div class="recording-header">
          <div class="recording-title">${checkbox(r)}<h3>${escapeHtml(r.name)}</h3></div>
          <div class="recording-actions">${deleteButton(r)}</div>
        </div>
        <div class="timestamp">${escapeHtml(r.created_at)}</div>
        ${audioRow}
        <span class="badge sentiment-${jr.ok ? "positive" : "negative"}">🗣️ Jarvis — ${escapeHtml(jr.action_type || "unknown")}</span>
        <p><strong>Heard:</strong> ${escapeHtml(jr.transcript || "")}</p>
        ${jr.spoken ? `<p><strong>Replied:</strong> ${escapeHtml(jr.spoken)}</p>` : ""}
      </div>
    `;
  }

  const watchBlock = (item, i) => {
    let statusHtml;
    if (item.watch_triggered) {
      const first = (item.watch_matches && item.watch_matches[0]) || {};
      statusHtml = `<div class="watch-alert">📧 New mail matching "${escapeHtml(item.watch_query)}"${first.subject ? ` &mdash; ${escapeHtml(first.subject)}` : ""}<a href="javascript:void(0)" class="watch-toggle" data-index="${i}">change</a></div>`;
    } else if (item.watch_query) {
      statusHtml = `<div class="watch-status">👁 watching for "${escapeHtml(item.watch_query)}" <a href="javascript:void(0)" class="watch-toggle" data-index="${i}">change</a></div>`;
    } else {
      statusHtml = `<a href="javascript:void(0)" class="watch-toggle" data-index="${i}">+ watch for email</a>`;
    }
    return `${statusHtml}<span class="watch-form"><input type="text" class="watch-input" placeholder="email or keyword to watch for" value="${escapeHtml(item.watch_query || "")}"><button type="button" class="watch-save">Save</button><button type="button" class="watch-clear">Clear</button></span>`;
  };
  const actionItems = (r.summary.action_items || [])
    .map((item, i) => `<li class="${item.done ? "action-item-done" : ""}"><label class="action-item-check"><input type="checkbox" class="action-item-checkbox" data-index="${i}" ${item.done ? "checked" : ""}><span>${escapeHtml(item.text)}${item.owner ? ` &mdash; <span class="owner">${escapeHtml(item.owner)}</span>` : ""}${item.due_date ? ` <span class="due">(due ${escapeHtml(item.due_date)})</span>` : ""}</span></label>${watchBlock(item, i)}</li>`)
    .join("");
  const followUps = (r.summary.follow_ups || [])
    .map(fu => `<li>${escapeHtml(fu.text)}${fu.owner ? ` &mdash; <span class="owner">${escapeHtml(fu.owner)}</span>` : ""}</li>`)
    .join("");
  const contactWidget = (name) => `
    <a class="contact-toggle" data-name="${escapeHtml(name)}" href="javascript:void(0)">+ contact</a>
    <span class="contact-form" style="display:none;">
      <input type="email" class="contact-email" placeholder="email">
      <input type="url" class="contact-linkedin" placeholder="linkedin.com/in/...">
      <button type="button" class="contact-save">Save</button>
      <span class="contact-status"></span>
    </span>`;
  const stakeholders = (r.summary.stakeholders || [])
    .map((s, i) => `<li data-stakeholder-index="${i}"><span class="owner">${escapeHtml(s.name)}</span>${s.note ? ` &mdash; ${escapeHtml(s.note)}` : ""}${contactWidget(s.name)}
      <a class="stakeholder-edit" href="javascript:void(0)">edit</a>
      <span class="stakeholder-form" style="display:none;">
        <input type="text" class="stakeholder-name" value="${escapeHtml(s.name)}" placeholder="name">
        <button type="button" class="stakeholder-save">Save</button>
        <button type="button" class="stakeholder-remove danger">Remove</button>
        <span class="stakeholder-status"></span>
      </span></li>`)
    .join("");
  // Mirrors providers.base.format_organization / ORGANIZATION_ROLE_LABELS --
  // an unnamed org is shown, not hidden (see that function's docstring).
  const ORG_ROLES = {
    employer_of_speaker: "speaker's employer",
    subject: "under discussion",
    client: "client",
    investor: "investor",
    other: "mentioned",
  };
  const organizations = (r.summary.organizations || [])
    .map(o => {
      const name = (o.name || "").trim() || "(unnamed)";
      const role = ORG_ROLES[o.role] || o.role || "";
      return `<li><span class="owner">${escapeHtml(name)}</span>${role ? ` <span class="org-role">${escapeHtml(role)}</span>` : ""}${o.note ? ` &mdash; ${escapeHtml(o.note)}` : ""}</li>`;
    })
    .join("");
  const calendarEvents = (r.summary.calendar_events || [])
    .map(ev => `<li>📅 ${escapeHtml(ev.title)}${ev.date ? ` &mdash; ${escapeHtml(ev.date)}` : ""}${ev.time ? ` ${escapeHtml(ev.time)}` : ""}</li>`)
    .join("");

  const speakerNames = r.speaker_names || {};
  // merged_segments (computed server-side, see app.py's _recordings_for_display)
  // combines consecutive same-speaker segments into one block each, so a
  // turn reads as coherent prose instead of choppy diarization fragments.
  const mergedSegments = r.merged_segments || [];
  const transcriptHtml = mergedSegments.length
    ? `<div class="transcript" data-hash="${escapeHtml(r.content_hash)}">${mergedSegments.map(seg => {
        const isBackground = seg.loudness_class === "background";
        const tag = isBackground ? `<span class="background-tag" title="Classified as a quieter, more distant conversation -- excluded from the summary">background</span>` : "";
        // The space after ${tag} is load-bearing: these are inline
        // elements, so without it the tag and the speaker name run
        // together when the transcript is copied out of the page
        // ("backgroundBen Wiggins:" -- reported from a real paste).
        return `<p class="${isBackground ? "background-line" : ""}">${tag}${tag ? " " : ""}<span class="speaker rename-speaker" data-speaker-id="${escapeHtml(seg.speaker_id)}" title="Click to rename">${escapeHtml(speakerNames[seg.speaker_id] || `Speaker ${seg.speaker_id}`)}:</span> ${escapeHtml(seg.text)}</p>`;
      }).join("")}</div>`
    : `<pre class="transcript">${escapeHtml(r.transcript)}</pre>`;

  const confidenceClass = (score) => score >= 0.9 ? "confidence-green" : score >= 0.75 ? "confidence-yellow" : "confidence-red";
  const confidenceBar = (score) => {
    const pct = Math.round(score * 100);
    return `<span class="confidence-bar" title="${pct}% match"><span class="confidence-bar-fill ${confidenceClass(score)}" style="width: ${pct}%;"></span></span>`;
  };

  const speakerIds = r.segments && r.segments.length ? [...new Set(r.segments.map(s => s.speaker_id))] : [];
  const suggestions = (r.summary && r.summary.speaker_name_suggestions) || {};
  const candidatesBySid = (r.summary && r.summary.speaker_name_candidates) || {};
  const speakersSection = speakerIds.length ? `
      <div class="section-label">Speakers
        <a class="fix-names" href="javascript:void(0)" title="Re-check the summary, action items, follow-ups and stakeholders against these confirmed names, and fix any misspelled or leftover references">fix names everywhere</a>
      </div>
      <ul class="action-items speakers-list" data-hash="${escapeHtml(r.content_hash)}">
        ${speakerIds.map(sid => {
          const hasName = speakerNames[sid];
          const suggestion = suggestions[sid];
          const candidates = candidatesBySid[sid];
          let hint = "";
          if (suggestion && !hasName) {
            hint = `${confidenceBar(suggestion.score)}<span class="speaker-suggestion-tag" title="Recognized from voice (${Math.round(suggestion.score * 100)}% match) -- confirm or correct">recognized</span>`;
          } else if (candidates && candidates.length && !hasName) {
            hint = `<span class="speaker-candidates-hint">Might be: ${candidates.map(c =>
              `${confidenceBar(c.score)}<a href="javascript:void(0)" class="speaker-candidate-chip" data-speaker-id="${escapeHtml(sid)}" data-name="${escapeHtml(c.name)}" title="${Math.round(c.score * 100)}% match">${escapeHtml(c.name)}</a>`
            ).join(", ")} <span class="speaker-candidates-or">— or type a new name below</span></span>`;
          } else if (!hasName && r.voice_id_enabled) {
            hint = `<span class="speaker-slot-hint">No voice match — type a name to add them</span>`;
          }
          return `
          <li>
            <span class="speaker-slot-label">Speaker ${escapeHtml(sid)}</span>
            <input type="text" class="speaker-slot-input${suggestion && !hasName ? " speaker-suggested" : ""}" data-speaker-id="${escapeHtml(sid)}" value="${escapeHtml(hasName || (suggestion && !hasName ? suggestion.name : ""))}" placeholder="Speaker ${escapeHtml(sid)}">
            ${hint}
            <span class="speaker-slot-status"></span>
            ${contactWidget(hasName || "")}
          </li>`;
        }).join("")}
      </ul>` : "";

  const destBadges = `${r.notion_synced ? '<span class="badge dest">Notion</span>' : ""}${r.notion_tasks_synced ? '<span class="badge dest">Notion Tasks</span>' : ""}${r.notion_people_synced ? '<span class="badge dest">Notion People</span>' : ""}${r.notion_events_synced ? '<span class="badge dest">Notion Calendar</span>' : ""}${r.obsidian_synced ? '<span class="badge dest">Obsidian</span>' : ""}`;

  const ins = r.deepgram_insights;
  const sentimentEmoji = ins && ins.sentiment === "positive" ? "😊" : ins && ins.sentiment === "negative" ? "😔" : "😐";
  const sentimentBadge = ins && ins.sentiment
    ? `<span class="badge sentiment-${escapeHtml(ins.sentiment)}">${sentimentEmoji} ${escapeHtml(ins.sentiment.charAt(0).toUpperCase() + ins.sentiment.slice(1))}</span>`
    : "";
  const topicsHtml = ins && ins.topics && ins.topics.length
    ? `<div class="topics">${ins.topics.slice(0, 6).map(t => `<span class="topic-chip">${escapeHtml(t)}</span>`).join("")}</div>`
    : "";

  const meetingHeader = r.meeting ? `
      <div class="meeting-header">
        📅 ${escapeHtml(r.meeting.title || "Meeting")}
        ${(r.meeting.attendees || []).map(a => `<span class="attendee-chip">${escapeHtml(a.name)}</span>`).join("")}
      </div>` : "";

  // Each non-empty section becomes its own small "info card" in a
  // responsive grid (see .info-grid/.info-card in index.html's <style>)
  // instead of two fixed-height columns -- cards wrap to fill whatever
  // width is available rather than leaving one side mostly empty.
  const infoCards = [
    actionItems ? `<div class="info-card"><div class="section-label">Action items</div><ul class="action-items" data-hash="${escapeHtml(r.content_hash)}">${actionItems}</ul></div>` : "",
    followUps ? `<div class="info-card"><div class="section-label">Follow-ups</div><ul class="action-items">${followUps}</ul></div>` : "",
    stakeholders ? `<div class="info-card"><div class="section-label">Stakeholders</div><ul class="action-items">${stakeholders}</ul></div>` : "",
    organizations ? `<div class="info-card"><div class="section-label">Organizations</div><ul class="action-items">${organizations}</ul></div>` : "",
    calendarEvents ? `<div class="info-card"><div class="section-label">Calendar events</div><ul class="action-items">${calendarEvents}</ul></div>` : "",
    speakersSection ? `<div class="info-card">${speakersSection}</div>` : "",
    renderDrafts(r) ? `<div class="info-card">${renderDrafts(r)}</div>` : "",
  ].join("");

  return `
    <div class="recording" data-hash="${escapeHtml(r.content_hash)}">
      <div class="recording-header">
        <div class="recording-title">${checkbox(r)}<h3>${escapeHtml(r.name)}</h3></div>
        <div class="recording-actions">
          ${r.notion_url ? `<a href="${escapeHtml(r.notion_url)}" target="_blank" rel="noopener" class="quick-open-link">Open in Notion</a>` : ""}
          ${r.obsidian_url ? `<a href="${escapeHtml(r.obsidian_url)}" class="quick-open-link">Open in Obsidian</a>` : ""}
          ${deleteButton(r)}
        </div>
      </div>
      <div class="timestamp">${escapeHtml(r.created_at)}</div>
      ${audioRow}
      ${meetingHeader}
      ${sentimentBadge}
      ${topicsHtml}
      <p class="summary-text">${escapeHtml(r.summary.summary)}${r.summary_edited ? ` <span class="edited-badge" title="You edited this summary; automatic passes leave it alone">edited</span>` : ""}
        <a class="summary-edit" href="javascript:void(0)">edit</a></p>
      <div class="summary-form" style="display:none;">
        <textarea class="summary-input" rows="4">${escapeHtml(r.summary.summary)}</textarea>
        <button type="button" class="summary-save">Save</button>
        <button type="button" class="summary-cancel secondary">Cancel</button>
        <span class="summary-status"></span>
      </div>
      ${r.summary.excluded_background_note ? `<p class="providers" style="font-style: italic;">${escapeHtml(r.summary.excluded_background_note)}</p>` : ""}
      <div class="info-grid">
        ${infoCards}
        <div class="info-card transcript-card">
          <details>
            <summary>Full transcript</summary>
            ${transcriptHtml}
          </details>
          <div class="providers">stt=${escapeHtml(r.stt_provider)} · llm=${escapeHtml(r.llm_provider)} ${destBadges}</div>
        </div>
      </div>
    </div>
  `;
}

function draftSummaryLine(d) {
  if (d.kind === "email") {
    const recipient = (d.to && d.to.length) ? (d.to || []).join(", ") : (d.recipient_name || "(recipient unknown)");
    return `✉️ Email to ${escapeHtml(recipient)} — ${escapeHtml(d.subject)}`;
  }
  if (d.kind === "task") return `✅ Task: ${escapeHtml(d.title)}${d.due ? ` (due ${escapeHtml(d.due)})` : ""}`;
  if (d.kind === "calendar_event") return `📅 Follow-up event: ${escapeHtml(d.title)}`;
  return escapeHtml(d.kind);
}

function renderDrafts(r) {
  const items = (r.drafts && r.drafts.items) || [];
  if (!items.length) return "";
  return `
    <div class="section-label">Drafts</div>
    <ul class="action-items drafts-list" data-hash="${escapeHtml(r.content_hash)}">
      ${items.map(d => {
        // A standalone (non-meeting) email draft may not have a resolved
        // recipient address (see poller.py's _lookup_email_for_name) --
        // ask for one inline rather than blocking the draft entirely.
        const needsRecipient = d.kind === "email" && d.status === "pending" && !(d.to && d.to.length);
        return `
        <li class="draft-item" data-draft-id="${escapeHtml(d.id)}">
          <span class="draft-text">${draftSummaryLine(d)}</span>
          ${needsRecipient ? `<input type="email" class="draft-recipient-input" placeholder="${escapeHtml(d.recipient_name || 'recipient')}@example.com" data-draft-id="${escapeHtml(d.id)}">` : ""}
          ${d.status === "pending" ? `
            <span class="draft-actions">
              <button class="draft-approve-btn" data-draft-id="${escapeHtml(d.id)}" data-kind="${escapeHtml(d.kind)}">Approve</button>
              <button class="draft-dismiss-btn" data-draft-id="${escapeHtml(d.id)}">Dismiss</button>
            </span>
          ` : `<span class="badge ${d.status === 'approved_sent' ? 'dest' : 'failed'}">${d.status === 'approved_sent' ? 'Sent' : d.status === 'dismissed' ? 'Dismissed' : 'Failed'}</span>`}
          ${d.error ? `<div class="error-text">${escapeHtml(d.error)}</div>` : ""}
        </li>
      `;
      }).join("")}
    </ul>`;
}

async function refresh() {
  try {
    // The render below replaces #recordings' innerHTML wholesale, which
    // would destroy whatever the user is part-way through typing. Any open
    // edit form means "leave the DOM alone" -- the next poll (or the save
    // handler's own forced refresh) picks it up once they're done.
    if (document.querySelector(".summary-form[style*='block'], .stakeholder-form[style*='inline']")) return;

    const resp = await fetch("/api/recordings");
    if (!resp.ok) return;
    const recordings = await resp.json();

    // Re-render on any change to id/status/distribution, not just count --
    // a recording going pending -> done, or later getting pushed to
    // Notion/Obsidian, doesn't change how many there are. Also include the
    // summary text itself and speaker_names -- a rename triggers
    // poller.resync_after_rename to rewrite summary.summary/stakeholders
    // in place (see storage.update_summary) without touching status or
    // any *_synced flag, so without this the corrected prose was saved
    // server-side but the page would never re-render to show it.
    const signature = recordings.map(r => {
      const draftStatuses = (r.drafts && r.drafts.items || []).map(d => d.status).join("|");
      const summaryText = (r.summary && r.summary.summary) || "";
      const speakerNamesSig = JSON.stringify(r.speaker_names || {});
      const actionItemsSig = ((r.summary && r.summary.action_items) || [])
        .map(it => `${it.done}:${it.watch_query || ""}:${it.watch_triggered}`).join("|");
      // Stakeholders and organizations are both directly user-editable, so
      // they need to be in the signature for the same reason summaryText
      // is -- otherwise an edit saves server-side and never re-renders.
      const stakeholdersSig = JSON.stringify((r.summary && r.summary.stakeholders) || []);
      const orgsSig = JSON.stringify((r.summary && r.summary.organizations) || []);
      return `${r.id}:${r.status}:${r.notion_synced}:${r.notion_tasks_synced}:${r.notion_people_synced}:${r.notion_events_synced}:${r.obsidian_synced}:${draftStatuses}:${summaryText}:${r.summary_edited}:${speakerNamesSig}:${actionItemsSig}:${stakeholdersSig}:${orgsSig}`;
    }).join(",");
    if (signature === lastSignature) return;
    lastSignature = signature;

    const container = document.getElementById("recordings");
    container.innerHTML = recordings.length
      ? recordings.map(renderRecording).join("")
      : '<p class="empty">No recordings processed yet — record a memo on the device and it\'ll show up here within one poll interval.</p>';
    document.getElementById("count").textContent = recordings.length;

    // innerHTML above just blew away every checkbox's checked state --
    // reapply from selectedHashes (and drop any hash that no longer has a
    // matching recording, e.g. it was deleted from elsewhere).
    const presentHashes = new Set(recordings.map(r => r.content_hash));
    for (const hash of [...selectedHashes]) {
      if (!presentHashes.has(hash)) selectedHashes.delete(hash);
    }
    container.querySelectorAll(".recording-select").forEach(cb => {
      cb.checked = selectedHashes.has(cb.dataset.hash);
    });
    updateBulkBar();
  } catch (e) {
    // device/pipeline hiccup — silently retry on the next interval
  }
}

function updateBulkBar() {
  const bar = document.getElementById("bulk-bar");
  const countEl = document.getElementById("bulk-count");
  if (!bar || !countEl) return;
  countEl.textContent = selectedHashes.size;
  bar.classList.toggle("active", selectedHashes.size > 0);
}

// Bottom-right status pill: device connection dot (blue) and sync dot
// (green). Blinking = actively working, solid = last attempt succeeded,
// red = last attempt failed. Polled on the same interval as recordings.
async function refreshStatus() {
  try {
    const resp = await fetch("/api/status");
    if (!resp.ok) return;
    const s = await resp.json();

    const deviceDot = document.getElementById("dot-device");
    deviceDot.className = "status-dot " + (
      s.device_connecting ? "blue blink" : s.device_connected ? "blue" : "red"
    );

    const syncDot = document.getElementById("dot-sync");
    syncDot.className = "status-dot " + (
      s.sync_in_progress ? "green blink" : s.sync_ok ? "green" : "red"
    );

    // Byte-level download progress -- only reported by the BLE transports
    // (GATT and L2CAP), which stream in small chunks; the WiFi transport's
    // single blocking request has nothing to report incrementally, so
    // these fields are just null there and the label stays empty.
    const progressEl = document.getElementById("sync-progress");
    if (progressEl) {
      if (s.sync_progress_total) {
        const pct = Math.min(100, Math.round((s.sync_progress_bytes / s.sync_progress_total) * 100));
        // Prefix with the active transport so it's self-explanatory which
        // path is doing the work (WiFi auto-wins whenever reachable; BLE
        // is the fallback).
        const via = s.sync_transport_active ? `${s.sync_transport_active} ▸ ` : "";
        progressEl.textContent = `${via}${pct}%`;
      } else {
        progressEl.textContent = "";
      }
    }

    const label = document.getElementById("label-device");
    // Show the transport actually in use (resolved live by the poller),
    // falling back to the configured setting before the first poll.
    if (label) label.textContent = (s.sync_transport_active || s.sync_transport).toUpperCase();

    updateMeetingIndicator(s.meeting);
  } catch (e) {
    // pipeline hiccup — silently retry on the next interval
  }
}

// --- Meeting recording indicator ---
// Manual start/stop lives in the menu-bar agent (meetingcap), not the
// dashboard — this just reflects that state passively so a recording in
// progress isn't invisible, and forces a refresh once it ends so the new
// recording card appears without waiting a full poll interval.

let wasRecording = false;

function fmtElapsed(sec) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function updateMeetingIndicator(meeting) {
  const el = document.getElementById("meeting-indicator");
  const textEl = document.getElementById("meeting-indicator-text");
  if (!el) return;

  const recording = !!(meeting && meeting.recording);
  el.style.display = recording ? "flex" : "none";
  if (recording && textEl) {
    textEl.textContent = `Recording meeting… ${fmtElapsed(meeting.elapsed_sec || 0)}`;
  }

  if (wasRecording && !recording) {
    lastSignature = ""; // a recording just finished — pick up the new pending card immediately
    refresh();
  }
  wasRecording = recording;
}

// Shared by both rename surfaces (inline transcript label, Speakers-section
// input): posts the rename, then updates every matching element across the
// whole recording card -- transcript labels AND the Speakers-section label/
// input -- so the two stay in sync regardless of which one was edited.
async function submitSpeakerRename(hash, speakerId, name) {
  const resp = await fetch(`/recordings/${encodeURIComponent(hash)}/speakers/${encodeURIComponent(speakerId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `name=${encodeURIComponent(name)}`,
  });
  if (!resp.ok) throw new Error("request failed");

  const card = document.querySelector(`.transcript[data-hash="${CSS.escape(hash)}"]`)?.closest(".recording")
    || document.querySelector(`.speakers-list[data-hash="${CSS.escape(hash)}"]`)?.closest(".recording");
  if (!card) return;
  const label = name || `Speaker ${speakerId}`;
  card.querySelectorAll(`.rename-speaker[data-speaker-id="${CSS.escape(speakerId)}"]`)
    .forEach(el => { el.textContent = `${label}:`; });
  card.querySelectorAll(`.speaker-slot-input[data-speaker-id="${CSS.escape(speakerId)}"]`).forEach(el => {
    el.value = name;
  });
  lastSignature = ""; // force next poll to pick up the persisted name
}

// Inline rename: swaps the clicked speaker label for a text <input> right
// in place, rather than window.prompt() -- prompt() is unreliable on
// mobile browsers (some suppress it entirely inside embedded/PWA contexts),
// so this needed to be a real DOM element to work everywhere.
function startSpeakerRename(speakerEl) {
  if (speakerEl.querySelector("input")) return; // already editing
  const container = speakerEl.closest(".transcript");
  const hash = container.dataset.hash;
  const speakerId = speakerEl.dataset.speakerId;
  const current = speakerEl.textContent.replace(/:$/, "");
  const currentName = current.startsWith("Speaker ") ? "" : current;

  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.placeholder = `Speaker ${speakerId}`;
  input.className = "speaker-rename-input";
  input.size = Math.max(8, (currentName || speakerId).length + 2);

  const originalHtml = speakerEl.innerHTML;
  speakerEl.textContent = "";
  speakerEl.appendChild(input);
  input.focus();
  input.select();

  let settled = false;
  const revert = () => { if (!settled) { settled = true; speakerEl.innerHTML = originalHtml; } };

  const commit = async () => {
    if (settled) return;
    settled = true;
    const name = input.value.trim();
    try {
      await submitSpeakerRename(hash, speakerId, name);
    } catch (err) {
      alert("Rename failed — check the pipeline log.");
      speakerEl.innerHTML = originalHtml;
    }
  };

  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); commit(); }
    else if (ev.key === "Escape") { ev.preventDefault(); revert(); }
  });
  input.addEventListener("blur", commit);
  input.addEventListener("click", (ev) => ev.stopPropagation());
}

// Speakers-section inputs: commit on blur (via focusout, which -- unlike
// blur -- bubbles, so this can stay one delegated listener like everything
// else here) or Enter. A small status label next to each input gives
// feedback without needing a popup, since this section is meant to be the
// primary/lower-friction way to rename speakers vs. opening the transcript.
async function commitSpeakerSlot(input) {
  const li = input.closest("li");
  const status = li?.querySelector(".speaker-slot-status");
  const list = input.closest(".speakers-list");
  const hash = list.dataset.hash;
  const speakerId = input.dataset.speakerId;
  const name = input.value.trim();
  if (input.dataset.lastSubmitted === name) return; // no change since last save
  if (status) status.textContent = "Saving…";
  try {
    await submitSpeakerRename(hash, speakerId, name);
    input.dataset.lastSubmitted = name;
    if (status) {
      status.textContent = "Saved";
      setTimeout(() => { if (status.textContent === "Saved") status.textContent = ""; }, 2000);
    }
  } catch (err) {
    if (status) status.textContent = "Failed";
  }
}

document.getElementById("recordings").addEventListener("change", (e) => {
  const cb = e.target.closest(".recording-select");
  if (cb) {
    if (cb.checked) selectedHashes.add(cb.dataset.hash);
    else selectedHashes.delete(cb.dataset.hash);
    updateBulkBar();
    return;
  }
  const itemCb = e.target.closest(".action-item-checkbox");
  if (itemCb) {
    const hash = itemCb.closest(".action-items")?.dataset.hash;
    const li = itemCb.closest("li");
    li.classList.toggle("action-item-done", itemCb.checked);
    fetch(`/recordings/${encodeURIComponent(hash)}/action-item/${itemCb.dataset.index}`, {
      method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: `done=${itemCb.checked}`,
    }).catch(() => {});
  }
});

document.getElementById("recordings").addEventListener("focusout", (e) => {
  const input = e.target.closest(".speaker-slot-input");
  if (input) commitSpeakerSlot(input);
});
document.getElementById("recordings").addEventListener("keydown", (e) => {
  const input = e.target.closest(".speaker-slot-input");
  if (input && e.key === "Enter") { e.preventDefault(); input.blur(); }
});

// Event delegation on the container (not per-card listeners) so this keeps
// working after refresh() replaces #recordings' innerHTML wholesale.
document.getElementById("recordings").addEventListener("click", async (e) => {
  const speakerEl = e.target.closest(".rename-speaker");
  if (speakerEl) {
    startSpeakerRename(speakerEl);
    return;
  }

  // Voice-ID "might be one of these" candidate chip -- fills the adjacent
  // rename input with the clicked name but does NOT save it; the user
  // still has to confirm (same input/save flow as any other rename), this
  // just saves them typing it out. See voice_id.match_candidates.
  const candidateChip = e.target.closest(".speaker-candidate-chip");
  if (candidateChip) {
    const li = candidateChip.closest("li");
    const input = li ? li.querySelector(".speaker-slot-input") : null;
    if (input) {
      input.value = candidateChip.dataset.name;
      input.focus();
    }
    return;
  }

  const watchToggle = e.target.closest(".watch-toggle");
  if (watchToggle) {
    const form = watchToggle.closest("li").querySelector(".watch-form");
    form.classList.toggle("active");
    return;
  }

  const watchSaveBtn = e.target.closest(".watch-save");
  if (watchSaveBtn) {
    const li = watchSaveBtn.closest("li");
    const hash = li.closest(".action-items").dataset.hash;
    const index = li.querySelector(".watch-toggle")?.dataset.index ?? li.querySelector(".action-item-checkbox")?.dataset.index;
    const query = li.querySelector(".watch-input").value.trim();
    await fetch(`/recordings/${encodeURIComponent(hash)}/action-item/${index}/watch`, {
      method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: `watch_query=${encodeURIComponent(query)}`,
    }).catch(() => {});
    lastSignature = "";
    refresh();
    return;
  }

  const watchClearBtn = e.target.closest(".watch-clear");
  if (watchClearBtn) {
    const li = watchClearBtn.closest("li");
    const hash = li.closest(".action-items").dataset.hash;
    const index = li.querySelector(".watch-toggle")?.dataset.index ?? li.querySelector(".action-item-checkbox")?.dataset.index;
    await fetch(`/recordings/${encodeURIComponent(hash)}/action-item/${index}/watch`, {
      method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: `watch_query=`,
    }).catch(() => {});
    lastSignature = "";
    refresh();
    return;
  }

  const fixNames = e.target.closest(".fix-names");
  if (fixNames) {
    const hash = fixNames.closest(".recording").dataset.hash;
    const original = fixNames.textContent;
    fixNames.textContent = "fixing...";
    fetch(`/recordings/${hash}/fix-names`, { method: "POST" })
      .then(r => r.json())
      .then(d => {
        // The correction runs as a background task (an LLM call plus a
        // Notion/Obsidian re-push), so there's nothing to show yet -- the
        // 5s poll picks up the corrected text when it lands.
        fixNames.textContent = d.ok ? "fixing in background..." : "failed";
        setTimeout(() => { fixNames.textContent = original; }, 8000);
      })
      .catch(() => { fixNames.textContent = "failed"; });
    return;
  }

  const summaryEdit = e.target.closest(".summary-edit");
  if (summaryEdit) {
    const p = summaryEdit.closest(".summary-text");
    const form = p.nextElementSibling;
    p.style.display = "none";
    form.style.display = "block";
    form.querySelector(".summary-input").focus();
    return;
  }

  const summaryCancel = e.target.closest(".summary-cancel");
  if (summaryCancel) {
    const form = summaryCancel.closest(".summary-form");
    form.style.display = "none";
    form.previousElementSibling.style.display = "";
    return;
  }

  const summarySave = e.target.closest(".summary-save");
  if (summarySave) {
    const form = summarySave.closest(".summary-form");
    const hash = form.closest(".recording").dataset.hash;
    const text = form.querySelector(".summary-input").value;
    const statusEl = form.querySelector(".summary-status");
    statusEl.textContent = "Saving...";
    fetch(`/recordings/${hash}/summary`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `summary=${encodeURIComponent(text)}`,
    })
      .then(r => r.json())
      .then(d => {
        if (!d.ok) { statusEl.textContent = "Failed."; return; }
        // Close the form BEFORE refreshing: refresh() deliberately bails
        // out while any edit form is open (see its guard), so leaving it
        // open here would suppress the very re-render we're asking for.
        form.style.display = "none";
        form.previousElementSibling.style.display = "";
        lastSignature = "";  // force a re-render so the new text and "edited" badge appear
        refresh();
      })
      .catch(() => { statusEl.textContent = "Failed."; });
    return;
  }

  const stakeholderEdit = e.target.closest(".stakeholder-edit");
  if (stakeholderEdit) {
    const form = stakeholderEdit.nextElementSibling;
    form.style.display = form.style.display === "none" ? "inline" : "none";
    return;
  }

  const stakeholderSave = e.target.closest(".stakeholder-save");
  const stakeholderRemove = e.target.closest(".stakeholder-remove");
  if (stakeholderSave || stakeholderRemove) {
    const btn = stakeholderSave || stakeholderRemove;
    const li = btn.closest("li");
    const hash = btn.closest(".recording").dataset.hash;
    const index = li.dataset.stakeholderIndex;
    // An empty name is how the backend expresses "remove this entry"
    // (storage.set_stakeholder) -- so Remove is just a save of "".
    const name = stakeholderRemove ? "" : li.querySelector(".stakeholder-name").value.trim();
    const statusEl = li.querySelector(".stakeholder-status");
    statusEl.textContent = "Saving...";
    fetch(`/recordings/${hash}/stakeholder/${index}`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `name=${encodeURIComponent(name)}`,
    })
      .then(r => r.json())
      .then(d => {
        if (!d.ok) { statusEl.textContent = "Failed."; return; }
        // Close before refreshing -- refresh() bails while a form is open.
        li.querySelector(".stakeholder-form").style.display = "none";
        lastSignature = "";  // force a re-render so the edited list reappears
        refresh();
      })
      .catch(() => { statusEl.textContent = "Failed."; });
    return;
  }

  const contactToggle = e.target.closest(".contact-toggle");
  if (contactToggle) {
    const form = contactToggle.nextElementSibling;
    form.style.display = form.style.display === "none" ? "inline" : "none";
    return;
  }

  const contactSaveBtn = e.target.closest(".contact-save");
  if (contactSaveBtn) {
    const form = contactSaveBtn.closest(".contact-form");
    const toggle = form.previousElementSibling;
    // Speaker slots can be renamed in the same session before contact info
    // is ever saved -- read the live value from the adjacent rename input
    // if there is one, rather than whichever name this render happened to
    // have. Stakeholders have no such input, so they fall back to the
    // fixed data-name.
    const li = toggle.closest("li");
    const renameInput = li ? li.querySelector(".speaker-slot-input") : null;
    const name = (renameInput ? renameInput.value.trim() : "") || toggle.dataset.name;
    const email = form.querySelector(".contact-email").value.trim();
    const linkedin = form.querySelector(".contact-linkedin").value.trim();
    const statusEl = form.querySelector(".contact-status");
    if (!name) {
      statusEl.textContent = "Type a name for this speaker first.";
      return;
    }
    statusEl.textContent = "Saving…";
    try {
      const resp = await fetch("/people/contact", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ name, email, linkedin }),
      });
      const data = await resp.json();
      statusEl.textContent = data.ok ? "Saved." : ("Failed: " + (data.error || "unknown error"));
    } catch (err) {
      statusEl.textContent = "Failed: " + err;
    }
    return;
  }

  const approveBtn = e.target.closest(".draft-approve-btn");
  if (approveBtn) {
    const hash = approveBtn.closest(".drafts-list").dataset.hash;
    const draftId = approveBtn.dataset.draftId;
    const kind = approveBtn.dataset.kind;
    const label = kind === "email" ? "send this email" : kind === "task" ? "create this task" : "create this calendar event";

    // Email drafts (see poller.py's _build_email_drafts) may not have a
    // resolved recipient yet -- renderDrafts() shows an
    // inline input for that case; require it filled in before sending.
    const recipientInput = approveBtn.closest(".draft-item")?.querySelector(".draft-recipient-input");
    const recipient = recipientInput ? recipientInput.value.trim() : "";
    if (recipientInput && !recipient) {
      alert("Enter a recipient email address first.");
      recipientInput.focus();
      return;
    }

    if (!confirm(`Approve — ${label}?`)) return;
    approveBtn.disabled = true;
    approveBtn.textContent = "Sending…";
    try {
      const resp = await fetch(`/recordings/${encodeURIComponent(hash)}/drafts/${encodeURIComponent(draftId)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: recipient ? `to=${encodeURIComponent(recipient)}` : "",
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        alert(data.detail || (data.error === "missing_recipient" ? "Enter a recipient email address first." : "Approve failed — check the pipeline log."));
        approveBtn.disabled = false;
        approveBtn.textContent = "Approve";
        return;
      }
      lastSignature = "";
      refresh();
    } catch (err) {
      alert("Approve failed — check the pipeline log.");
      approveBtn.disabled = false;
      approveBtn.textContent = "Approve";
    }
    return;
  }

  const dismissBtn = e.target.closest(".draft-dismiss-btn");
  if (dismissBtn) {
    const hash = dismissBtn.closest(".drafts-list").dataset.hash;
    const draftId = dismissBtn.dataset.draftId;
    dismissBtn.disabled = true;
    try {
      const resp = await fetch(`/recordings/${encodeURIComponent(hash)}/drafts/${encodeURIComponent(draftId)}/dismiss`, { method: "POST" });
      if (!resp.ok) {
        alert("Dismiss failed — check the pipeline log.");
        dismissBtn.disabled = false;
        return;
      }
      lastSignature = "";
      refresh();
    } catch (err) {
      alert("Dismiss failed — check the pipeline log.");
      dismissBtn.disabled = false;
    }
    return;
  }

  const unmergeBtn = e.target.closest(".unmerge-btn");
  if (unmergeBtn) {
    const keeperHash = unmergeBtn.dataset.keeperHash;
    const loserHash = unmergeBtn.dataset.loserHash;
    if (!confirm("Split this back into two separate recordings?")) return;

    unmergeBtn.disabled = true;
    try {
      const resp = await fetch(`/recordings/${encodeURIComponent(keeperHash)}/unmerge/${encodeURIComponent(loserHash)}`, { method: "POST" });
      if (!resp.ok) {
        alert("Unmerge failed — check the pipeline log.");
        unmergeBtn.disabled = false;
        return;
      }
      location.reload();
    } catch (e) {
      alert("Unmerge failed — check the pipeline log.");
      unmergeBtn.disabled = false;
    }
    return;
  }

  const deviceBtn = e.target.closest(".delete-device-btn");
  if (deviceBtn) {
    const hash = deviceBtn.dataset.hash;
    const name = deviceBtn.dataset.name;
    if (!confirm(`Permanently delete "${name}" from the device's SD card too?\n\nThis cannot be undone — unlike plain Delete, the recording will NOT be recoverable from the device afterward.`)) return;

    deviceBtn.disabled = true;
    try {
      const resp = await fetch(`/recordings/${encodeURIComponent(hash)}/device`, { method: "DELETE" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        alert("Delete failed — check the pipeline log.");
        deviceBtn.disabled = false;
        return;
      }
      if (!data.deleted_on_device) {
        alert(`Removed locally, but couldn't reach the device to delete it there too (${data.device_error || "unknown error"}). ` +
              `It won't re-sync (already excluded), but the file still physically exists on the SD card until the device is reachable again.`);
      }
      selectedHashes.delete(hash);
      updateBulkBar();
      deviceBtn.closest(".recording").remove();
      lastSignature = "";
      document.getElementById("count").textContent = document.querySelectorAll(".recording").length;
    } catch (e) {
      alert("Delete failed — check the pipeline log.");
      deviceBtn.disabled = false;
    }
    return;
  }

  const btn = e.target.closest(".delete-btn");
  if (!btn) return;

  const hash = btn.dataset.hash;
  const name = btn.dataset.name;
  if (!confirm(`Delete "${name}"? This removes the audio file and its transcript/summary permanently, and it won't be re-synced from the device.`)) return;

  btn.disabled = true;
  try {
    const resp = await fetch(`/recordings/${encodeURIComponent(hash)}`, { method: "DELETE" });
    if (!resp.ok) {
      alert("Delete failed — check the pipeline log.");
      btn.disabled = false;
      return;
    }
    selectedHashes.delete(hash);
    updateBulkBar();
    btn.closest(".recording").remove();
    lastSignature = ""; // force the next poll to pick up the change immediately
    document.getElementById("count").textContent = document.querySelectorAll(".recording").length;
  } catch (e) {
    alert("Delete failed — check the pipeline log.");
    btn.disabled = false;
  }
});

// --- Bulk delete (multi-select checkboxes + bulk-bar) ---
// Reuses the same two single-recording endpoints as the per-card delete
// buttons, just looped sequentially -- no batch endpoint needed. Sequential
// (not Promise.all) so a slow/failing device connection doesn't fire a
// pile of concurrent BLE/HTTP calls at once.

async function bulkDelete(fromDevice) {
  const hashes = [...selectedHashes];
  if (!hashes.length) return;

  const verb = fromDevice ? "permanently delete from the device's SD card" : "delete";
  if (!confirm(`${verb.charAt(0).toUpperCase() + verb.slice(1)} ${hashes.length} recording(s)?` +
    (fromDevice ? "\n\nThis cannot be undone — they will NOT be recoverable from the device afterward." : ""))) {
    return;
  }

  const bar = document.getElementById("bulk-bar");
  bar.querySelectorAll("button").forEach(b => b.disabled = true);

  const failures = [];
  for (const hash of hashes) {
    try {
      const url = fromDevice ? `/recordings/${encodeURIComponent(hash)}/device` : `/recordings/${encodeURIComponent(hash)}`;
      const resp = await fetch(url, { method: "DELETE" });
      if (!resp.ok) { failures.push(hash); continue; }
      selectedHashes.delete(hash);
    } catch (e) {
      failures.push(hash);
    }
  }

  bar.querySelectorAll("button").forEach(b => b.disabled = false);
  if (failures.length) {
    alert(`${failures.length} of ${hashes.length} failed to delete — check the pipeline log. The rest were removed.`);
  }
  lastSignature = "";
  refresh();
}

document.getElementById("bulk-delete-btn").addEventListener("click", () => bulkDelete(false));
document.getElementById("bulk-delete-device-btn").addEventListener("click", () => bulkDelete(true));
document.getElementById("bulk-clear-btn").addEventListener("click", () => {
  selectedHashes.clear();
  document.querySelectorAll(".recording-select").forEach(cb => cb.checked = false);
  updateBulkBar();
});

// "Select all" / "Delete all" -- reuse the existing bulk-select state and
// bulkDelete() rather than a separate all-at-once endpoint, so the same
// per-item confirm/failure handling applies.
function selectAllRecordings() {
  document.querySelectorAll(".recording-select").forEach(cb => {
    cb.checked = true;
    selectedHashes.add(cb.dataset.hash);
  });
  updateBulkBar();
}
const selectAllLink = document.getElementById("select-all-link");
if (selectAllLink) selectAllLink.addEventListener("click", selectAllRecordings);
const deleteAllLink = document.getElementById("delete-all-link");
if (deleteAllLink) deleteAllLink.addEventListener("click", () => {
  selectAllRecordings();
  bulkDelete(false);
});

// --- "Confirm who this is" queue ---
// A Task/Calendar entry created with just a name (task owner, email draft
// recipient) and 1+ existing People pages sharing that name never gets
// auto-linked (see notion_sync.py's resolve_person_for_relation -- even a
// single match isn't confident enough on its own, only a real email is).
// This polls for those and lets the user pick the right one, or say it's
// someone new entirely.

let lastPersonLinkSignature = "";

function renderPersonLinkCard(link) {
  const options = link.candidates.map(c => {
    const detail = [c.note, c.email].filter(Boolean).join(" — ");
    return `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}${detail ? ` (${escapeHtml(detail)})` : ""}</option>`;
  }).join("");
  return `
    <div class="person-link-card" data-link-id="${escapeHtml(link.id)}">
      <div class="title">Which <strong>${escapeHtml(link.name)}</strong> is this? (from "${escapeHtml(link.recording_name)}")</div>
      <div class="person-link-row">
        <select class="person-link-select">
          ${options}
          <option value="__new__">Someone new</option>
        </select>
        <input type="text" class="person-link-new-name" placeholder="${escapeHtml(link.name)}">
        <button type="button" class="person-link-confirm-btn">Confirm</button>
      </div>
    </div>
  `;
}

async function refreshPendingPersonLinks() {
  try {
    const resp = await fetch("/api/pending-person-links");
    if (!resp.ok) return;
    const links = await resp.json();

    const signature = links.map(l => l.id).join(",");
    if (signature === lastPersonLinkSignature) return;
    lastPersonLinkSignature = signature;

    document.getElementById("person-link-queue").innerHTML = links.map(renderPersonLinkCard).join("");
  } catch (e) {
    // pipeline hiccup — silently retry on the next interval
  }
}

document.getElementById("person-link-queue").addEventListener("change", (e) => {
  const select = e.target.closest(".person-link-select");
  if (!select) return;
  const card = select.closest(".person-link-card");
  card.querySelector(".person-link-new-name").classList.toggle("active", select.value === "__new__");
});

document.getElementById("person-link-queue").addEventListener("click", async (e) => {
  const btn = e.target.closest(".person-link-confirm-btn");
  if (!btn) return;
  const card = btn.closest(".person-link-card");
  const linkId = card.dataset.linkId;
  const select = card.querySelector(".person-link-select");
  const newNameInput = card.querySelector(".person-link-new-name");
  const isNew = select.value === "__new__";

  if (isNew && !newNameInput.value.trim()) {
    alert("Enter the new person's name first.");
    newNameInput.focus();
    return;
  }

  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const body = isNew
      ? `new_person_name=${encodeURIComponent(newNameInput.value.trim())}`
      : `person_page_id=${encodeURIComponent(select.value)}`;
    const resp = await fetch(`/api/pending-person-links/${encodeURIComponent(linkId)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!resp.ok) {
      alert("Couldn't save that — check the pipeline log.");
      btn.disabled = false;
      btn.textContent = "Confirm";
      return;
    }
    card.remove();
    lastPersonLinkSignature = ""; // force next poll to reflect the change
  } catch (e) {
    alert("Couldn't save that — check the pipeline log.");
    btn.disabled = false;
    btn.textContent = "Confirm";
  }
});

setInterval(refresh, REFRESH_MS);
setInterval(refreshStatus, REFRESH_MS);
setInterval(refreshPendingPersonLinks, REFRESH_MS);
refreshStatus();
refreshPendingPersonLinks();
