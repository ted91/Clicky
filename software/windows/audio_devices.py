"""Windows' current audio endpoints, read straight from the MMDevice API (plain ctypes COM, no extra
package). PortAudio only learns about devices when it is initialised, so while a recording runs it
can't see a headset that was just connected or a default device that just changed -- live_recorder's
device follower polls snapshot() instead and reopens the capture when something changed.

Names are the endpoint friendly names ("Headset (Jabra Evolve2 65)"), the same names PortAudio's
WASAPI host API uses (its loopback devices add " [Loopback]").
Call from a thread that has COM initialised (CoInitializeEx).
"""
import ctypes
from ctypes import POINTER, byref, c_uint, c_ulong, c_void_p, c_wchar_p, HRESULT

_ole32 = ctypes.OleDLL("ole32")

eRender, eCapture = 0, 1
eConsole, eCommunications = 0, 2
DEVICE_STATE_ACTIVE = 1
STGM_READ = 0
VT_LPWSTR = 31
CLSCTX_ALL = 23


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        _ole32.CLSIDFromString(c_wchar_p("{" + s + "}"), byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("val", c_void_p), ("pad", c_void_p)]


CLSID_MMDeviceEnumerator = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
PKEY_Device_FriendlyName = PROPERTYKEY(GUID("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)


def _method(obj, index, *argtypes):
    """Bound COM method `index` of interface pointer `obj` (index counts IUnknown's 3 methods)."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return lambda *args: ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)(vtbl[index])(obj, *args)


def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(obj)


def _name(device) -> str:
    store = c_void_p()
    _method(device, 4, c_uint, POINTER(c_void_p))(STGM_READ, byref(store))      # OpenPropertyStore
    try:
        pv = PROPVARIANT()
        _method(store, 5, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(byref(PKEY_Device_FriendlyName), byref(pv))
        name = ctypes.wstring_at(pv.val) if pv.vt == VT_LPWSTR and pv.val else ""
        _ole32.PropVariantClear(byref(pv))
        return name
    finally:
        _release(store)


def _enumerator():
    enum = c_void_p()
    _ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                            byref(IID_IMMDeviceEnumerator), byref(enum))
    return enum


def _default(enum, flow, role) -> str:
    dev = c_void_p()
    try:
        _method(enum, 4, c_uint, c_uint, POINTER(c_void_p))(flow, role, byref(dev))   # GetDefaultAudioEndpoint
    except OSError:
        return ""                                    # no device of this kind at all
    try:
        return _name(dev)
    finally:
        _release(dev)


def _active(enum, flow) -> list:
    coll = c_void_p()
    _method(enum, 3, c_uint, c_uint, POINTER(c_void_p))(flow, DEVICE_STATE_ACTIVE, byref(coll))  # EnumAudioEndpoints
    try:
        n = c_uint()
        _method(coll, 3, POINTER(c_uint))(byref(n))                                             # GetCount
        names = []
        for i in range(n.value):
            dev = c_void_p()
            _method(coll, 4, c_uint, POINTER(c_void_p))(i, byref(dev))                         # Item
            try:
                names.append(_name(dev))
            finally:
                _release(dev)
        return names
    finally:
        _release(coll)


def snapshot() -> dict:
    """{"mic", "mic_comm", "output", "output_comm": default device names ("" if none),
    "mics", "outputs": names of all active devices}. Raises OSError if Windows can't be asked."""
    enum = _enumerator()
    try:
        return {"mic": _default(enum, eCapture, eConsole), "mic_comm": _default(enum, eCapture, eCommunications),
                "output": _default(enum, eRender, eConsole), "output_comm": _default(enum, eRender, eCommunications),
                "mics": _active(enum, eCapture), "outputs": _active(enum, eRender)}
    finally:
        _release(enum)


if __name__ == "__main__":
    import json
    _ole32.CoInitializeEx(None, 0)
    print(json.dumps(snapshot(), indent=2))
