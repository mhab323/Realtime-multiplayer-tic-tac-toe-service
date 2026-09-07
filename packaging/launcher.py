"""Entry point for the packaged build.

Not part of the assignment. This exists so the service can be double-clicked and
played on a phone over the local network, which is a demo convenience rather
than a feature of the game.

Differs from `uvicorn app.main:app` in exactly two ways: it binds 0.0.0.0 so
other devices on the Wi-Fi can reach it, and it prints the address they should
use, because working that out by hand is the actual obstacle.
"""

from __future__ import annotations

import os
import socket
import sys

DEFAULT_PORT = 8000


def lan_address() -> str | None:
    """Best guess at this machine's address on the local network.

    Opening a UDP socket to a public address sends no packets; it just asks the
    OS which local interface would be used to get there, which is the interface
    a phone on the same Wi-Fi will reach us on. `gethostbyname` is not a
    substitute — it commonly answers 127.0.0.1.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        return None if address.startswith("127.") else address
    except OSError:
        return None  # no route: offline, or no network at all
    finally:
        probe.close()


def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def main() -> int:
    # Python block-buffers stdout when it is not a terminal, so piping or
    # logging the launcher would swallow the banner — which is the only thing
    # this script exists to print.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):  # pragma: no cover - very old or odd stdio
        pass

    port = int(os.environ.get("TTT_PORT", DEFAULT_PORT))
    if not port_is_free(port):
        print(f"\n  Port {port} is already in use.")
        print(f"  Close whatever is using it, or set TTT_PORT to something else.\n")
        input("  Press Enter to close. ")
        return 1

    # Imported after the port check so a failed start is a clear message rather
    # than a stack trace behind a wall of uvicorn logging.
    import uvicorn

    from app.main import DEFAULT_DB, app, segno

    lan = lan_address()
    # ASCII only. A Windows console defaults to cp1252, and a box-drawing
    # character here raises UnicodeEncodeError before the server ever starts —
    # the packaged app dies on launch with a traceback and no server.
    rule = "-" * 52

    print(f"\n  {rule}")
    print("   Realtime Tic-Tac-Toe")
    print(f"  {rule}")
    print(f"   On this computer : http://localhost:{port}")
    if lan:
        print(f"   On your phone    : http://{lan}:{port}   (same Wi-Fi)")
    else:
        print("   On your phone    : unavailable - no network connection found")
    print()
    print(f"   Games are saved in : {DEFAULT_DB}")
    if segno is None:
        print("   QR invites         : off (segno not installed)")
    print("   Stop the server    : Ctrl+C")
    print(f"  {rule}\n")

    if lan:
        print("  Windows may ask to allow this through the firewall. Say yes for")
        print("  private networks, or your phone will not be able to connect.\n")

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
