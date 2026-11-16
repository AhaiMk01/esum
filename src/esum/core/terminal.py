"""Whether a person is at the keyboard. Interactive commands (a key login, `esum keys ui`) refuse
without one, so an agent's tool shell can't start them."""

from __future__ import annotations

import os
import sys


def interactive() -> bool:
    """Whether a person can type here. On Windows `isatty()` is also true for NUL (`< /dev/null`),
    and getpass reads the console itself, so it would wait forever: require a real console there."""
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name != "nt":
        return True
    import ctypes
    import msvcrt
    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (OSError, ValueError):
        return False
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
