"""
Notifications.

Previously this launched ``powershell.exe`` (loading the WinRT toast stack) for
every notification, which was slow and — because it interpolated text into a
PowerShell here-string — a code-injection risk.  It now delegates to the fast,
fully in-process toast in :mod:`ui`.  The public ``show()`` signature is kept so
callers (``main._notify``) are unchanged.

Public API
──────────
    show(title, body, image_path=None, fallback_icon=None)
"""

import ui


def show(
    title: str,
    body: str,
    image_path: str | None = None,
    fallback_icon=None,
) -> None:
    """Show a notification via the in-app toast, with a tray-balloon fallback."""
    try:
        if ui.is_running():
            ui.toast(title, body, image_path=image_path)
            return
    except Exception:
        pass

    # Fallback: native tray balloon (no thumbnail) before the UI root is up.
    if fallback_icon is not None:
        try:
            fallback_icon.notify(body, title)
        except Exception:
            pass
