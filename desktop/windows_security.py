"""Native Windows permissions for application-owned SSH key material."""
import ctypes
import re
from ctypes import wintypes as wt


def libraries():
    return ctypes.WinDLL('advapi32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)


def current_sid():
    adv, kernel = libraries()
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    token, needed = wt.HANDLE(), wt.DWORD()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is PSID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wt.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def protect_path(path, directory=False):
    adv, kernel = libraries()
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD)]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.BOOL)]
    adv.SetNamedSecurityInfoW.argtypes = [wt.LPWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wt.DWORD
    inheritance = 'OICI' if directory else ''
    sddl = 'D:P' + ''.join(f'(A;{inheritance};FA;;;{sid})' for sid in ('SY', 'BA', current_sid()))
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = wt.BOOL(), wt.BOOL()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value or not dacl.value:
            raise ctypes.WinError(ctypes.get_last_error())
        result = adv.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if result:
            raise ctypes.WinError(result)
    finally:
        kernel.LocalFree(descriptor)


def read_dacl(path):
    """Used by native/compiled checks; no secret file contents are read."""
    adv, kernel = libraries()
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.GetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wt.DWORD
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(wt.DWORD)]
    descriptor, text = ctypes.c_void_p(), wt.LPWSTR()
    result = adv.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.LocalFree(descriptor)


def expand_sid(value):
    """Normalize SDDL aliases such as local Administrator LA using Windows."""
    adv, kernel = libraries()
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    sid, text = ctypes.c_void_p(), wt.LPWSTR()
    if not adv.ConvertStringSidToSidW(value, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.LocalFree(sid)


def private_acl_valid(path):
    sddl = read_dacl(path)
    entries = [entry.split(';') for entry in re.findall(r'\(([^()]*)\)', sddl)]
    if not sddl.startswith('D:P') or not entries or any(len(entry) != 6 or entry[0] != 'A' or entry[2] != 'FA' for entry in entries):
        return False
    actual = {expand_sid(entry[5]) for entry in entries}
    return actual == {current_sid(), 'S-1-5-18', 'S-1-5-32-544'}
