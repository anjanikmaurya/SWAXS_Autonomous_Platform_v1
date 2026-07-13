"""
tools/pump_diag.py — verify the Mitos P-Pump ASCII protocol (from slaclab/paws).

Protocol (per paws MitosPPumpController): 57600 baud, 8N1, no handshaking;
newline-terminated ('\\r\\n') text commands. This test only READS status ('s')
and toggles remote-control mode ('A1'/'A0'). It sends NO flow command, so it is
safe.

Usage (Dolomite GUI must be CLOSED):
    python tools/pump_diag.py COM3
"""

from __future__ import annotations

import sys
import time

import serial


def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/pump_diag.py COM3")
        return
    port = sys.argv[1]
    print(f"\nOpening {port} at 57600 8N1 (GUI must be closed)…\n")
    s = serial.Serial(port, 57600, timeout=1,
                      parity=serial.PARITY_NONE, bytesize=serial.EIGHTBITS,
                      xonxoff=0, rtscts=0)
    try:
        time.sleep(0.3)
        # 's' = status; 'A1' = enter remote control; 'A0' = release.
        for cmd in ["s", "s", "A1", "s", "A0"]:
            s.reset_input_buffer()
            s.write(f"{cmd}\r\n".encode("utf-8"))
            time.sleep(0.3)
            resp = s.readline().strip().decode("ascii", "replace")
            print(f"  sent {cmd!r:6} -> {resp!r}")
    finally:
        s.close()
        print(f"\nClosed {port}.")
        print("\nA status reply like '#s0,0,1,...' confirms the protocol — paste it "
              "and I'll port this into the app's driver.\n")


if __name__ == "__main__":
    main()
