"""macOS-only CoreBluetooth L2CAP CoC client -- the Phase 2 fast path for
downloading recordings (see ble_sync.cpp's L2CAP server, PSM must match
exactly). Owns its own CBCentralManager connection end-to-end (GATT writes
AND the L2CAP channel) rather than sharing bleak's connection -- bleak's
macOS backend already wraps CoreBluetooth privately, so there's no way to
hand a bleak-held peripheral over to raw pyobjc code.

ble_device_client.py's download_recording() tries this module first and
falls back to its own bleak/notify() path on ANY failure (ImportError if
pyobjc isn't installed, connection failure, timeout, etc) -- see the
DOWNLOAD_STRATEGY log line there to tell which path was actually used.

This is genuinely novel, high-risk code: pyobjc + CoreBluetooth's L2CAP API
is undocumented/niche, and none of it could be compiled or dry-run without
live hardware -- expect iteration once real testing starts. In particular:
CBCentralManager/CBPeripheral deliver delegate callbacks on a dispatch
queue, but CBL2CAPChannel's stream I/O is old-style NSStream, which only
delivers via an NSRunLoop -- this module runs both a dispatch queue AND a
spinning run loop on the same background thread to bridge the two.

SDK: pip install pyobjc-framework-CoreBluetooth
"""
import ctypes
import ctypes.util
import logging
import queue
import threading
import time

import status

log = logging.getLogger("ble_l2cap_client")

DEVICE_NAME_PREFIX = "EpaperTranscriber"
SERVICE_UUID = "E9A10000-1000-4000-8000-00805F9B34FB"
LIST_CHAR_UUID = "E9A10001-1000-4000-8000-00805F9B34FB"
CONTROL_CHAR_UUID = "E9A10002-1000-4000-8000-00805F9B34FB"

# Must match L2CAP_PSM in ble_sync.cpp exactly -- hardcoded on both ends
# since we control both, no GATT-published-PSM discovery needed.
L2CAP_PSM = 0x0081

CONNECT_TIMEOUT_SECONDS = 20
# Deliberately short (not the ~minutes a large real transfer might need) --
# this is the timeout for the STREAM ITSELF going quiet, not the whole
# transfer. Confirmed live this needs to be short: ble_device_client.py's
# hard outer timeout (L2CAP_HARD_TIMEOUT_SECONDS=60) abandons this module's
# thread rather than killing it, and an abandoned thread only calls
# session.close() -- which disconnects from the device -- once ITS OWN
# wait here gives up. A 300s value here meant the device stayed connected
# to this orphaned session for up to 5 minutes after the outer wrapper had
# already given up and fallen back to bleak/GATT, which made bleak's
# reconnect scan fail outright ("No BLE device matching 'EpaperTranscriber*'
# found") since the device was still busy and not advertising. Keeping this
# comfortably under the outer 60s hard timeout means this module cleans up
# and disconnects on its own, in time for the GATT fallback's rescan to
# actually find the device again.
TRANSFER_TIMEOUT_SECONDS = 30
READ_BUF_SIZE = 8192

_libdispatch = ctypes.CDLL(ctypes.util.find_library("System"))
_libdispatch.dispatch_queue_create.restype = ctypes.c_void_p
_libdispatch.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]


def _make_dispatch_queue(label: str):
    """CBCentralManager needs a real dispatch queue when created off the
    main thread (queue=None only works on the main thread's main queue).
    pyobjc has no direct libdispatch bindings, so this goes through ctypes
    -- a known workaround pattern for pyobjc CoreBluetooth on a background
    thread."""
    return _libdispatch.dispatch_queue_create(label.encode("utf-8"), None)


class _Session:
    """One-shot connect + GATT + L2CAP session. Not persistent like
    ble_device_client's connection -- Phase 2 favors a simple, restartable
    connection per download over added statefulness, since this path is
    new/unproven and a clean reconnect-per-call is easier to reason about
    while it's being shaken out on real hardware."""

    def __init__(self):
        self.central_delegate = None
        self.central_manager = None
        self.peripheral_delegate = None
        self.peripheral = None
        self.run_loop_thread = None
        self._events = queue.Queue()
        self._pending_write = None  # (data, result_queue) handoff to the run-loop thread, see write_command

    def _wait_for(self, kind: str, timeout: float):
        # Step-level timing -- the handshake sequence (poweredOn ->
        # discovered -> connected -> servicesDiscovered ->
        # characteristicsDiscovered) was previously a total black box: this
        # module had exactly one log line in it, deep inside the transfer
        # loop, so a hang/slowness anywhere in connect() was silently
        # indistinguishable from every other step. Confirmed live: the
        # whole attempt ran past a 60s outer timeout with zero output.
        start = time.monotonic()
        deadline = start + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("L2CAP step '%s' timed out after %.1fs", kind, time.monotonic() - start)
                raise TimeoutError(f"timed out waiting for {kind}")
            try:
                event_kind, payload = self._events.get(timeout=remaining)
            except queue.Empty:
                log.warning("L2CAP step '%s' timed out after %.1fs", kind, time.monotonic() - start)
                raise TimeoutError(f"timed out waiting for {kind}")
            if event_kind == kind:
                log.info("L2CAP step '%s' completed in %.1fs", kind, time.monotonic() - start)
                return payload
            if event_kind == "error":
                raise RuntimeError(f"BLE error while waiting for {kind}: {payload}")

    def connect(self):
        # Imported lazily so the whole module is optional -- machines
        # without pyobjc installed (or non-macOS) never hit this import.
        from CoreBluetooth import CBCentralManager, CBUUID
        import objc

        self.central_delegate = _CentralDelegate.alloc().init()
        self.central_delegate.session = self
        dispatch_queue = _make_dispatch_queue("ble-l2cap-central")
        self.central_manager = CBCentralManager.alloc().initWithDelegate_queue_(
            self.central_delegate, objc.objc_object(c_void_p=dispatch_queue)
        )

        # CBCentralManager/CBPeripheral delegate callbacks are delivered via
        # the dispatch queue created above -- GCD services that
        # independently, no run loop needed. The run loop is only started
        # later, in open_l2cap_and_download(), right before the L2CAP
        # channel's NSStream is scheduled on it (NSStream is the one thing
        # here that's old-style run-loop-only, no dispatch-queue option).
        # Starting it here unconditionally was a real, confirmed bug: an
        # NSRunLoop with nothing scheduled on it returns immediately from
        # runMode:beforeDate: instead of actually waiting, turning the
        # "pump" loop into a tight GIL-starving busy-spin (measured at ~99%
        # CPU) for the entire connect/discover phase, which in turn starved
        # every other Python thread -- including the one this method's own
        # _wait_for() timeouts run on -- of scheduling time badly enough to
        # look like a multi-minute hang rather than a fast, clean timeout.

        self._wait_for("poweredOn", CONNECT_TIMEOUT_SECONDS)

        self.central_manager.scanForPeripheralsWithServices_options_(
            [CBUUID.UUIDWithString_(SERVICE_UUID)], None
        )
        peripheral = self._wait_for("discovered", CONNECT_TIMEOUT_SECONDS)
        self.central_manager.stopScan()

        self.peripheral = peripheral
        self.peripheral_delegate = _PeripheralDelegate.alloc().init()
        self.peripheral_delegate.session = self
        self.peripheral.setDelegate_(self.peripheral_delegate)

        self.central_manager.connectPeripheral_options_(peripheral, None)
        self._wait_for("connected", CONNECT_TIMEOUT_SECONDS)

        self.peripheral.discoverServices_([CBUUID.UUIDWithString_(SERVICE_UUID)])
        self._wait_for("servicesDiscovered", CONNECT_TIMEOUT_SECONDS)
        self.peripheral.discoverCharacteristics_forService_(None, self._service)
        self._wait_for("characteristicsDiscovered", CONNECT_TIMEOUT_SECONDS)

    def _run_loop_worker(self, run_loop_ready: threading.Event):
        from Foundation import NSRunLoop, NSDate

        run_loop = NSRunLoop.currentRunLoop()
        # Published so open_l2cap_and_download() (running on a different
        # thread) can schedule the NSStream onto *this* specific run loop
        # instance -- scheduling onto NSRunLoop.currentRunLoop() from the
        # calling thread would silently capture the wrong thread's run loop.
        self._stream_run_loop = run_loop
        run_loop_ready.set()

        # Only reaches here once a real source (the stream, scheduled by
        # the caller right after run_loop_ready is set) exists on the run
        # loop -- runMode:beforeDate: correctly blocks/services once there's
        # something to service. Do NOT start this thread before that point:
        # an NSRunLoop with nothing scheduled returns immediately from
        # runMode:beforeDate: instead of waiting, which turns this into a
        # GIL-starving busy-spin (confirmed live at ~99% CPU, see the
        # comment in connect() for what that actually broke).
        while not getattr(self, "_closed", False):
            run_loop.runMode_beforeDate_("kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.05))
            # NSStream is not documented as thread-safe for concurrent
            # access -- confirmed live that calling write() directly from a
            # different thread than the one pumping this run loop (the
            # asyncio.to_thread worker, in write_command's original form)
            # made the write fail outright (returned -1, immediately
            # followed by a stream error event) even though
            # hasSpaceAvailable() had just reported True. Performing the
            # write here, on the same thread that owns/pumps the stream,
            # is the fix -- write_command() hands off the actual write via
            # this queue instead of calling the stream directly.
            pending = self._pending_write
            if pending is not None:
                self._pending_write = None
                data, out_queue = pending
                try:
                    n = self._output_stream.write_maxLength_(data, len(data))
                    out_queue.put(("ok", n))
                except Exception as e:
                    out_queue.put(("error", e))

    def close(self):
        self._closed = True
        if self.peripheral is not None and self.central_manager is not None:
            try:
                self.central_manager.cancelPeripheralConnection_(self.peripheral)
            except Exception:
                pass

    def write_control(self, command: str):
        from CoreBluetooth import CBCharacteristicWriteWithResponse
        data = command.encode("utf-8")
        self.peripheral.writeValue_forCharacteristic_type_(
            data, self._control_char, CBCharacteristicWriteWithResponse
        )
        self._wait_for("writeComplete", CONNECT_TIMEOUT_SECONDS)

    def read_list(self) -> bytes:
        self.peripheral.readValueForCharacteristic_(self._list_char)
        return self._wait_for("listRead", CONNECT_TIMEOUT_SECONDS)

    def open_l2cap_channel(self):
        # Must happen BEFORE write_command() sends "GET <name>" -- confirmed
        # live this ordering was backwards and silently broke every L2CAP
        # transfer: the firmware decides GATT-vs-L2CAP the instant it
        # receives the GET command (ble_sync.cpp's transferTask checks
        # s_l2capChannel != nullptr right then), so if the channel isn't
        # open yet, it commits to the GATT notify() path -- which nothing
        # here is listening for -- and this stream sits open but silent
        # forever. The channel opening successfully (l2capOpened firing)
        # says nothing about whether the firmware picked it for the actual
        # transfer.
        self.peripheral.openL2CAPChannel_(L2CAP_PSM)
        channel = self._wait_for("l2capOpened", CONNECT_TIMEOUT_SECONDS)

        # NSStream only delivers events via a run loop (no dispatch-queue
        # option), and scheduling has to happen on the same thread that
        # pumps that run loop -- NSRunLoop.currentRunLoop() here would
        # otherwise capture *this* thread (the asyncio.to_thread worker),
        # not the dedicated pump thread started below, and the stream would
        # never actually get serviced. Start the pump thread now (not
        # earlier -- see the comment in connect() about the busy-spin bug
        # from starting it before there's anything to schedule) and wait
        # for it to publish its own run loop before scheduling onto it.
        run_loop_ready = threading.Event()
        self.run_loop_thread = threading.Thread(
            target=self._run_loop_worker, args=(run_loop_ready,), name="ble-l2cap-runloop", daemon=True
        )
        self.run_loop_thread.start()
        if not run_loop_ready.wait(timeout=CONNECT_TIMEOUT_SECONDS):
            raise TimeoutError("L2CAP run loop thread never became ready")

        # CBL2CAPChannel's input/output streams are a matched pair over the
        # same underlying socket -- confirmed live that opening only the
        # input side left the channel connected (l2capOpened fired) but
        # totally silent (zero stream_handleEvent_ calls, ever, no error
        # either -- not a timeout on our end, the stream just never started
        # flowing). The output stream is also how the "GET <name>" command
        # itself gets sent now (see write_command below) -- the original
        # design sent it over the separate GATT CONTROL characteristic
        # instead, which turned out to make the firmware's NimBLE L2CAP CoC
        # implementation disconnect the channel almost immediately (see
        # ble_sync.cpp's L2CAPTransferCallbacks comment).
        input_stream = channel.inputStream()
        input_stream.setDelegate_(self.peripheral_delegate)
        input_stream.scheduleInRunLoop_forMode_(self._stream_run_loop, "kCFRunLoopDefaultMode")
        input_stream.open()

        output_stream = channel.outputStream()
        output_stream.setDelegate_(self.peripheral_delegate)
        output_stream.scheduleInRunLoop_forMode_(self._stream_run_loop, "kCFRunLoopDefaultMode")
        output_stream.open()

        self._input_stream = input_stream
        self._output_stream = output_stream

    def write_command(self, command: str):
        """Sends "GET <name>" / "DELETE <name>" over the L2CAP channel's
        own output stream -- NOT the GATT CONTROL characteristic (see
        open_l2cap_channel's comment for why mixing the two broke the
        firmware's L2CAP implementation). The actual write happens on the
        run-loop thread (see _run_loop_worker's pending-write handling) --
        confirmed live that calling the stream's write() directly from this
        thread failed outright (returned -1, immediate stream error), since
        NSStream isn't safe to touch concurrently from a thread other than
        the one servicing it."""
        data = command.encode("utf-8")
        result_queue = queue.Queue()
        self._pending_write = (data, result_queue)
        kind, payload = result_queue.get(timeout=CONNECT_TIMEOUT_SECONDS)
        if kind == "error":
            raise payload
        n = payload
        if n != len(data):
            raise RuntimeError(f"L2CAP command write incomplete: wrote {n} of {len(data)} bytes")

    def read_transfer(self):
        """Returns (actual_name, wav_bytes). Framing (see ble_sync.cpp's
        transferTask L2CAP branch): 1-byte name length, name's raw UTF-8
        bytes, 4-byte little-endian data length, then the data itself. The
        name is on the wire at all because the firmware -- not this client
        -- picks which pending file to auto-send the instant the channel
        connects (see open_l2cap_channel's comment), so this can't just
        assume it matches whatever name the caller originally asked for."""
        input_stream = self._input_stream
        output_stream = self._output_stream

        buffer = bytearray()
        name_len = None
        actual_name = None
        total_len = None
        deadline = time.monotonic() + TRANSFER_TIMEOUT_SECONDS
        while True:
            try:
                event_kind, payload = self._events.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError("L2CAP transfer timed out")
            if event_kind == "streamBytesAvailable":
                buffer.extend(payload)
                if name_len is None and len(buffer) >= 1:
                    name_len = buffer[0]
                    del buffer[0:1]
                if name_len is not None and actual_name is None and len(buffer) >= name_len:
                    actual_name = buffer[:name_len].decode("utf-8")
                    del buffer[:name_len]
                    log.info("L2CAP download: firmware is sending '%s'", actual_name)
                if actual_name is not None and total_len is None and len(buffer) >= 4:
                    total_len = int.from_bytes(buffer[:4], "little")
                    del buffer[:4]
                    log.info("L2CAP download: expecting %d bytes", total_len)
                    status.update(sync_progress_name=actual_name, sync_progress_bytes=len(buffer), sync_progress_total=total_len)
                elif total_len is not None:
                    status.update(sync_progress_bytes=len(buffer))
                if total_len is not None and len(buffer) >= total_len:
                    break
            elif event_kind == "streamError":
                raise RuntimeError(f"L2CAP stream error: {payload}")
            if time.monotonic() > deadline:
                raise TimeoutError("L2CAP transfer timed out")

        input_stream.close()
        output_stream.close()
        return actual_name, bytes(buffer[:total_len])

    # -- delegate-populated attributes, set as discovery events arrive --
    _service = None
    _list_char = None
    _control_char = None
    download_name = None


def download_recording(name: str):
    """Returns (actual_name, wav_bytes) -- see _Session.read_transfer()'s
    docstring for why actual_name can differ from the requested name (the
    firmware auto-picks and auto-sends the pending file the instant the
    L2CAP channel connects; nothing is ever written into the channel from
    this side, see open_l2cap_channel's comment for why)."""
    session = _Session()
    session.download_name = name
    try:
        session.connect()
        session.open_l2cap_channel()
        return session.read_transfer()
    finally:
        # See ble_device_client.py's identical fix for why close()'s own
        # exceptions must not be allowed to skip clearing progress state.
        try:
            session.close()
        except Exception:
            pass
        status.update(sync_progress_name=None, sync_progress_bytes=None, sync_progress_total=None)


def list_recordings():
    import json
    session = _Session()
    try:
        session.connect()
        raw = session.read_list()
        return json.loads(raw.decode("utf-8"))
    finally:
        session.close()


def delete_recording(name: str):
    session = _Session()
    try:
        session.connect()
        session.write_control(f"DELETE {name}")
    finally:
        session.close()


# --- delegates ---
# Defined at import time only if pyobjc is actually installed -- keeps this
# module importable-but-inert (ImportError deferred to first real call) on
# machines without the optional dependency.
try:
    from Foundation import NSObject
    import objc

    class _CentralDelegate(NSObject):
        session = None

        def centralManagerDidUpdateState_(self, central):
            if central.state() == 5:  # CBManagerStatePoweredOn
                self.session._events.put(("poweredOn", None))

        def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(
            self, central, peripheral, advertisementData, rssi
        ):
            name = peripheral.name()
            if name and name.startswith(DEVICE_NAME_PREFIX):
                self.session._events.put(("discovered", peripheral))

        def centralManager_didConnectPeripheral_(self, central, peripheral):
            self.session._events.put(("connected", peripheral))

        def centralManager_didFailToConnectPeripheral_error_(self, central, peripheral, error):
            self.session._events.put(("error", str(error)))

        def centralManager_didDisconnectPeripheral_error_(self, central, peripheral, error):
            if error is not None:
                self.session._events.put(("error", str(error)))

    class _PeripheralDelegate(NSObject):
        session = None

        def peripheral_didDiscoverServices_(self, peripheral, error):
            if error is not None:
                self.session._events.put(("error", str(error)))
                return
            for service in peripheral.services():
                if str(service.UUID()).upper() == SERVICE_UUID:
                    self.session._service = service
                    self.session._events.put(("servicesDiscovered", None))
                    return

        def peripheral_didDiscoverCharacteristicsForService_error_(self, peripheral, service, error):
            if error is not None:
                self.session._events.put(("error", str(error)))
                return
            for char in service.characteristics():
                uuid = str(char.UUID()).upper()
                if uuid == LIST_CHAR_UUID:
                    self.session._list_char = char
                elif uuid == CONTROL_CHAR_UUID:
                    self.session._control_char = char
            self.session._events.put(("characteristicsDiscovered", None))

        def peripheral_didUpdateValueForCharacteristic_error_(self, peripheral, characteristic, error):
            if error is not None:
                self.session._events.put(("error", str(error)))
                return
            if str(characteristic.UUID()).upper() == LIST_CHAR_UUID:
                self.session._events.put(("listRead", bytes(characteristic.value())))

        def peripheral_didWriteValueForCharacteristic_error_(self, peripheral, characteristic, error):
            if error is not None:
                self.session._events.put(("error", str(error)))
                return
            self.session._events.put(("writeComplete", None))

        def peripheral_didOpenL2CAPChannel_error_(self, peripheral, channel, error):
            if error is not None:
                self.session._events.put(("error", str(error)))
                return
            self.session._events.put(("l2capOpened", channel))

        def stream_handleEvent_(self, stream, event_code):
            # NSStreamEventOpenCompleted=1, HasBytesAvailable=2,
            # HasSpaceAvailable=4, ErrorOccurred=8, EndEncountered=16.
            # Diagnostic only -- if bytes still never arrive after opening
            # both stream halves, this tells us whether the stream is
            # opening/erroring/ending instead of silently doing nothing.
            log.info("L2CAP stream event: code=%d", event_code)
            if event_code == 2:
                # -[NSInputStream read:maxLength:] takes a C out-buffer
                # pointer alongside its NSInteger return value -- pyobjc's
                # bridging convention for that shape is to allocate the
                # buffer itself (pass None) and return (bytesRead, buffer)
                # as a tuple, not just the int. Confirmed live: passing a
                # ctypes buffer and treating the return as a plain int
                # crashed with "'>' not supported between instances of
                # 'tuple' and 'int'" the first time real stream data
                # arrived -- the return actually was a tuple all along.
                n, buf = stream.read_maxLength_(None, READ_BUF_SIZE)
                if n > 0:
                    self.session._events.put(("streamBytesAvailable", bytes(buf[:n])))
            elif event_code == 8:
                self.session._events.put(("streamError", str(stream.streamError())))

except ImportError:
    pass
