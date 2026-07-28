// meetingcap — a persistent macOS menu-bar agent that captures system audio
// (ScreenCaptureKit) + microphone (AVAudioEngine) into a 16kHz stereo s16le
// WAV per recording:
//   left channel  = system audio (the other meeting participants)
//   right channel = microphone (the user)
//
// The channel split is deliberate: no lossy mixing, and downstream STT can
// attribute "user vs. everyone else" deterministically (Deepgram
// multichannel=true transcribes each channel independently).
//
// Launched once by Clicky at startup and left running for the lifetime of
// the app (menu-bar icon only, no Dock icon -- see setActivationPolicy
// below). Recording is controlled two ways:
//   1. The user clicks the menu-bar icon directly (manual start/stop).
//   2. Clicky's Python side sends a command over stdin when it auto-detects
//      a calendar meeting starting (see meeting_recorder.py).
// Either path shows the same on-screen "recording" banner with a
// stop-and-discard button -- since system audio capture includes other
// meeting participants' voices without them clicking anything, a visible
// signal + an easy way to cancel is the consent-conscious default.
//
// stdin commands (JSON lines) -- driven by meeting_recorder.py:
//   {"cmd":"start","meeting":{...}|null}
//   {"cmd":"stop"}
//   {"cmd":"status"}
//   {"cmd":"show_prep","title":"...","body":"..."}  pre-meeting prep note,
//     shown as a popover anchored under the menu-bar icon (not a
//     Notification Center banner -- avoids truncation for longer context).
//     See poller.py's check_meeting_prep_once.
//   {"cmd":"get_event","id":"...","window_min":N}  Apple Calendar lookup
//     via EventKit (see CalendarLookup) -- request/response, correlated by
//     "id"; response comes back as a "calendar_event" stdout event below.
//     See apple_calendar.py / meeting_recorder.get_apple_calendar_event.
//   {"cmd":"quit"}
// stdout events (JSON lines) -- read continuously by meeting_recorder.py:
//   {"event":"ready"}                                     agent initialized, icon visible
//   {"event":"recording_started","source":"manual"|"auto"}
//   {"event":"recording_stopped","path":"...","duration_sec":N,"discarded":bool}
//   {"event":"error","code":"...","message":"..."}
//   {"event":"user_clicked_start"} / {"event":"user_clicked_stop"}  menu-driven, not command-driven
//   {"event":"calendar_event","id":"...","data":{...}|null}  response to "get_event"
//   {"event":"adhoc_meeting_detected","app":"bundle.id","meeting_url":"..."|null}
//     an instantaneous/non-calendar meeting was detected (see
//     checkForAdhocMeeting below) -- either a known conferencing app
//     (Teams/Zoom/Slack) actively doing microphone input, or a browser
//     doing mic input with a real (not pre-join-lobby) Google Meet call
//     URL open in some tab. meeting_recorder.py's _handle_event starts a
//     recording for this the same way it does for a calendar auto-start,
//     just with a synthetic meeting dict (no real calendar event exists).
//   {"event":"user_quit"}  sent just before exit(0) when the user picks
//     Quit from the menu -- meeting_recorder.py treats this as "shut the
//     whole app down now," distinct from the agent merely disappearing
//     (crash), which shouldn't kill the rest of the running server.
//
// Build: see build.sh (plain swiftc, no Xcode project).

import AppKit
import AVFoundation
import CoreAudio
import CoreMedia
import EventKit
import Foundation
import ScreenCaptureKit

let SAMPLE_RATE: Double = 16000
let OUT_CHANNELS: UInt16 = 2 // L = system, R = mic

// MARK: - stdout events

func emit(_ dict: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: dict),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

func fail(_ code: String, _ message: String) -> Never {
    emit(["event": "error", "code": code, "message": message])
    exit(1)
}

// MARK: - WAV writer (16kHz stereo s16le, header patched on close)

final class WavWriter {
    private let handle: FileHandle
    private var dataBytes: UInt32 = 0
    private let lock = NSLock()

    init?(path: String) {
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let h = FileHandle(forWritingAtPath: path) else { return nil }
        handle = h
        handle.write(WavWriter.header(dataBytes: 0))
    }

    private static func header(dataBytes: UInt32) -> Data {
        var d = Data()
        let byteRate = UInt32(SAMPLE_RATE) * UInt32(OUT_CHANNELS) * 2
        let blockAlign = UInt16(OUT_CHANNELS * 2)
        d.append(contentsOf: Array("RIFF".utf8))
        d.append(le32(36 + dataBytes))
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8))
        d.append(le32(16))
        d.append(le16(1)) // PCM
        d.append(le16(OUT_CHANNELS))
        d.append(le32(UInt32(SAMPLE_RATE)))
        d.append(le32(byteRate))
        d.append(le16(blockAlign))
        d.append(le16(16)) // bits per sample
        d.append(contentsOf: Array("data".utf8))
        d.append(le32(dataBytes))
        return d
    }

    private static func le16(_ v: UInt16) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
    private static func le32(_ v: UInt32) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }

    // frames: interleaved [sysL, micR] Int16 pairs
    func append(_ interleaved: [Int16]) {
        lock.lock(); defer { lock.unlock() }
        interleaved.withUnsafeBufferPointer { buf in
            let data = Data(buffer: buf)
            handle.write(data)
            dataBytes += UInt32(data.count)
        }
    }

    var framesWritten: Int {
        lock.lock(); defer { lock.unlock() }
        return Int(dataBytes) / Int(OUT_CHANNELS) / 2
    }

    func close() {
        lock.lock(); defer { lock.unlock() }
        handle.seek(toFileOffset: 0)
        handle.write(WavWriter.header(dataBytes: dataBytes))
        handle.closeFile()
    }
}

// MARK: - mic ring buffer (mono Int16 @16kHz)

final class MicBuffer {
    private var samples: [Int16] = []
    private let lock = NSLock()

    func push(_ s: [Int16]) {
        lock.lock(); defer { lock.unlock() }
        samples.append(contentsOf: s)
        // Cap growth if system-audio callbacks stall (e.g. no display): keep
        // at most ~5s of backlog so memory stays bounded.
        let cap = Int(SAMPLE_RATE) * 5
        if samples.count > cap {
            samples.removeFirst(samples.count - cap)
        }
    }

    // Pops exactly n samples, zero-filling if the mic is behind.
    func pop(_ n: Int) -> [Int16] {
        lock.lock(); defer { lock.unlock() }
        if samples.count >= n {
            let out = Array(samples.prefix(n))
            samples.removeFirst(n)
            return out
        }
        var out = samples
        out.append(contentsOf: [Int16](repeating: 0, count: n - out.count))
        samples.removeAll()
        return out
    }
}

// MARK: - mic capture (AVAudioEngine -> 16kHz mono Int16 -> ring buffer)

final class MicCapture {
    private let engine = AVAudioEngine()
    private let buffer: MicBuffer
    private var converter: AVAudioConverter?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                          sampleRate: SAMPLE_RATE,
                                          channels: 1,
                                          interleaved: true)!

    init(buffer: MicBuffer) {
        self.buffer = buffer
    }

    func start() throws {
        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0 else {
            throw NSError(domain: "meetingcap", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "no microphone input available"])
        }
        converter = AVAudioConverter(from: inFormat, to: outFormat)

        input.installTap(onBus: 0, bufferSize: 4096, format: inFormat) { [weak self] pcm, _ in
            guard let self, let conv = self.converter else { return }
            let ratio = SAMPLE_RATE / inFormat.sampleRate
            let outCap = AVAudioFrameCount(Double(pcm.frameLength) * ratio + 32)
            guard let out = AVAudioPCMBuffer(pcmFormat: self.outFormat, frameCapacity: outCap) else { return }
            var fed = false
            var convErr: NSError?
            conv.convert(to: out, error: &convErr) { _, status in
                if fed {
                    status.pointee = .noDataNow
                    return nil
                }
                fed = true
                status.pointee = .haveData
                return pcm
            }
            if convErr != nil { return }
            let n = Int(out.frameLength)
            guard n > 0, let ch = out.int16ChannelData else { return }
            self.buffer.push(Array(UnsafeBufferPointer(start: ch[0], count: n)))
        }
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}

// MARK: - system audio capture (SCStream -> interleave with mic -> WAV)

final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let writer: WavWriter
    private let mic: MicBuffer
    private var stream: SCStream?
    private var converter: AVAudioConverter?
    private var srcFormat: AVAudioFormat?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                          sampleRate: SAMPLE_RATE,
                                          channels: 1,
                                          interleaved: true)!
    private let queue = DispatchQueue(label: "meetingcap.audio")
    var onFirstBuffer: (() -> Void)?
    private var firstBufferSeen = false

    // Retry state for didStopWithError -- see that method below. Bounded so
    // a genuinely broken capture pipeline doesn't spin forever, but high
    // enough to ride out the transient SCStream drops seen in the wild
    // (confirmed via Console: a live capture threw didStopWithError three
    // times over ~20 minutes during one otherwise-uneventful call).
    private var restartAttempts = 0
    private let maxRestartAttempts = 5
    private var isRestarting = false

    init(writer: WavWriter, mic: MicBuffer) {
        self.writer = writer
        self.mic = mic
    }

    func start() async throws {
        // This call is what triggers the Screen Recording TCC prompt/denial.
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw NSError(domain: "meetingcap", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "no display found for capture"])
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = Int(SAMPLE_RATE)
        config.channelCount = 1
        // Video is required by SCStream but we throw the frames away —
        // minimal size + low frame rate keeps overhead negligible.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        // Registering a video output too (even though we never read from
        // it) — without this, SCStream still generates the throwaway 2x2
        // video frames the config above requests, finds no output
        // registered to receive them, and logs "stream output NOT found.
        // Dropping frame" once per frame for the whole capture (confirmed
        // via Console: this line repeated every ~1s, matching
        // minimumFrameInterval, for an entire session). That sustained
        // internal error condition is the leading suspect for the
        // occasional real didStopWithError below.
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try await stream.startCapture()
        self.stream = stream
    }

    func stop() async {
        try? await stream?.stopCapture()
        stream = nil
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }

        guard let pcm = pcmBuffer(from: sampleBuffer) else { return }
        let sys = toInt16Mono(pcm)
        guard !sys.isEmpty else { return }

        if !firstBufferSeen {
            firstBufferSeen = true
            onFirstBuffer?()
        }

        let micSamples = mic.pop(sys.count)
        var interleaved = [Int16](repeating: 0, count: sys.count * 2)
        for i in 0..<sys.count {
            interleaved[i * 2] = sys[i]           // L = system
            interleaved[i * 2 + 1] = micSamples[i] // R = mic
        }
        writer.append(interleaved)
    }

    // Previously called exit(1) here unconditionally -- confirmed via
    // Console that this is what actually fragmented one continuous meeting
    // into several separate recordings: didStopWithError fired mid-call
    // (root cause above), the whole meetingcap process died, Python's
    // crash-auto-relaunch (meeting_recorder.py) brought it back up a few
    // seconds later, and the still-ongoing call got re-detected as a
    // "new" ad-hoc meeting. The mic tap and WAV file are untouched by an
    // SCStream failure, so the fix is to just recreate and restart the
    // SCStream in place -- worst case a few hundred ms of missing system
    // audio, not a whole new recording.
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        guard !isRestarting else { return }
        isRestarting = true
        self.stream = nil
        restartAttempts += 1

        guard restartAttempts <= maxRestartAttempts else {
            emit(["event": "error", "code": "stream_stopped",
                  "message": "system audio capture failed repeatedly, giving up: \(error.localizedDescription)"])
            exit(1)
        }

        emit(["event": "warning", "code": "stream_restarting",
              "message": "system audio stream stopped (\(error.localizedDescription)) -- reconnecting, attempt \(restartAttempts)/\(maxRestartAttempts)"])
        Task {
            try? await Task.sleep(nanoseconds: 500_000_000)  // brief backoff, not a tight retry loop
            do {
                try await self.start()
                self.isRestarting = false
            } catch {
                self.isRestarting = false
                self.stream(stream, didStopWithError: error)
            }
        }
    }

    private func pcmBuffer(from sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let desc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(desc) else { return nil }
        let format = AVAudioFormat(streamDescription: asbdPtr)
        guard let format else { return nil }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frames > 0,
              let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return nil }
        pcm.frameLength = frames
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(frames), into: pcm.mutableAudioBufferList)
        return status == noErr ? pcm : nil
    }

    private func toInt16Mono(_ pcm: AVAudioPCMBuffer) -> [Int16] {
        let inFormat = pcm.format
        // We requested 16kHz/1ch from SCK, so the common path needs only a
        // float->int16 cast; the converter covers any OS that ignores the hint.
        if inFormat.sampleRate == SAMPLE_RATE, inFormat.channelCount == 1,
           inFormat.commonFormat == .pcmFormatFloat32, let ch = pcm.floatChannelData {
            let n = Int(pcm.frameLength)
            var out = [Int16](repeating: 0, count: n)
            for i in 0..<n {
                out[i] = Int16(max(-1.0, min(1.0, ch[0][i])) * 32767.0)
            }
            return out
        }

        if converter == nil || srcFormat != inFormat {
            converter = AVAudioConverter(from: inFormat, to: outFormat)
            srcFormat = inFormat
        }
        guard let conv = converter else { return [] }
        let ratio = SAMPLE_RATE / inFormat.sampleRate
        let outCap = AVAudioFrameCount(Double(pcm.frameLength) * ratio + 32)
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: outCap) else { return [] }
        var fed = false
        var convErr: NSError?
        conv.convert(to: out, error: &convErr) { _, status in
            if fed {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return pcm
        }
        if convErr != nil { return [] }
        let n = Int(out.frameLength)
        guard n > 0, let ch = out.int16ChannelData else { return [] }
        return Array(UnsafeBufferPointer(start: ch[0], count: n))
    }
}

// MARK: - one recording session (created fresh per start, torn down per stop)

final class RecordingSession {
    let writer: WavWriter
    let micBuffer = MicBuffer()
    let micCapture: MicCapture
    let sysCapture: SystemAudioCapture
    let path: String
    let startedAt = Date()

    init?(outputPath: String) {
        guard let w = WavWriter(path: outputPath) else { return nil }
        writer = w
        path = outputPath
        micCapture = MicCapture(buffer: micBuffer)
        sysCapture = SystemAudioCapture(writer: w, mic: micBuffer)
    }

    // Returns nil on success, or an (code, message) error.
    func start() async -> (String, String)? {
        do {
            try micCapture.start()
        } catch {
            return ("mic_error", "microphone capture failed: \(error.localizedDescription) — check Microphone permission in System Settings > Privacy & Security")
        }
        do {
            try await sysCapture.start()
        } catch {
            micCapture.stop()
            let msg = error.localizedDescription
            let code = msg.lowercased().contains("declined") || (error as NSError).domain == "com.apple.ScreenCaptureKit.SCStreamErrorDomain"
                ? "permission_denied" : "capture_error"
            return (code, "system audio capture failed: \(msg) — enable Screen Recording for this app in System Settings > Privacy & Security")
        }
        return nil
    }

    // Returns duration in whole seconds.
    func stop() async -> Int {
        await sysCapture.stop()
        micCapture.stop()
        let frames = writer.framesWritten
        writer.close()
        return frames / Int(SAMPLE_RATE)
    }

    func discard() async -> Void {
        _ = await stop()
        try? FileManager.default.removeItem(atPath: path)
    }
}

// MARK: - consent banner (shown on every recording start, manual or auto --
// system audio capture includes other meeting participants' voices without
// them clicking anything, so a visible signal + an easy way to cancel is
// the consent-conscious default regardless of who triggered it)

final class ConsentBanner {
    private var panel: NSPanel?
    var onDiscard: (() -> Void)?

    func show() {
        DispatchQueue.main.async { [weak self] in
            self?.buildAndShow()
        }
    }

    func hide() {
        DispatchQueue.main.async { [weak self] in
            self?.panel?.close()
            self?.panel = nil
        }
    }

    private func buildAndShow() {
        guard let screen = NSScreen.main else { return }
        let width: CGFloat = 340, height: CGFloat = 56
        let x = screen.frame.midX - width / 2
        let y = screen.frame.maxY - height - 12
        let p = NSPanel(contentRect: NSRect(x: x, y: y, width: width, height: height),
                         styleMask: [.nonactivatingPanel, .fullSizeContentView],
                         backing: .buffered, defer: false)
        p.level = .statusBar
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.collectionBehavior = [.canJoinAllSpaces, .stationary]

        let content = NSVisualEffectView(frame: p.contentRect(forFrameRect: p.frame))
        content.material = .hudWindow
        content.state = .active
        content.wantsLayer = true
        content.layer?.cornerRadius = 12

        let label = NSTextField(labelWithString: "🔴 Clicky is recording this meeting")
        label.frame = NSRect(x: 16, y: 16, width: 220, height: 24)
        label.font = .systemFont(ofSize: 13, weight: .medium)
        content.addSubview(label)

        let button = NSButton(title: "Stop && Discard", target: self, action: #selector(discardTapped))
        button.frame = NSRect(x: width - 130, y: 12, width: 116, height: 30)
        button.bezelStyle = .rounded
        content.addSubview(button)

        p.contentView = content
        p.orderFrontRegardless()
        panel = p

        // Auto-dismiss the banner after 10s -- recording continues; this
        // just stops the visual nag once the user has clearly seen it.
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in
            self?.panel?.close()
            self?.panel = nil
        }
    }

    @objc private func discardTapped() {
        hide()
        onDiscard?()
    }
}

// MARK: - Apple Calendar lookup (EventKit) -- a second, optional calendar
// source alongside Google Calendar (see apple_calendar.py / google_client.py)
// for people who use Calendar.app instead of/alongside Google Calendar.
// Gated by the standard macOS "Calendars" privacy permission, same TCC
// prompt pattern as Screen Recording/Microphone -- no OAuth, no developer
// setup, since this runs entirely on-device.

final class CalendarLookup {
    private let store = EKEventStore()

    // Same meeting-link patterns as google_client.py's MEETING_URL_PATTERNS
    // -- kept as simple substring checks here (not full regex) since the
    // set of known video-call domains is small and fixed.
    private static let meetingURLMarkers = ["meet.google.com", "teams.microsoft.com", "teams.live.com"]

    private func requestAccess() async -> Bool {
        if #available(macOS 14.0, *) {
            return (try? await store.requestFullAccessToEvents()) ?? false
        } else {
            return await withCheckedContinuation { cont in
                store.requestAccess(to: .event) { granted, _ in cont.resume(returning: granted) }
            }
        }
    }

    private func extractMeetingURL(from event: EKEvent) -> String? {
        let haystack = [event.location, event.notes, event.url?.absoluteString]
            .compactMap { $0 }.joined(separator: " ")
        for marker in Self.meetingURLMarkers where haystack.contains(marker) {
            // Pull out just the URL token containing the marker, not the whole haystack.
            if let token = haystack.components(separatedBy: .whitespacesAndNewlines).first(where: { $0.contains(marker) }) {
                return token
            }
        }
        return nil
    }

    private func extractAttendees(from event: EKEvent) -> [[String: String]] {
        guard let participants = event.attendees else { return [] }
        var result: [[String: String]] = []
        for p in participants {
            // EKParticipant doesn't expose a direct email property -- for
            // most account types (iCloud, Exchange, Google-via-CalDAV) the
            // participant's `url` is a "mailto:" URL, which is the only
            // reliable way to recover an address. Participants without one
            // (e.g. resource bookings) are skipped rather than included
            // with a blank email, matching how google_client.py's attendee
            // list is built (email-less entries can't be matched to
            // anything downstream: Notion People, past-meeting context).
            guard p.url.scheme == "mailto" else { continue }
            let email = p.url.absoluteString.replacingOccurrences(of: "mailto:", with: "")
            let name = p.name ?? email
            result.append(["name": name, "email": email])
        }
        return result
    }

    // Returns a dict matching google_client.current_or_next_event()'s shape,
    // or nil if nothing qualifies / access wasn't granted.
    func currentOrNextEvent(windowMin: Int) async -> [String: Any]? {
        guard await requestAccess() else { return nil }

        let now = Date()
        let start = now.addingTimeInterval(-5 * 60)
        let end = now.addingTimeInterval(Double(windowMin) * 60)
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        let events = store.events(matching: predicate).sorted { $0.startDate < $1.startDate }

        let isoFormatter = ISO8601DateFormatter()
        for event in events {
            guard let meetingURL = extractMeetingURL(from: event) else { continue }
            return [
                "title": event.title ?? "Untitled meeting",
                "start": isoFormatter.string(from: event.startDate),
                "end": isoFormatter.string(from: event.endDate),
                "meeting_url": meetingURL,
                "attendees": extractAttendees(from: event),
            ]
        }
        return nil
    }
}

// MARK: - pre-meeting prep popover -- anchored under the menu-bar icon
// (not a Notification Center banner, which truncates long text; this shows
// the full synthesized context) -- see poller.py's check_meeting_prep_once.

final class PrepPopover: NSViewController {
    private let popover = NSPopover()
    private let titleLabel = NSTextField(labelWithString: "")
    private let bodyLabel = NSTextField(wrappingLabelWithString: "")
    // Remembers the most recent note so the menu's "Show Last Prep Note"
    // item can bring it back after the popover's own 30s auto-dismiss --
    // easy to miss it the first time if you're not looking at the screen
    // right when it appears.
    private(set) var lastTitle: String?
    private(set) var lastBody: String?

    override func loadView() {
        let width: CGFloat = 320
        let container = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 10))

        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        titleLabel.frame = NSRect(x: 14, y: 0, width: width - 28, height: 18)
        container.addSubview(titleLabel)

        bodyLabel.font = .systemFont(ofSize: 12)
        bodyLabel.textColor = .secondaryLabelColor
        bodyLabel.frame = NSRect(x: 14, y: 0, width: width - 28, height: 18)
        container.addSubview(bodyLabel)

        view = container
        popover.contentViewController = self
        popover.behavior = .transient  // clicking outside dismisses it, standard menu-bar-popover UX
        popover.contentSize = NSSize(width: width, height: 10)
    }

    func show(title: String, body: String, relativeTo button: NSStatusBarButton) {
        _ = view  // force loadView()
        lastTitle = title
        lastBody = body
        titleLabel.stringValue = title

        // Wrap manually to size the popover -- NSTextField's own auto-layout
        // sizing is unreliable outside a real Auto Layout constraint chain,
        // and this view is built by hand (no xib/storyboard) to keep the
        // whole agent a single-file swiftc build with no Xcode project.
        let width: CGFloat = 320
        bodyLabel.stringValue = body
        let bodyHeight = body.boundingRect(
            with: NSSize(width: width - 28, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: bodyLabel.font as Any]
        ).height + 4

        let titleHeight: CGFloat = 20
        let padding: CGFloat = 14
        let totalHeight = padding + titleHeight + 6 + bodyHeight + padding

        titleLabel.frame = NSRect(x: 14, y: totalHeight - padding - titleHeight, width: width - 28, height: titleHeight)
        bodyLabel.frame = NSRect(x: 14, y: padding, width: width - 28, height: bodyHeight)
        view.frame = NSRect(x: 0, y: 0, width: width, height: totalHeight)
        popover.contentSize = NSSize(width: width, height: totalHeight)

        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)

        // Auto-dismiss after 30s if the user doesn't click elsewhere first --
        // this is informational, not something that needs an explicit ack.
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self] in
            self?.popover.close()
        }
    }
}

// MARK: - Ad-hoc (non-calendar) meeting detection
//
// Calendar-based auto-start (see meeting_recorder.py's
// check_meeting_auto_start_once) only ever fires for a scheduled event with
// a Meet/Teams link. An instantaneous call -- someone just starts a Teams
// call, joins a Zoom, or opens a Meet link with nothing on the calendar --
// has no calendar trigger at all. This detects that case via two different
// mechanisms for two different situations:
//
//   1. A known conferencing app (Teams/Zoom/Slack) is actively doing
//      microphone I/O right now -- that alone means it's on a call, no
//      further check needed.
//   2. A browser is actively doing microphone I/O AND has a real Google
//      Meet call URL open in some tab -- a browser's mic use isn't
//      meeting-specific by itself (could be any site), so both signals
//      are required together.
//
// Per-process mic-input detection uses kAudioHardwarePropertyProcessObjectList
// + kAudioProcessPropertyIsRunningInput -- the same first-party CoreAudio
// mechanism behind macOS's own orange mic-in-use indicator and Control
// Center's mic list (confirmed against Apple's own AudioHardwareProcess
// docs and a working reference implementation, insidegui/AudioCap on
// GitHub). Requires macOS 14.4+ (kAudioProcessPropertyIsRunningInput's
// actual introduction version) -- silently no-ops entirely on older
// systems, same "gate a newer API, degrade gracefully" style already used
// elsewhere in this file (see the #available(macOS 14.0, *) check below).
//
// KNOWN LIMITATION, not fully solved: Google Meet's pre-join lobby (mic
// preview/level meter before you actually click "Join") uses the exact
// same URL as an actual in-call tab, and the lobby's live level meter may
// itself cause the browser to show as doing mic input even before joining
// -- there's no URL-based way to distinguish the two. In practice this
// means a false positive is possible for someone who opens a Meet link but
// doesn't join; accepted as a real, acknowledged gap rather than silently
// pretending it's solved (see this feature's plan notes).
//
// NEVER tested against a real Teams/Zoom/Slack call or real Meet tab --
// this environment has no macOS GUI session or live call to verify
// against. Treat the first real on-device test as debugging, not a
// one-shot "should just work."

let ADHOC_CONFERENCING_APP_BUNDLE_IDS: [String: String] = [
    "com.microsoft.teams2": "Microsoft Teams",
    "com.microsoft.teams": "Microsoft Teams",  // older Teams releases used this bundle id
    "us.zoom.xos": "Zoom",
    "com.tinyspeck.slackmacgap": "Slack",
]

let ADHOC_BROWSER_BUNDLE_IDS: [String: String] = [
    "com.apple.Safari": "Safari",
    "com.google.Chrome": "Google Chrome",
    "com.microsoft.edgemac": "Microsoft Edge",
]

/// True for a real Google Meet call URL (https://meet.google.com/xxx-yyyy-zzz),
/// false for the "start a new meeting" landing page and anything else --
/// see this section's own comment on why this can't also exclude the
/// pre-join lobby (same URL as an in-call tab).
func isRealMeetCallURL(_ urlString: String) -> Bool {
    guard urlString.contains("meet.google.com/") else { return false }
    guard let range = urlString.range(of: "meet.google.com/") else { return false }
    let path = urlString[range.upperBound...].split(separator: "?").first.map(String.init) ?? ""
    if path.isEmpty || path == "new" { return false }
    let parts = path.split(separator: "-")
    return parts.count == 3 && parts.allSatisfy { $0.count >= 3 && $0.allSatisfy { $0.isLetter } }
}

/// Asks a Chromium/Safari-family browser (via AppleScript -- requires
/// Automation permission, see clicky.spec's NSAppleEventsUsageDescription)
/// for every open tab's URL, across all windows. Returns [] on any
/// scripting error (Automation permission not yet granted, browser not
/// actually running despite showing up in the audio-process list, an
/// incognito/private window Automation can't see into by browser design,
/// etc.) -- never treated as fatal, just "nothing found this cycle."
func openTabURLs(browserAppName: String) -> [String] {
    let script = """
    tell application "\(browserAppName)"
        set urlList to {}
        repeat with w in windows
            repeat with t in tabs of w
                set end of urlList to URL of t
            end repeat
        end repeat
        return urlList
    end tell
    """
    guard let appleScript = NSAppleScript(source: script) else { return [] }
    var errorDict: NSDictionary?
    let result = appleScript.executeAndReturnError(&errorDict)
    if errorDict != nil { return [] }
    guard result.numberOfItems > 0 else { return [] }
    var urls: [String] = []
    for i in 1...result.numberOfItems {
        if let s = result.atIndex(i)?.stringValue {
            urls.append(s)
        }
    }
    return urls
}

@available(macOS 14.4, *)
private func audioProcessObjectList() -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize)
    guard status == noErr, dataSize > 0 else { return [] }
    let count = Int(dataSize) / MemoryLayout<AudioObjectID>.size
    var objs = [AudioObjectID](repeating: 0, count: count)
    status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize, &objs)
    guard status == noErr else { return [] }
    return objs
}

@available(macOS 14.4, *)
private func isProcessRunningInput(_ processObject: AudioObjectID) -> Bool {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyIsRunningInput,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value: UInt32 = 0
    var dataSize = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(processObject, &address, 0, nil, &dataSize, &value)
    guard status == noErr else { return false }
    return value != 0
}

@available(macOS 14.4, *)
private func pid(forAudioProcess processObject: AudioObjectID) -> pid_t? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyPID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value: pid_t = 0
    var dataSize = UInt32(MemoryLayout<pid_t>.size)
    let status = AudioObjectGetPropertyData(processObject, &address, 0, nil, &dataSize, &value)
    guard status == noErr else { return nil }
    return value
}

/// Returns the bundle identifiers of every process currently doing
/// microphone input, right now. [] on macOS < 14.4 (feature unavailable)
/// or any CoreAudio error -- never raises/crashes the caller.
@available(macOS 14.4, *)
func bundleIdsCurrentlyUsingMicrophone() -> Set<String> {
    var result: Set<String> = []
    for processObject in audioProcessObjectList() {
        guard isProcessRunningInput(processObject) else { continue }
        guard let processPID = pid(forAudioProcess: processObject) else { continue }
        guard let bundleId = NSRunningApplication(processIdentifier: processPID)?.bundleIdentifier else { continue }
        result.insert(bundleId)
    }
    return result
}

/// One detection pass: returns (appLabel, meetingURLOrNil) for a newly
/// detected ad-hoc meeting, or nil if nothing's active this cycle. Checks
/// known conferencing apps first (cheaper, no AppleScript round-trip
/// needed), then known browsers.
@available(macOS 14.4, *)
func detectAdhocMeeting() -> (app: String, meetingURL: String?)? {
    let micActiveBundleIds = bundleIdsCurrentlyUsingMicrophone()
    guard !micActiveBundleIds.isEmpty else { return nil }

    for bundleId in micActiveBundleIds {
        if let label = ADHOC_CONFERENCING_APP_BUNDLE_IDS[bundleId] {
            return (label, nil)
        }
    }
    for bundleId in micActiveBundleIds {
        guard let browserName = ADHOC_BROWSER_BUNDLE_IDS[bundleId] else { continue }
        let tabURLs = openTabURLs(browserAppName: browserName)
        if let meetURL = tabURLs.first(where: isRealMeetCallURL) {
            return (browserName, meetURL)
        }
    }
    return nil
}

// MARK: - status bar agent (persistent; lives for Clicky's whole lifetime)

@MainActor
final class Agent: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var startMenuItem: NSMenuItem!
    private var lastPrepMenuItem: NSMenuItem!
    private var session: RecordingSession?
    private let banner = ConsentBanner()
    private let prepPopover = PrepPopover()
    private let calendarLookup = CalendarLookup()
    private var heartbeat: DispatchSourceTimer?
    private var adhocDetectionTimer: DispatchSourceTimer?
    private var lastAdhocDetectionKey: String?

    // Auto-stop-on-leave for ad-hoc recordings (the counterpart to auto-start
    // above). A calendar-triggered recording already has a known end time
    // handled Python-side; a manual recording should never auto-stop on its
    // own. An ad-hoc recording has neither, so mic-activity loss for the
    // same app/URL that triggered it is the only signal we have that the
    // user left/ended the call. `pendingAdhocKey` bridges the gap between
    // emitting "adhoc_meeting_detected" and Python's "start" command coming
    // back in response -- captured into `activeAdhocKey` the moment that
    // start actually happens, so only a session that really began via this
    // path gets auto-stopped this way.
    private var pendingAdhocKey: String?
    private var activeAdhocKey: String?
    private var adhocMissCount = 0
    private let adhocMissesBeforeStop = 2  // ~20s of no mic activity from that app/URL

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory) // menu-bar only, no Dock icon, no app switcher entry

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "mic", accessibilityDescription: "Clicky")

        let menu = NSMenu()
        startMenuItem = NSMenuItem(title: "Start Recording", action: #selector(menuToggle), keyEquivalent: "")
        startMenuItem.target = self
        menu.addItem(startMenuItem)
        menu.addItem(NSMenuItem.separator())
        lastPrepMenuItem = NSMenuItem(title: "Show Last Prep Note", action: #selector(showLastPrep), keyEquivalent: "")
        lastPrepMenuItem.target = self
        lastPrepMenuItem.isEnabled = false  // enabled once a prep note has actually been shown
        menu.addItem(lastPrepMenuItem)
        let dashboardItem = NSMenuItem(title: "Open Clicky Dashboard", action: #selector(openDashboard), keyEquivalent: "")
        dashboardItem.target = self
        menu.addItem(dashboardItem)
        menu.addItem(NSMenuItem.separator())
        let quitItem = NSMenuItem(title: "Quit", action: #selector(menuQuit), keyEquivalent: "")
        quitItem.target = self
        menu.addItem(quitItem)
        statusItem.menu = menu

        banner.onDiscard = { [weak self] in
            Task { await self?.performStop(discard: true, source: "user_banner") }
        }

        emit(["event": "ready"])
        startStdinReader()
        startAdhocMeetingDetection()
    }

    // MARK: ad-hoc (non-calendar) meeting detection -- see the free
    // functions above this class for the actual CoreAudio/AppleScript work.

    private func startAdhocMeetingDetection() {
        guard #available(macOS 14.4, *) else { return }  // silently unavailable on older systems
        let t = DispatchSource.makeTimerSource(queue: .global())
        t.schedule(deadline: .now() + 10, repeating: 10)
        t.setEventHandler { [weak self] in
            guard #available(macOS 14.4, *) else { return }
            self?.checkForAdhocMeeting()
        }
        t.resume()
        adhocDetectionTimer = t
    }

    @available(macOS 14.4, *)
    private func checkForAdhocMeeting() {
        guard session == nil else {
            lastAdhocDetectionKey = nil
            checkForAdhocMeetingEnded()
            return
        }
        guard let detected = detectAdhocMeeting() else {
            lastAdhocDetectionKey = nil
            return
        }
        let key = detected.app + "|" + (detected.meetingURL ?? "")
        guard key != lastAdhocDetectionKey else { return }  // already emitted for this exact ongoing call
        lastAdhocDetectionKey = key
        pendingAdhocKey = key
        emit(["event": "adhoc_meeting_detected", "app": detected.app, "meeting_url": detected.meetingURL ?? NSNull()])
    }

    // Counterpart to the start-detection above: while a session that began
    // via ad-hoc detection is active, keep checking whether that same
    // app/URL is still using the microphone. Debounced over a couple of
    // consecutive misses so a brief mute, a momentary CoreAudio hiccup, or
    // one slow poll tick doesn't end the recording out from under the user
    // mid-call.
    @available(macOS 14.4, *)
    private func checkForAdhocMeetingEnded() {
        guard let key = activeAdhocKey else { return }
        if let detected = detectAdhocMeeting(), detected.app + "|" + (detected.meetingURL ?? "") == key {
            adhocMissCount = 0
            return
        }
        adhocMissCount += 1
        guard adhocMissCount >= adhocMissesBeforeStop else { return }
        adhocMissCount = 0
        activeAdhocKey = nil
        Task { await performStop(discard: false, source: "adhoc_ended") }
    }

    @objc private func menuToggle() {
        if session != nil {
            Task { await performStop(discard: false, source: "manual") }
            emit(["event": "user_clicked_stop"])
        } else {
            Task { await performStart(meeting: nil, source: "manual") }
            emit(["event": "user_clicked_start"])
        }
    }

    @objc private func openDashboard() {
        if let url = URL(string: "http://127.0.0.1:8000") {
            NSWorkspace.shared.open(url)
        }
    }

    private func showPrep(title: String, body: String) {
        guard let button = statusItem.button else { return }
        prepPopover.show(title: title, body: body, relativeTo: button)
        lastPrepMenuItem.isEnabled = true
    }

    @objc private func showLastPrep() {
        guard let button = statusItem.button,
              let title = prepPopover.lastTitle, let body = prepPopover.lastBody else { return }
        prepPopover.show(title: title, body: body, relativeTo: button)
    }

    // User clicked Quit in the menu bar -- this is the ONLY place that
    // should tell Python to shut down the whole app (see "user_quit" in
    // the stdin/stdout protocol comment above). Without this distinction,
    // clicking Quit only ever killed the menu-bar agent itself -- the
    // actual Python/uvicorn server (and everything it's doing: polling,
    // recording, Notion/Google syncs) kept running invisibly forever,
    // since there's no Dock icon, no window, and now no menu-bar icon
    // either to reveal it's still alive.
    @objc private func menuQuit() {
        Task {
            if session != nil { _ = await performStop(discard: false, source: "quit") }
            emit(["event": "user_quit"])
            exit(0)
        }
    }

    // The "quit" IPC command, by contrast, is sent by Python's OWN
    // shutdown path (meeting_recorder.shutdown(), e.g. during a graceful
    // dev-mode Ctrl+C) -- Python already knows it's shutting down in that
    // case, so this must NOT also emit "user_quit" and race Python's own
    // shutdown sequence with an abrupt os._exit from the response.
    private func quitFromCommand() {
        Task {
            if session != nil { _ = await performStop(discard: false, source: "quit") }
            exit(0)
        }
    }

    // Fixed filename -- Python (meeting_recorder.py) reads it, hashes the
    // content, and assigns the final timestamped name once the recording
    // is handed into the pipeline, same as the original one-shot CLI did.
    private func outputPath() -> String {
        let dir = ProcessInfo.processInfo.environment["CLICKY_DATA_DIR"]
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".clicky-pipeline")
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        return (dir as NSString).appendingPathComponent("meeting_in_progress.wav")
    }

    @discardableResult
    private func performStart(meeting: [String: Any]?, source: String) async -> Bool {
        guard session == nil else { return false }
        guard let s = RecordingSession(outputPath: outputPath()) else {
            emit(["event": "error", "code": "file_error", "message": "cannot open recording file for writing"])
            return false
        }
        if let err = await s.start() {
            emit(["event": "error", "code": err.0, "message": err.1])
            return false
        }
        session = s
        // Only a session that actually began via the ad-hoc detection path
        // (pendingAdhocKey set moments ago by checkForAdhocMeeting) is
        // eligible for mic-loss auto-stop -- a manual click or a
        // calendar-triggered start leaves this nil.
        activeAdhocKey = pendingAdhocKey
        pendingAdhocKey = nil
        adhocMissCount = 0
        statusItem.button?.image = NSImage(systemSymbolName: "mic.fill", accessibilityDescription: "Recording")
        startMenuItem.title = "Stop Recording"
        banner.show()
        startHeartbeat()
        emit(["event": "recording_started", "source": source])
        return true
    }

    @discardableResult
    private func performStop(discard: Bool, source: String) async -> Bool {
        guard let s = session else { return false }
        session = nil
        activeAdhocKey = nil
        adhocMissCount = 0
        stopHeartbeat()
        banner.hide()
        statusItem.button?.image = NSImage(systemSymbolName: "mic", accessibilityDescription: "Clicky")
        startMenuItem.title = "Start Recording"

        if discard {
            await s.discard()
            emit(["event": "recording_stopped", "path": NSNull(), "duration_sec": 0, "discarded": true])
        } else {
            let dur = await s.stop()
            emit(["event": "recording_stopped", "path": s.path, "duration_sec": dur, "discarded": false])
        }
        return true
    }

    private func startHeartbeat() {
        let startedAt = Date()
        let t = DispatchSource.makeTimerSource(queue: .global())
        t.schedule(deadline: .now() + 5, repeating: 5)
        t.setEventHandler {
            emit(["event": "level", "sec": Int(Date().timeIntervalSince(startedAt))])
        }
        t.resume()
        heartbeat = t
    }

    private func stopHeartbeat() {
        heartbeat?.cancel()
        heartbeat = nil
    }

    // MARK: stdin command loop -- driven by meeting_recorder.py

    private func startStdinReader() {
        let handle = FileHandle.standardInput
        handle.readabilityHandler = { fh in
            let data = fh.availableData
            guard !data.isEmpty else { return }
            let lines = (String(data: data, encoding: .utf8) ?? "").split(separator: "\n").map(String.init)
            DispatchQueue.main.async { [weak self] in
                for line in lines {
                    self?.handleCommand(line)
                }
            }
        }
    }

    private func handleCommand(_ line: String) {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let cmd = obj["cmd"] as? String else { return }
        switch cmd {
        case "start":
            let meeting = obj["meeting"] as? [String: Any]
            Task { await performStart(meeting: meeting, source: "auto") }
        case "stop":
            Task { await performStop(discard: false, source: "command") }
        case "status":
            emit(["event": "status", "recording": session != nil])
        case "show_prep":
            let title = (obj["title"] as? String) ?? "Upcoming meeting"
            let body = (obj["body"] as? String) ?? ""
            showPrep(title: title, body: body)
        case "get_event":
            let reqId = (obj["id"] as? String) ?? ""
            let windowMin = (obj["window_min"] as? Int) ?? 15
            Task {
                let result = await calendarLookup.currentOrNextEvent(windowMin: windowMin)
                if let result {
                    emit(["event": "calendar_event", "id": reqId, "data": result])
                } else {
                    emit(["event": "calendar_event", "id": reqId, "data": NSNull()])
                }
            }
        case "quit":
            quitFromCommand()
        default:
            break
        }
    }
}

// MARK: - main

signal(SIGINT, SIG_IGN) // ignore -- the persistent agent quits via the "quit" stdin command instead
// Without this, if the parent Python process ever dies unexpectedly (crash,
// force-quit, kill -9 -- confirmed via a real crash report from a Python/
// bleak/CoreBluetooth threading bug, unrelated to this agent), the read end
// of this agent's stdout pipe closes. The very next emit() (at most 5s
// later, via the recording heartbeat) then hits the default SIGPIPE
// disposition, which terminates the WHOLE process -- including an
// in-progress recording, abruptly, well before WavWriter.close() ever runs
// to patch in the real data-chunk size. That leaves a mid-recording file on
// disk whose header still claims 0 bytes of audio despite the real PCM data
// being physically present (confirmed live: a 13MB orphaned
// meeting_in_progress.wav with a 0-byte data header, from exactly this
// failure mode) -- silently discarded on the pipeline side since it looks
// empty. Ignoring SIGPIPE means a failed emit() just fails the write
// (Foundation's `print`/`fflush` don't raise Swift errors on that, so this
// needs no additional error handling here) instead of killing the process,
// so recording keeps working right up until the user actually stops it.
signal(SIGPIPE, SIG_IGN)
let app = NSApplication.shared
// NSApplication.delegate is weak -- keep a strong top-level reference so the
// agent isn't deallocated the instant this closure exits.
let agentDelegate = MainActor.assumeIsolated { Agent() }
MainActor.assumeIsolated {
    app.delegate = agentDelegate
}
app.run()
