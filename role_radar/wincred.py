"""Windows Credential Manager, for keychain.py on Windows.

Each setting is a generic credential (Control Panel → Credential Manager → Windows Credentials)
named by its target, e.g. "Role Radar/SMTP_PASSWORD", kept for this user on this PC. Its value is
stored as UTF-16 text, as Windows' own tools and Python's keyring store theirs.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import cache
from typing import Any

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # this user's, on this PC, across sign-ins
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
ERROR_NOT_FOUND = 1168


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


@cache
def _api() -> Any:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    return advapi


def _failed(action: str, target: str) -> RuntimeError:
    code = ctypes.get_last_error()  # type: ignore[attr-defined]
    return RuntimeError(f"couldn't {action} {target} in Windows Credential Manager: {ctypes.FormatError(code)}")  # type: ignore[attr-defined]


def read(target: str) -> str | None:
    """A credential's value, or None if there's none by that name."""
    found = ctypes.POINTER(CREDENTIAL)()
    if not _api().CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:  # type: ignore[attr-defined]
            return None
        raise _failed("read", target)
    try:
        credential = found.contents
        return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-16-le")
    finally:
        _api().CredFree(found)


def write(target: str, value: str, user: str = "") -> None:
    """Save a credential, replacing one by the same name."""
    blob = value.encode("utf-16-le")
    if not blob or len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
        raise ValueError(f"{target} must be 1 to {CRED_MAX_CREDENTIAL_BLOB_SIZE // 2} characters")
    data = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=target, Comment="Role Radar alert setting",
                            CredentialBlobSize=len(blob), CredentialBlob=ctypes.cast(data, ctypes.POINTER(ctypes.c_ubyte)),
                            Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=user)
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise _failed("save", target)


def delete(target: str) -> bool:
    """Remove a credential. False if there was none."""
    if _api().CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    if ctypes.get_last_error() == ERROR_NOT_FOUND:  # type: ignore[attr-defined]
        return False
    raise _failed("delete", target)
