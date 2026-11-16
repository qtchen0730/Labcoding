"""Resolve the current Windows user's WinINet proxy through WinHTTP."""

import ctypes
from ctypes import wintypes
import os


class ProxyDiscoveryError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Windows proxy discovery failed")


class _UserConfig(ctypes.Structure):
    _fields_ = [
        ("auto_detect", wintypes.BOOL), ("pac_url", ctypes.c_void_p),
        ("proxy", ctypes.c_void_p), ("bypass", ctypes.c_void_p),
    ]


class _AutoOptions(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.DWORD), ("detect_flags", wintypes.DWORD),
        ("pac_url", wintypes.LPCWSTR), ("reserved_ptr", ctypes.c_void_p),
        ("reserved", wintypes.DWORD), ("auto_login", wintypes.BOOL),
    ]


class _ProxyInfo(ctypes.Structure):
    _fields_ = [
        ("access_type", wintypes.DWORD), ("proxy", ctypes.c_void_p), ("bypass", ctypes.c_void_p),
    ]


def _bind(dll, name, result, *args):
    call = getattr(dll, name)
    call.restype, call.argtypes = result, args
    return call


def _read_string(pointer):
    return ctypes.wstring_at(pointer) if pointer else ""


def _resolve_pac(dll, free, origin, config_url, autodetect):
    handle_type = wintypes.HANDLE
    wide = wintypes.LPCWSTR
    dw = wintypes.DWORD
    open_session = _bind(dll, "WinHttpOpen", handle_type, wide, dw, wide, wide, dw)
    close = _bind(dll, "WinHttpCloseHandle", wintypes.BOOL, handle_type)
    get_proxy = _bind(dll, "WinHttpGetProxyForUrl", wintypes.BOOL, handle_type, wide,
                      ctypes.POINTER(_AutoOptions), ctypes.POINTER(_ProxyInfo))
    set_timeouts = _bind(dll, "WinHttpSetTimeouts", wintypes.BOOL, handle_type,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    session = open_session("CCSLab Code Lite", 1, None, None, 0)
    if not session:
        raise ProxyDiscoveryError(ctypes.get_last_error())
    info = _ProxyInfo()
    try:
        if not set_timeouts(session, 2500, 2500, 2500, 2500):
            raise ProxyDiscoveryError(ctypes.get_last_error())
        flags = (1 if autodetect else 0) | (2 if config_url else 0)
        options = _AutoOptions(flags, 3 if autodetect else 0, config_url or None, None, 0, False)
        if not get_proxy(session, origin, ctypes.byref(options), ctypes.byref(info)):
            raise ProxyDiscoveryError(ctypes.get_last_error())
        if info.access_type == 1:
            return "", ""
        return _read_string(info.proxy), _read_string(info.bypass)
    finally:
        for pointer in (info.proxy, info.bypass):
            if pointer:
                free(pointer)
        close(session)


def windows_proxy(origin):
    """Return Windows' configured proxy and bypass strings without disclosing PAC data."""
    if os.name != "nt":
        return "", ""
    dll = ctypes.WinDLL("winhttp.dll", use_last_error=True)
    free = _bind(ctypes.WinDLL("kernel32.dll"), "GlobalFree", ctypes.c_void_p, ctypes.c_void_p)
    get_current = _bind(dll, "WinHttpGetIEProxyConfigForCurrentUser", wintypes.BOOL,
                        ctypes.POINTER(_UserConfig))
    config = _UserConfig()
    try:
        if not get_current(ctypes.byref(config)):
            code = ctypes.get_last_error()
            if code == 2:
                return "", ""
            raise ProxyDiscoveryError(code)
        pac_url = _read_string(config.pac_url)
        manual = _read_string(config.proxy), _read_string(config.bypass)
        if pac_url or config.auto_detect:
            try:
                return _resolve_pac(dll, free, origin, pac_url, bool(config.auto_detect))
            except ProxyDiscoveryError as error:
                # WPAD finding no PAC on an unmanaged network leaves its manual
                # route, if any, in effect.
                if manual[0] or (not pac_url and error.code == 12180):
                    return manual
                raise
        return manual
    finally:
        for pointer in (config.pac_url, config.proxy, config.bypass):
            if pointer:
                free(pointer)
