#!/usr/bin/env python3
"""USB gamepad -> OmniPad PS5 LAN input bridge.

This is a community bridge, not an official OmniPad component. It reads a
controller through Pygame, converts its controls into the 16-byte input frame
used by the OmniPad LAN testing socket, and sends frames to the PS5 over TCP.

Install:  py -m pip install pygame
Run:      py omnipad_gamepad_bridge.py
"""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import platform
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

try:
    import pygame
except ImportError:  # Allows --sweep and --help to work without Pygame installed.
    pygame = None


# =============================================================================
# USER CONFIGURATION - EDIT THIS SECTION FOR YOUR CONTROLLER / NETWORK
# =============================================================================

# OmniPad PS5 console network details.
PS5_IP = "192.168.1.100"     # Change this to your PS5's LAN IP address.
OMNIPAD_INPUT_PORT = 9045     # OmniPad LAN testing input socket.
OMNIPAD_DASHBOARD_PORT = 8095 # Web dashboard; informational link only.

# Select which controller Pygame should read (0 = first detected controller).
CONTROLLER_INDEX = 0
CONTROLLER_PROFILE_NAME = "Generic / custom Pygame gamepad profile"

# Send rate. 20 Hz (one frame every 50 ms) matches the tested script behaviour.
SEND_RATE_HZ = 20.0
CONNECT_TIMEOUT_SECONDS = 2.0
RETRY_DELAY_SECONDS = 0.20
HEARTBEAT_SECONDS = 5.0
PS_BUTTON_PULSE_SECONDS = 0.15

# IMPORTANT: Button indices are Pygame's button numbers, not PS5 button numbers.
# Change the values on the right to match the indices printed by --monitor.
# Set an action to None when your controller does not have that physical input.
# You can also map multiple physical buttons to one action with a list, e.g. [5, 9].
#
# Current known-working DragonRise profile used during development:
#   0 Cross, 1 Circle, 2 Square, 3 Triangle, 4 L1, 5 R1,
#   6 Create/Select (mapped to R2), 7 Start/Options (mapped to PS),
#   8 L3, 9 R3.
BUTTON_MAP: dict[str, Any] = {
    "triangle": 0,  # Physical button 1
    "circle": 1,    # Physical button 2
    "cross": 2,     # Physical button 3
    "square": 3,    # Physical button 4
    "l1": 4,
    "r1": 5,
    "l2": None,
    "r2": None,
    "l3": 10,       # Physical L3
    "r3": 11,       # Physical R3
    "options": 8,   # Select → Options
    "create": None,
    "share": None,
    "touchpad": None,
    "ps": 9,        # Start → PS button
}

# Pygame axis indices. Set an axis to None if it is unavailable.
# Common layout used by the tested controller: left 0/1, right 2/3,
# L2 axis 4, R2 axis 5. Other controllers may use a different layout.
AXIS_MAP: dict[str, int | None] = {
    "left_x": 0,
    "left_y": 1,
    "right_x": 2,
    "right_y": 3,
    "l2": 4,
    "r2": 5,
}

# Stick calibration. Invert an axis if its direction is reversed on your pad.
STICK_DEADZONE = 0.08
INVERT_AXES: dict[str, bool] = {
    "left_x": False,
    "left_y": False,
    "right_x": False,
    "right_y": False,
}

# Trigger calibration. Most Pygame trigger axes are either -1..+1 or 0..+1.
# For the tested gamepad, released = -1 and fully pressed = +1.
TRIGGER_AXIS_MIN = -1.0
TRIGGER_AXIS_MAX = 1.0

# D-pad configuration. Use a hat (usual Pygame representation) or map its
# directions to button indices. Set DPAD_HAT_INDEX=None for button-based D-pad.
DPAD_HAT_INDEX: int | None = 0
DPAD_BUTTON_MAP: dict[str, int | None] = {
    "up": None,
    "right": None,
    "down": None,
    "left": None,
}

# Network/logging behaviour.
# Keep "per_frame" for compatibility with the working development script:
# it opens a TCP connection, sends one 16-byte frame, then closes it.
# Do not change this unless you have tested the alternate transport behaviour.
TCP_CONNECTION_MODE = "per_frame"
LOG_FILE_PATH = "logs/omnipad_gamepad_bridge.log"
LOG_FILE_MAX_BYTES = 2 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 3

# Diagnostic sweep timings. The sweep can activate buttons on the PS5, so use
# it only when you are watching the console and are ready for each test input.
SWEEP_PRESS_SECONDS = 1.5
SWEEP_RELEASE_SECONDS = 0.7

# =============================================================================
# OMNIPAD OUTPUT PROTOCOL - LEAVE AS-IS UNLESS OMNIPAD CHANGES ITS PROTOCOL
# =============================================================================

FRAME_FORMAT = "<I6B6x"
FRAME_LENGTH_BYTES = struct.calcsize(FRAME_FORMAT)  # 16 bytes

BUTTON_MASKS: dict[str, int] = {
    "create": 1 << 0,
    "l3": 1 << 1,
    "r3": 1 << 2,
    "options": 1 << 3,
    "dpad_up": 1 << 4,
    "dpad_right": 1 << 5,
    "dpad_down": 1 << 6,
    "dpad_left": 1 << 7,
    "l2": 1 << 8,
    "r2": 1 << 9,
    "l1": 1 << 10,
    "r1": 1 << 11,
    "triangle": 1 << 12,
    "circle": 1 << 13,
    "cross": 1 << 14,
    "square": 1 << 15,
    "ps": 1 << 16,
    "share": 1 << 0,
    "touchpad": 1 << 20,
}

MASK_LABELS: dict[int, str] = {
    0: "Create / Share (provisional)",
    1: "L3",
    2: "R3",
    3: "Options",
    4: "D-pad Up",
    5: "D-pad Right",
    6: "D-pad Down",
    7: "D-pad Left",
    8: "L2 digital",
    9: "R2 digital",
    10: "L1",
    11: "R1",
    12: "Triangle",
    13: "Circle",
    14: "Cross",
    15: "Square",
    16: "PS button",
    20: "Touchpad click",
}

# Pygame action names -> human-friendly names for logging.
ACTION_LABELS = {
    "cross": "Cross / X",
    "circle": "Circle",
    "square": "Square",
    "triangle": "Triangle",
    "l1": "L1",
    "r1": "R1",
    "l2": "L2",
    "r2": "R2",
    "l3": "L3 / left-stick click",
    "r3": "R3 / right-stick click",
    "options": "Options",
    "create": "Create",
    "share": "Share",
    "touchpad": "Touchpad click",
    "ps": "PS button pulse",
}

NEUTRAL_AXES = (128, 128, 128, 128, 0, 0)


# =============================================================================
# LOGGING AND FORMATTING
# =============================================================================

LOGGER = logging.getLogger("omnipad_gamepad_bridge")


def setup_logging(log_file: str | None) -> None:
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.handlers.clear()
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
        LOGGER.info("Log file: %s", path.resolve())


def action_indices(value: Any) -> list[int]:
    """Allow a mapping value to be None, one index, or a list of indices."""
    if value is None:
        return []
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, (list, tuple)):
        return [int(item) for item in value if item is not None]
    raise ValueError(f"Invalid button mapping value: {value!r}")


def build_index_to_actions() -> dict[int, list[str]]:
    reverse: dict[int, list[str]] = {}
    for action, configured_indices in BUTTON_MAP.items():
        for index in action_indices(configured_indices):
            reverse.setdefault(index, []).append(action)
    for direction, index in DPAD_BUTTON_MAP.items():
        if index is not None:
            reverse.setdefault(index, []).append(f"dpad_{direction}")
    return reverse


def describe_mask(mask: int) -> list[str]:
    labels = [
        f"{MASK_LABELS.get(bit, 'Unknown / unverified')} (bit {bit})"
        for bit in range(32)
        if mask & (1 << bit)
    ]
    return labels or ["No buttons pressed"]


def byte_hex(data: bytes) -> str:
    return " ".join(f"{value:02X}" for value in data)


# =============================================================================
# INPUT CONVERSION
# =============================================================================


def clamp_byte(value: float) -> int:
    return max(0, min(255, int(round(value))))


def stick_to_byte(value: float, deadzone: float, invert: bool = False) -> int:
    if invert:
        value = -value
    if abs(value) < deadzone:
        return 128
    return clamp_byte((value + 1.0) * 127.5)


def trigger_to_byte(value: float) -> int:
    minimum = TRIGGER_AXIS_MIN
    maximum = TRIGGER_AXIS_MAX
    if maximum <= minimum:
        raise ValueError("TRIGGER_AXIS_MAX must be greater than TRIGGER_AXIS_MIN")
    normalized = (value - minimum) / (maximum - minimum)
    return clamp_byte(normalized * 255.0)


def get_raw_axis(pad: Any, action: str) -> float | None:
    axis_index = AXIS_MAP.get(action)
    if axis_index is None:
        return None
    if axis_index < 0 or axis_index >= pad.get_numaxes():
        return None
    return float(pad.get_axis(axis_index))


def get_hat(pad: Any) -> tuple[int, int] | None:
    if DPAD_HAT_INDEX is None:
        return None
    if DPAD_HAT_INDEX < 0 or DPAD_HAT_INDEX >= pad.get_numhats():
        return None
    return pad.get_hat(DPAD_HAT_INDEX)


def read_controller(pad: Any, index_to_actions: dict[int, list[str]]) -> dict[str, Any]:
    """Read one controller state and translate it into OmniPad fields."""
    pressed = [
        index
        for index in range(pad.get_numbuttons())
        if pad.get_button(index)
    ]
    pressed_set = set(pressed)
    active_actions: set[str] = set()

    for button_index in pressed:
        active_actions.update(index_to_actions.get(button_index, []))

    buttons = 0
    for action in active_actions:
        if action == "ps" or action.startswith("dpad_"):
            continue
        buttons |= BUTTON_MASKS.get(action, 0)

    hat = get_hat(pad)
    dpad_x, dpad_y = hat if hat is not None else (0, 0)

    if dpad_y > 0:
        buttons |= BUTTON_MASKS["dpad_up"]
        active_actions.add("dpad_up")
    elif dpad_y < 0:
        buttons |= BUTTON_MASKS["dpad_down"]
        active_actions.add("dpad_down")

    if dpad_x < 0:
        buttons |= BUTTON_MASKS["dpad_left"]
        active_actions.add("dpad_left")
    elif dpad_x > 0:
        buttons |= BUTTON_MASKS["dpad_right"]
        active_actions.add("dpad_right")

    for direction in ("up", "right", "down", "left"):
        action = f"dpad_{direction}"
        if action in active_actions:
            buttons |= BUTTON_MASKS[action]

    raw_axes = {
        action: get_raw_axis(pad, action)
        for action in ("left_x", "left_y", "right_x", "right_y", "l2", "r2")
    }

    axis_bytes = [128, 128, 128, 128, 0, 0]
    stick_actions = ("left_x", "left_y", "right_x", "right_y")
    for output_index, action in enumerate(stick_actions):
        raw_value = raw_axes[action]
        if raw_value is not None:
            axis_bytes[output_index] = stick_to_byte(
                raw_value,
                STICK_DEADZONE,
                INVERT_AXES.get(action, False),
            )

    for output_index, action in ((4, "l2"), (5, "r2")):
        raw_value = raw_axes[action]
        if raw_value is not None:
            axis_bytes[output_index] = trigger_to_byte(raw_value)

    # A mapped digital L2/R2 button also forces its analog trigger depth to 255.
    # This is used by the current profile for Create/Select -> R2.
    if "l2" in active_actions:
        axis_bytes[4] = 255
    if "r2" in active_actions:
        axis_bytes[5] = 255

    ps_requested = "ps" in active_actions

    # Report which Pygame buttons were pressed, including unknown/unmapped ones.
    input_descriptions: list[str] = []
    for button_index in pressed:
        actions = index_to_actions.get(button_index, [])
        if actions:
            labels = [ACTION_LABELS.get(a, a.replace("_", " ").title()) for a in actions]
            input_descriptions.append(f"Button {button_index}: {' + '.join(labels)}")
        else:
            input_descriptions.append(f"Button {button_index}: unmapped")

    if hat is not None and hat != (0, 0):
        input_descriptions.append(f"Hat {DPAD_HAT_INDEX}: {hat}")
    for direction in ("up", "right", "down", "left"):
        action = f"dpad_{direction}"
        if action in active_actions and DPAD_BUTTON_MAP.get(direction) is not None:
            input_descriptions.append(f"D-pad {direction.upper()} (button)")

    return {
        "buttons": buttons,
        "axes": tuple(axis_bytes),
        "pressed": pressed,
        "pressed_set": pressed_set,
        "hat": hat,
        "ps_requested": ps_requested,
        "raw_axes": raw_axes,
        "active_actions": sorted(active_actions),
        "input_descriptions": input_descriptions or ["No buttons pressed"],
    }


def pack_frame(buttons: int, axes: tuple[int, int, int, int, int, int]) -> bytes:
    if len(axes) != 6:
        raise ValueError(f"Expected 6 axis values, received {len(axes)}")
    return struct.pack(FRAME_FORMAT, buttons, *axes)


# =============================================================================
# NETWORK TRANSPORT
# =============================================================================

class OmniPadLink:
    """Sends one 16-byte frame per TCP connection (known-working mode)."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connection_attempts = 0
        self.frames_sent = 0
        self.failures = 0
        self.has_connected = False
        self.recovering = False

    def send(self, frame: bytes) -> None:
        if len(frame) != FRAME_LENGTH_BYTES:
            raise ValueError(f"Invalid frame length: {len(frame)} bytes")

        self.connection_attempts += 1
        try:
            with socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout,
            ) as connection:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.sendall(frame)

            self.frames_sent += 1
            if not self.has_connected or self.recovering:
                LOGGER.info(
                    "[TCP CONNECTED] PS5=%s:%d | mode=%s | frame=%d bytes",
                    self.host,
                    self.port,
                    TCP_CONNECTION_MODE,
                    len(frame),
                )
            self.has_connected = True
            self.recovering = False
        except OSError:
            self.failures += 1
            self.recovering = True
            raise


# =============================================================================
# CONTROLLER DISCOVERY / VALIDATION
# =============================================================================


def require_pygame() -> None:
    if pygame is None:
        raise SystemExit(
            "Pygame is not installed. Install it with: py -m pip install pygame"
        )


def initialize_pygame() -> None:
    require_pygame()
    pygame.init()
    pygame.joystick.init()


def list_devices() -> None:
    initialize_pygame()
    try:
        count = pygame.joystick.get_count()
        print(f"\nDetected gamepads: {count}")
        if count == 0:
            print("No gamepads detected. Connect a controller and try again.")
            return
        for device_index in range(count):
            device = pygame.joystick.Joystick(device_index)
            device.init()
            print(
                f"[{device_index}] {device.get_name()} | "
                f"axes={device.get_numaxes()} | buttons={device.get_numbuttons()} | "
                f"hats={device.get_numhats()}"
            )
    finally:
        pygame.quit()


def validate_profile(pad: Any) -> None:
    LOGGER.info("[CONTROLLER] Name: %s", pad.get_name())
    LOGGER.info(
        "[CONTROLLER] Pygame index=%d | axes=%d | buttons=%d | hats=%d",
        pad.get_id(),
        pad.get_numaxes(),
        pad.get_numbuttons(),
        pad.get_numhats(),
    )
    LOGGER.info("[PROFILE] %s", CONTROLLER_PROFILE_NAME)

    for action, configured_index in BUTTON_MAP.items():
        for index in action_indices(configured_index):
            if index < 0 or index >= pad.get_numbuttons():
                LOGGER.warning(
                    "[PROFILE WARNING] %s maps to button %d, but this controller "
                    "has buttons 0..%d.",
                    action,
                    index,
                    pad.get_numbuttons() - 1,
                )

    if DPAD_HAT_INDEX is not None and DPAD_HAT_INDEX >= pad.get_numhats():
        LOGGER.warning(
            "[PROFILE WARNING] D-pad hat index %d is configured, but this pad "
            "reports only %d hat(s). Set DPAD_HAT_INDEX=None and map "
            "DPAD_BUTTON_MAP if your D-pad uses buttons.",
            DPAD_HAT_INDEX,
            pad.get_numhats(),
        )

    for action, axis_index in AXIS_MAP.items():
        if axis_index is not None and (axis_index < 0 or axis_index >= pad.get_numaxes()):
            LOGGER.warning(
                "[PROFILE WARNING] %s maps to axis %d, but this controller "
                "reports axes 0..%d. Set this AXIS_MAP entry to the correct index "
                "or None.",
                action,
                axis_index,
                pad.get_numaxes() - 1,
            )

    LOGGER.info("[PROFILE] Button mapping:")
    for action, configured_index in BUTTON_MAP.items():
        LOGGER.info("    %-10s <- %s", action, configured_index)
    LOGGER.info("[PROFILE] Axis mapping: %s", AXIS_MAP)
    LOGGER.info("[PROFILE] D-pad: hat=%s | button mapping=%s", DPAD_HAT_INDEX, DPAD_BUTTON_MAP)
    LOGGER.info(
        "[CALIBRATION] stick deadzone=%.3f | trigger range=%.2f..%.2f | invert=%s",
        STICK_DEADZONE,
        TRIGGER_AXIS_MIN,
        TRIGGER_AXIS_MAX,
        INVERT_AXES,
    )


# =============================================================================
# BRIDGE LOOP AND LIVE MONITOR
# =============================================================================


def format_raw_axes(raw_axes: dict[str, float | None]) -> str:
    return " | ".join(
        f"{name}={value:+.3f}" if value is not None else f"{name}=N/A"
        for name, value in raw_axes.items()
    )


def log_controller_state(state: dict[str, Any], buttons: int, frame: bytes) -> None:
    LOGGER.info("[USB INPUT] %s", "; ".join(state["input_descriptions"]))
    LOGGER.info(
        "[USB RAW] pressed_indices=%s | hat=%s | %s",
        state["pressed"],
        state["hat"],
        format_raw_axes(state["raw_axes"]),
    )
    LOGGER.info(
        "[OMNIPAD BUTTONS] mask=0x%08X | %s",
        buttons,
        ", ".join(describe_mask(buttons)),
    )
    LOGGER.info("[OMNIPAD AXES] LX,LY,RX,RY,L2,R2=%s", state["axes"])
    LOGGER.info("[OMNIPAD FRAME] length=%d | hex=%s", len(frame), byte_hex(frame))


def run_input_loop(
    host: str,
    port: int,
    device_index: int,
    monitor_only: bool,
) -> None:
    initialize_pygame()
    try:
        device_count = pygame.joystick.get_count()
        if device_count == 0:
            raise SystemExit(
                "No controller detected. Connect a USB gamepad, then run "
                "--list-devices to check that Windows/Pygame can see it."
            )
        if device_index < 0 or device_index >= device_count:
            raise SystemExit(
                f"Controller index {device_index} is invalid. "
                f"Detected {device_count} controller(s); run --list-devices."
            )

        pad = pygame.joystick.Joystick(device_index)
        pad.init()
        validate_profile(pad)

        rate = max(1.0, float(SEND_RATE_HZ))
        send_interval = 1.0 / rate
        index_to_actions = build_index_to_actions()
        link = None if monitor_only else OmniPadLink(host, port, CONNECT_TIMEOUT_SECONDS)

        LOGGER.info("=" * 78)
        LOGGER.info("USB GAMEPAD -> OMNIPAD PS5 LAN BRIDGE")
        LOGGER.info("Mode: %s", "INPUT MONITOR ONLY (no network frames)" if monitor_only else "LIVE BRIDGE")
        LOGGER.info("Target: %s:%d (TCP)", host, port)
        LOGGER.info("Dashboard: http://%s:%d/", host, OMNIPAD_DASHBOARD_PORT)
        LOGGER.info("Frame: %d bytes | format=%s | send rate=%.1f Hz", FRAME_LENGTH_BYTES, FRAME_FORMAT, rate)
        LOGGER.info("PS action: mapped button -> bit 16 for %.2f seconds", PS_BUTTON_PULSE_SECONDS)
        LOGGER.info("Stop safely with Ctrl+C")
        LOGGER.info("=" * 78)

        last_state_key = None
        last_ps_pressed = False
        ps_until = 0.0
        started_at = time.monotonic()
        last_heartbeat = started_at
        last_send_at = started_at
        iterations = 0
        consecutive_errors = 0

        try:
            while True:
                pygame.event.pump()
                state = read_controller(pad, index_to_actions)
                now = time.monotonic()
                buttons = state["buttons"]

                if state["ps_requested"] and not last_ps_pressed:
                    ps_until = now + PS_BUTTON_PULSE_SECONDS
                    LOGGER.info(
                        "[PS ACTION] PS button requested; setting mask bit 16 "
                        "for %.2f seconds.",
                        PS_BUTTON_PULSE_SECONDS,
                    )
                last_ps_pressed = state["ps_requested"]

                if now < ps_until:
                    buttons |= BUTTON_MASKS["ps"]

                frame = pack_frame(buttons, state["axes"])
                raw_signature = tuple(
                    None if value is None else round(value, 3)
                    for value in state["raw_axes"].values()
                )
                state_key = (
                    tuple(state["pressed"]),
                    state["hat"],
                    tuple(state["active_actions"]),
                    buttons,
                    state["axes"],
                    raw_signature,
                )
                if state_key != last_state_key:
                    log_controller_state(state, buttons, frame)
                    last_state_key = state_key

                if link is not None:
                    try:
                        link.send(frame)
                        consecutive_errors = 0
                    except OSError as exc:
                        consecutive_errors += 1
                        if consecutive_errors == 1 or consecutive_errors % 20 == 0:
                            LOGGER.error(
                                "[TCP ERROR] %s:%d | %r | consecutive failures=%d. "
                                "Check PS5 IP, same-LAN connectivity, OmniPad status, "
                                "and port %d.",
                                host,
                                port,
                                exc,
                                consecutive_errors,
                                port,
                            )
                        time.sleep(RETRY_DELAY_SECONDS)

                iterations += 1
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    elapsed = max(0.001, now - started_at)
                    if link is None:
                        LOGGER.info(
                            "[HEARTBEAT] monitor_iterations=%d | elapsed=%.1fs",
                            iterations,
                            elapsed,
                        )
                    else:
                        LOGGER.info(
                            "[HEARTBEAT] frames_sent=%d | tcp_connect_attempts=%d | "
                            "tcp_failures=%d | avg_rate=%.1f frames/s | elapsed=%.1fs",
                            link.frames_sent,
                            link.connection_attempts,
                            link.failures,
                            link.frames_sent / elapsed,
                            elapsed,
                        )
                    last_heartbeat = now

                # Maintain a stable polling/sending cadence where possible.
                last_send_at += send_interval
                sleep_for = last_send_at - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    last_send_at = time.monotonic()

        except KeyboardInterrupt:
            LOGGER.info("[STOP] Ctrl+C received; stopping bridge.")
        finally:
            if link is not None and link.has_connected:
                try:
                    neutral_frame = pack_frame(0, NEUTRAL_AXES)
                    link.send(neutral_frame)
                    LOGGER.info(
                        "[SHUTDOWN] Sent neutral/release frame to release held buttons. "
                        "hex=%s",
                        byte_hex(neutral_frame),
                    )
                except OSError as exc:
                    LOGGER.warning("[SHUTDOWN] Could not send neutral frame: %r", exc)
            if link is not None:
                LOGGER.info(
                    "[SUMMARY] frames_sent=%d | tcp_connect_attempts=%d | tcp_failures=%d",
                    link.frames_sent,
                    link.connection_attempts,
                    link.failures,
                )
    finally:
        pygame.quit()
        LOGGER.info("[EXIT] Pygame resources released.")


# =============================================================================
# OMNIPAD BUTTON-MASK SWEEP
# =============================================================================


def run_button_sweep(host: str, port: int) -> None:
    LOGGER.warning(
        "The sweep will send individual button-mask bits to the PS5. "
        "Watch the console; unknown bits may trigger unexpected actions."
    )
    LOGGER.info("[SWEEP] Target=%s:%d | interval=%.2fs", host, port, 1.0 / SEND_RATE_HZ)
    LOGGER.info("[SWEEP] Each bit is held for %.2fs, then released for %.2fs.", SWEEP_PRESS_SECONDS, SWEEP_RELEASE_SECONDS)

    link = OmniPadLink(host, port, CONNECT_TIMEOUT_SECONDS)
    interval = 1.0 / max(1.0, SEND_RATE_HZ)

    def send_for(frame: bytes, duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            link.send(frame)
            time.sleep(interval)

    try:
        for bit in range(32):
            mask = 1 << bit
            label = MASK_LABELS.get(bit, "Unknown / unverified")
            frame = pack_frame(mask, NEUTRAL_AXES)
            LOGGER.info(
                "[SWEEP] Testing bit=%d | mask=0x%08X | expected=%s | frame=%s",
                bit,
                mask,
                label,
                byte_hex(frame),
            )
            send_for(frame, SWEEP_PRESS_SECONDS)
            send_for(pack_frame(0, NEUTRAL_AXES), SWEEP_RELEASE_SECONDS)
        LOGGER.info("[SWEEP] Complete. All 32 mask bits have been sent individually.")
    except KeyboardInterrupt:
        LOGGER.info("[SWEEP] Interrupted by user.")
    except OSError as exc:
        LOGGER.error("[SWEEP TCP ERROR] %r", exc)
    finally:
        if link.has_connected:
            try:
                link.send(pack_frame(0, NEUTRAL_AXES))
            except OSError:
                pass
        LOGGER.info(
            "[SWEEP SUMMARY] frames_sent=%d | tcp_connect_attempts=%d | tcp_failures=%d",
            link.frames_sent,
            link.connection_attempts,
            link.failures,
        )


# =============================================================================
# COMMAND LINE
# =============================================================================


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read a USB gamepad with Pygame and send input frames to OmniPad PS5 over LAN.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=PS5_IP, help="PS5 / OmniPad LAN IP address")
    parser.add_argument("--port", type=int, default=OMNIPAD_INPUT_PORT, help="OmniPad TCP input port")
    parser.add_argument("--device", type=int, default=CONTROLLER_INDEX, help="Pygame controller index")
    parser.add_argument("--list-devices", action="store_true", help="List detected controllers and exit")
    parser.add_argument("--monitor", action="store_true", help="Log controller input without sending anything to the PS5")
    parser.add_argument("--sweep", action="store_true", help="Send each of the 32 OmniPad button-mask bits individually")
    parser.add_argument("--log-file", default=LOG_FILE_PATH, help="Rotating diagnostic log file path")
    parser.add_argument("--no-log-file", action="store_true", help="Disable file logging; keep console logs")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    log_file = None if args.no_log_file else args.log_file
    setup_logging(log_file)

    if TCP_CONNECTION_MODE != "per_frame":
        LOGGER.error("Unsupported TCP_CONNECTION_MODE=%r. This release only implements the tested per_frame mode.", TCP_CONNECTION_MODE)
        return 2

    if not (1 <= args.port <= 65535):
        LOGGER.error("Invalid TCP port: %s", args.port)
        return 2

    if args.list_devices:
        list_devices()
        return 0

    if args.monitor and args.sweep:
        LOGGER.error("--monitor and --sweep cannot be used together.")
        return 2

    LOGGER.info("[START] OmniPad Gamepad Bridge")
    LOGGER.info("[ENV] Python=%s | OS=%s", sys.version.split()[0], platform.platform())
    if pygame is not None:
        LOGGER.info("[ENV] Pygame=%s", pygame.version.ver)
    LOGGER.info("[NETWORK] PS5 target=%s:%d | dashboard=http://%s:%d/", args.host, args.port, args.host, OMNIPAD_DASHBOARD_PORT)
    LOGGER.info("[NETWORK] TCP mode=%s | send interval=%.3fs", TCP_CONNECTION_MODE, 1.0 / max(1.0, SEND_RATE_HZ))
    LOGGER.info("[PROTOCOL] frame=%d bytes | struct=%s", FRAME_LENGTH_BYTES, FRAME_FORMAT)

    try:
        if args.sweep:
            run_button_sweep(args.host, args.port)
        else:
            run_input_loop(args.host, args.port, args.device, args.monitor)
    except SystemExit:
        raise
    except Exception:
        LOGGER.exception("[FATAL] Unexpected bridge error")
        return 1

    LOGGER.info("[DONE] Bridge process finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
