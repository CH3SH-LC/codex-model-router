"""Observe the installed desktop's lifetime without opening or changing its UI."""
import ctypes
from ctypes import wintypes
import os


class DesktopLifetime:
    def __init__(self):
        self.handles = []
        if os.name != "nt":
            return
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self.api
        api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                        ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                        ("name", wintypes.WCHAR * 260)]
        api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        snapshot = api.CreateToolhelp32Snapshot(2, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            return
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            available = api.Process32FirstW(snapshot, ctypes.byref(entry))
            while available:
                if entry.name.lower() == "chatgpt.exe":
                    handle = api.OpenProcess(0x1000 | 0x100000, False, entry.pid)
                    if handle:
                        buffer = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(buffer))
                        valid = api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length))
                        path = buffer.value.lower()
                        if valid and "\\windowsapps\\openai.codex_" in path and path.endswith("\\app\\chatgpt.exe"):
                            self.handles.append(handle)
                        else:
                            api.CloseHandle(handle)
                available = api.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            api.CloseHandle(snapshot)

    def alive(self):
        return any(self.api.WaitForSingleObject(h, 0) == 258 for h in self.handles)

    def close(self):
        for handle in self.handles:
            self.api.CloseHandle(handle)
        self.handles.clear()
