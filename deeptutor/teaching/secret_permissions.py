"""Cross-platform fail-closed permissions for operator-managed secret files."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def _windows_allowed_sids():
    try:
        import ntsecuritycon
        import win32api
        import win32security
    except ImportError:
        raise RuntimeError("Windows secret ACL support is unavailable") from None
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32security.TOKEN_QUERY,
    )
    current = win32security.GetTokenInformation(
        token,
        win32security.TokenUser,
    )[0]
    system = win32security.ConvertStringSidToSid("S-1-5-18")
    return win32security, ntsecuritycon, current, system


def restrict_secret_file(path: Path) -> None:
    target = Path(path)
    if os.name != "nt":
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return
    win32security, ntsecuritycon, current, system = _windows_allowed_sids()
    dacl = win32security.ACL()
    for sid in (current, system):
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    win32security.SetFileSecurity(
        str(target),
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )


def secret_file_is_restricted(path: Path) -> bool:
    target = Path(path)
    if os.name != "nt":
        return stat.S_IMODE(target.stat().st_mode) & 0o077 == 0
    try:
        win32security, _ntsecuritycon, current, system = _windows_allowed_sids()
        descriptor = win32security.GetFileSecurity(
            str(target),
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            return False
        allowed = {str(current), str(system)}
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            ace_type = ace[0][0]
            sid = ace[2]
            if ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE and str(sid) not in allowed:
                return False
        return dacl.GetAceCount() > 0
    except (OSError, RuntimeError):
        return False
