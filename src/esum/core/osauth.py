"""Ask the operating system whether the user is here (D-79), before `esum keys ui` changes a key.

Windows Hello (PIN, fingerprint or face), macOS LocalAuthentication (Touch ID or the login
password), or on Linux a fingerprint through fprintd. The OS owns the prompt: ESUM gets only yes or
no, never a PIN, password or fingerprint, and there is no ESUM secret to reset. Where none works,
`available()` says why and the page stays read-only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

_WINRT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$null = [Windows.Security.Credentials.UI.UserConsentVerifier, Windows.Security.Credentials.UI, ContentType = WindowsRuntime]
function Await($op, $type) {
  $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $null = $task.Wait(-1); $task.Result }
"""
_WIN_CHECK = _WINRT + r"""
Await ([Windows.Security.Credentials.UI.UserConsentVerifier]::CheckAvailabilityAsync()) `
  ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
"""
_WIN_VERIFY = _WINRT + r"""
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Esum {
  [ComImport, Guid("39E050C3-4E74-441A-8DC0-B81104DF949C"), InterfaceType(ComInterfaceType.InterfaceIsIInspectable)]
  public interface IUserConsentVerifierInterop {
    [PreserveSig] int RequestVerificationForWindowAsync(IntPtr appWindow, [MarshalAs(UnmanagedType.HString)] string message,
                                                        ref Guid riid, out IntPtr operation);
  }
  public static class Hello {
    [DllImport("combase.dll", PreserveSig = true)]
    static extern int RoGetActivationFactory([MarshalAs(UnmanagedType.HString)] string id, ref Guid iid, out IntPtr factory);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    public static object Start(string message) {
      Guid iid = typeof(IUserConsentVerifierInterop).GUID;
      IntPtr factory;
      Marshal.ThrowExceptionForHR(RoGetActivationFactory("Windows.Security.Credentials.UI.UserConsentVerifier", ref iid, out factory));
      var interop = (IUserConsentVerifierInterop)Marshal.GetObjectForIUnknown(factory);
      Guid op = new Guid("FD596FFD-2318-558F-9DBE-D21DF43764A5");   // IAsyncOperation<UserConsentVerificationResult>
      IntPtr operation;
      Marshal.ThrowExceptionForHR(interop.RequestVerificationForWindowAsync(GetForegroundWindow(), message, ref op, out operation));
      return Marshal.GetObjectForIUnknown(operation);
    }
  }
}
'@
# Owned by the foreground window (the browser showing the page), so the prompt opens on top of it;
# a plain RequestVerificationAsync from a console process opens behind it and the click seems to do nothing.
try { $operation = [Esum.Hello]::Start($env:ESUM_AUTH_REASON) }
catch { $operation = [Windows.Security.Credentials.UI.UserConsentVerifier]::RequestVerificationAsync($env:ESUM_AUTH_REASON) }
Await $operation ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
"""


def _powershell(script: str, *, timeout: float, reason: str = "") -> str:
    """Windows PowerShell 5.1, which can load WinRT types (PowerShell 7 can't). The reason goes in
    through the environment, so no text is ever spliced into the script."""
    exe = shutil.which("powershell.exe") or "powershell.exe"
    done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "ESUM_AUTH_REASON": reason})
    return done.stdout.strip()


def _mac_context():
    from LocalAuthentication import LAContext   # pyobjc-framework-LocalAuthentication
    return LAContext.alloc().init()


_MAC_POLICY = 2   # LAPolicyDeviceOwnerAuthentication: Touch ID, else the login password


def _fprintd_ready() -> tuple[bool, str]:
    if not shutil.which("fprintd-verify"):
        return False, "fprintd is not installed"
    try:
        listed = subprocess.run(["fprintd-list", os.environ.get("USER", "")], capture_output=True, text=True,
                                timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, "fprintd did not answer"
    if "finger" not in listed.lower() or "no fingers enrolled" in listed.lower():
        return False, "no fingerprint is enrolled (fprintd-enroll)"
    return True, ""


def available() -> tuple[str | None, str]:
    """(the method's name, "") when the OS can confirm the user, else (None, why not)."""
    try:
        if sys.platform == "win32":
            state = _powershell(_WIN_CHECK, timeout=30)
            if state == "Available":
                return "Windows Hello", ""
            return None, f"Windows Hello is not set up ({state or 'no answer'}); set a PIN in Settings > Accounts"
        if sys.platform == "darwin":
            try:
                context = _mac_context()
            except ImportError:
                return None, "pyobjc-framework-LocalAuthentication is not installed"
            ok, _ = context.canEvaluatePolicy_error_(_MAC_POLICY, None)
            return ("Touch ID or your login password", "") if ok else (None, "this Mac can't confirm the user")
        ok, why = _fprintd_ready()
        return ("your fingerprint (fprintd)", "") if ok else (None, why)
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"the system check failed: {error}"


def verify(reason: str) -> bool:
    """Show the OS prompt and wait for it. True only when the OS confirmed the user."""
    try:
        if sys.platform == "win32":
            return _powershell(_WIN_VERIFY, timeout=180, reason=reason) == "Verified"
        if sys.platform == "darwin":
            done = threading.Event()
            answer = {"ok": False}

            def reply(success, error):
                answer["ok"] = bool(success)
                done.set()

            _mac_context().evaluatePolicy_localizedReason_reply_(_MAC_POLICY, reason, reply)
            return done.wait(180) and answer["ok"]
        ok, _ = _fprintd_ready()
        if not ok:
            return False
        out = subprocess.run(["fprintd-verify"], capture_output=True, text=True, timeout=60).stdout
        return "verify-match" in out
    except (OSError, subprocess.SubprocessError, ImportError):
        return False
