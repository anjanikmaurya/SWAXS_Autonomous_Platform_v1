"""
Py_P_Pump.py — Dolomite Mitos P-Pump driver (ASCII serial protocol).

Rewritten to match the protocol used by the working slaclab/paws
``MitosPPumpController`` for these exact pumps (verified on the rig):

  Serial: 57600 baud, 8 data bits, 1 stop bit, no parity, no handshaking.
  Commands are ASCII lines terminated with '\\r\\n'; replies are prefixed '#'.

    s          -> status  '#s<Err>,<State>,<Mode>,<Pc>,<Ps>,<Pt>,<Qc>,<Qt>,<Ft>'
    A1 / A0    -> enter / leave REMOTE control      (reply '#A0')
    C          -> clear errors
    F<pl_s>    -> set flow-rate setpoint in picolitres/second
    P<mbar>    -> set target pressure in mbar        (P0 = idle / vent)
    R0         -> tare

  Status fields: Err(0=ok), State(0 IDLE/1 CONTROLLING/2 TARE/3 ERROR/4 LEAK),
  Mode(0 manual/1 remote), Pc chamber mbar, Ps supply mbar, Pt target mbar,
  Qc current flow pl/s, Qt target flow pl/s, Ft flow-sensor/mode field.

  IMPORTANT: while CONTROLLING, a command must be sent at least every 30 s or
  the pump drops out of control mode — the controller polls status frequently
  to keep the link alive.

Flow units: the platform works in microlitres/minute; the pump wants pl/s.
    pl_s  = round(uL_min * 1e6 / 60)
    uL_min = pl_s * 60 / 1e6

NOTE: this is a point-to-point protocol (one pump per serial port), so there is
no bus address — the ``pump_id`` argument is accepted for backwards
compatibility but ignored.
"""

from __future__ import annotations

import time
from threading import Lock

import serial
from serial.tools import list_ports

BAUD = 57600

STATE_NAMES = {0: "IDLE", 1: "CONTROLLING", 2: "TARE", 3: "ERROR", 4: "LEAK TEST"}
ERROR_NAMES = {
    0: "None", 1: "Supply greater than maximum", 2: "Tare timeout",
    3: "Tare supply still connected", 4: "Control start timeout",
    5: "Pressure target too low", 6: "Pressure target too high",
    7: "Leak test supply pressure too low", 8: "Leak test timeout",
}


def ulmin_to_pls(ulmin: float) -> int:
    """Microlitres/minute -> picolitres/second (pump setpoint unit)."""
    return int(round(float(ulmin) * 1.0e6 / 60.0))


def pls_to_ulmin(pls: float) -> float:
    """Picolitres/second -> microlitres/minute."""
    return float(pls) * 60.0 / 1.0e6


def find_address(identifier: str | None = None):
    """Print/return the serial port of a connected pump.

    With ``identifier`` (e.g. "Blacktrace" or "Dolomite") it greps the port
    descriptions; with no match it prints all ports so you can pick one.
    """
    ports = list(list_ports.comports())
    if identifier:
        hits = [p for p in ports if identifier.lower() in
                f"{p.manufacturer} {p.description} {p.product}".lower()]
        if len(hits) == 1:
            print("Device address: {}".format(hits[0].device))
            return hits[0]
        if len(hits) > 1:
            print("Multiple matches — listing all ports:")
    for p in ports:
        print(f"  {p.device}  | serial={p.serial_number} | {p.manufacturer} {p.description}")
    return ports[0] if ports else None


def find_port_by_serial(serial):
    """Return the COM/device path whose USB serial number matches ``serial``,
    else None. Serial numbers are fixed per pump, so matching on them makes the
    config portable across PCs (COM numbers differ machine-to-machine)."""
    if not serial:
        return None
    want = str(serial).strip().lower()
    for p in list_ports.comports():
        sn = str(p.serial_number or "").strip().lower()
        if sn and (sn == want or sn.rstrip("ab") == want or want in sn):
            return p.device
    return None


class P_pump:
    """Driver for one Mitos P-Pump over a serial (COM) port."""

    def __init__(self, address, name="", pump_id=0, verbose=False,
                 enter_remote=True):
        """Open the port at 57600 8N1 and (optionally) enter remote control."""
        self.address = address
        self.name = name or str(address)
        self.pump_id = pump_id          # accepted for compatibility, unused
        self.verbose = verbose
        self._lock = Lock()
        self.ser = serial.Serial(
            address, BAUD, timeout=1,
            parity=serial.PARITY_NONE, bytesize=serial.EIGHTBITS,
            stopbits=serial.STOPBITS_ONE, xonxoff=0, rtscts=0,
        )
        time.sleep(0.2)
        self.remote = False
        if enter_remote:
            self.enter_remote()

    # ── low-level command / response ────────────────────────────────────────
    def _cmd(self, cmd: str) -> str:
        """Send one command line and return the pump's reply (stripped)."""
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write(f"{cmd}\r\n".encode("ascii"))
            return self.ser.readline().strip().decode("ascii", "replace")

    # ── control mode ────────────────────────────────────────────────────────
    def enter_remote(self) -> bool:
        """Put the pump into REMOTE control ('A1'), then clear errors ('C')."""
        resp = self._cmd("A1")
        self._cmd("C")
        self.remote = (resp == "#A0")
        if self.verbose:
            print(f"{self.name}: enter_remote -> {resp!r} (remote={self.remote})")
        return self.remote

    def release(self) -> str:
        """Relinquish remote control ('A0')."""
        self.remote = False
        return self._cmd("A0")

    # ── status ──────────────────────────────────────────────────────────────
    def read_status(self) -> dict:
        """Query 's' and parse '#s<Err>,<State>,<Mode>,<Pc>,<Ps>,<Pt>,<Qc>,<Qt>,<Ft>'."""
        resp = ""
        for _ in range(5):
            resp = self._cmd("s")
            parts = resp.split(",")
            if resp.startswith("#s") and len(parts) >= 8 and "" not in parts[:8]:
                try:
                    qc = float(parts[6])
                    return {
                        "raw": resp,
                        "error_code": int(parts[0][2:]),
                        "state_code": int(parts[1]),
                        "mode": int(parts[2]),
                        "chamber_pressure": float(parts[3]),
                        "supply_pressure": float(parts[4]),
                        "target_pressure": float(parts[5]),
                        "flow_rate_pls": qc,
                        "target_flow_rate_pls": float(parts[7]),
                        "flow_rate_ulmin": pls_to_ulmin(qc),
                        "ft": int(parts[8]) if len(parts) > 8 and parts[8] != "" else None,
                    }
                except ValueError:
                    pass
            time.sleep(0.05)
        raise IOError(f"{self.name}: bad/no status reply: {resp!r}")

    # ── setpoints ───────────────────────────────────────────────────────────
    def set_flow(self, rate, unit="ul/m", hold=None) -> str:
        """Set the flow-rate setpoint. ``rate`` is in µL/min (unit/hold kept for
        call-compatibility with the old driver and ignored)."""
        return self._cmd(f"F{ulmin_to_pls(rate)}")

    def set_flowrate_ulmin(self, ulmin) -> str:
        return self._cmd(f"F{ulmin_to_pls(ulmin)}")

    def set_pressure(self, mbar) -> str:
        return self._cmd(f"P{int(round(float(mbar)))}")

    def set_idle(self) -> str:
        """Idle / vent the chamber (target pressure 0)."""
        return self._cmd("P0")

    def tare(self) -> str:
        """Pressure tare ('R0'). Disconnect the air supply first. Non-interactive."""
        return self._cmd("R0")

    tare_pump = tare       # backwards-compatible alias
    tare_pressure = tare   # explicit name for the pressure tare

    def tare_flow(self) -> str:
        """Flow-sensor tare ('R1'). Ensure no flow through the sensor first.
        (Confirmed on the rig: R1 zeroes the flow-sensor reading.)"""
        return self._cmd("R1")

    # ── convenience readers (compatible with the old driver) ─────────────────
    def get_pressure(self):
        """Return [atmospheric, supply, chamber] mbar (atm not reported → 0)."""
        st = self.read_status()
        return [0.0, st["supply_pressure"], st["chamber_pressure"]]

    def get_flow_ulmin(self) -> float:
        return self.read_status()["flow_rate_ulmin"]

    def get_sensor(self) -> str:
        """Summary of the flow-sensor / state fields (Ft is the sensor+mode field)."""
        st = self.read_status()
        return (f"Ft={st['ft']}, state={STATE_NAMES.get(st['state_code'], '?')}, "
                f"flow={st['flow_rate_ulmin']:.3g} uL/min")

    def close(self):
        try:
            self.release()
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    find_address()
