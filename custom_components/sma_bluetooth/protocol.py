# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2010-2011 Wim Hofman and Stephen Collier
# Copyright (C) 2026 sma-net2-bluetooth contributors

"""Pure Python SMA Bluetooth Classic / SMA-Net2 protocol client.

This module is a Python adaptation of the GPLv3-or-later protocol work in
sma-bluetooth/sma-bluetooth (Wim Hofman and Stephen Collier, 2010-2011).
It contains no external executable or native library and performs all RFCOMM
framing and SMA record parsing itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import random
import socket
import struct
import time

from .const import (
    CONNECTION_MODE_AUTO,
    CONNECTION_MODE_NETWORK,
    CONNECTION_MODE_SINGLE,
    EFFECTIVE_MODE_NETWORK,
    EFFECTIVE_MODE_SINGLE,
    NETWORK_ROLE_DIRECT,
    NETWORK_ROLE_PARTICIPANT,
    NETWORK_ROLE_ROOT,
)
from .models import SMAInverter

APP_SUSY_ID = 125
ANY_SUSY_ID = 0xFFFF
ANY_SERIAL = 0xFFFFFFFF
UNKNOWN_ADDRESS = b"\xff" * 6
L2_SIGNATURE = 0x656003FF
RESERVED = {0x7D, 0x7E, 0x11, 0x12, 0x13}

STATUS_TAGS = {
    35: "Fault",
    51: "Closed",
    307: "Ok",
    311: "Open",
    455: "Warning",
    0xFFFFFD: "Information not available",
}
MODEL_TAGS = {9073: "SB 3000HF-30"}
CLASS_TAGS = {8001: "Solar Inverters"}

LRI_OPERATION_HEALTH = 0x00214800
LRI_TEMPERATURE = 0x00237700
LRI_DC_POWER = 0x00251E00
LRI_ENERGY_TOTAL = 0x00260100
LRI_ENERGY_TODAY = 0x00262200
LRI_AC_POWER_TOTAL = 0x00263F00
LRI_RELAY_STATUS = 0x00416400
LRI_OPERATION_TIME = 0x00462E00
LRI_FEED_IN_TIME = 0x00462F00
LRI_DC_VOLTAGE = 0x00451F00
LRI_DC_CURRENT = 0x00452100
LRI_AC_POWER_1 = 0x00464000
LRI_AC_POWER_2 = 0x00464100
LRI_AC_POWER_3 = 0x00464200
LRI_AC_VOLTAGE_1 = 0x00464800
LRI_AC_VOLTAGE_2 = 0x00464900
LRI_AC_VOLTAGE_3 = 0x00464A00
LRI_AC_CURRENT_1 = 0x00465000
LRI_AC_CURRENT_2 = 0x00465100
LRI_AC_CURRENT_3 = 0x00465200
LRI_AC_CURRENT_ALT_1 = 0x00465300
LRI_AC_CURRENT_ALT_2 = 0x00465400
LRI_AC_CURRENT_ALT_3 = 0x00465500
LRI_FREQUENCY = 0x00465700
LRI_DEVICE_NAME = 0x00821E00
LRI_DEVICE_CLASS = 0x00821F00
LRI_DEVICE_MODEL = 0x00822000
LRI_SOFTWARE_VERSION = 0x00823400

QUERIES = (
    (0x58000200, 0x00823400, 0x008234FF),
    (0x58000200, 0x00821E00, 0x008220FF),
    (0x51800200, 0x00214800, 0x002148FF),
    (0x52000200, 0x00237700, 0x002377FF),
    (0x51800200, 0x00416400, 0x004164FF),
    (0x54000200, 0x00260100, 0x002622FF),
    (0x54000200, 0x00462E00, 0x00462FFF),
    (0x53800200, 0x00251E00, 0x00251EFF),
    (0x53800200, 0x00451F00, 0x004521FF),
    (0x51000200, 0x00464000, 0x004642FF),
    (0x51000200, 0x00464800, 0x004655FF),
    (0x51000200, 0x00263F00, 0x00263FFF),
    (0x51000200, 0x00465700, 0x004657FF),
)


class SMAProtocolError(RuntimeError):
    """SMA Bluetooth protocol failure."""


class SMATransportError(SMAProtocolError):
    """The Bluetooth transport failed before a valid SMA response arrived."""


class SMAAuthenticationError(SMAProtocolError):
    """The inverter rejected the configured password."""


class SMAConfigurationError(SMAProtocolError):
    """The confirmed entry configuration does not match the physical system."""


class SMANetworkModeError(SMAConfigurationError):
    """Full-network mode is incompatible with the detected NetID."""

    def __init__(self, net_id: int) -> None:
        super().__init__(
            f"Full SMA network mode requires NetID 2-F; detected NetID {net_id:X}"
        )
        self.net_id = net_id


@dataclass(slots=True)
class _Device:
    address: bytes
    serial: int = 0
    susy_id: int = 0
    inverter: SMAInverter | None = None


@dataclass(frozen=True, slots=True)
class SMAArchivePoint:
    """One validated five-minute archive point."""

    timestamp: int
    total_energy_kwh: float
    power_w: float | None


@dataclass(frozen=True, slots=True)
class SMAClockInfo:
    """Plant clock information returned by the SMA Bluetooth network."""

    current_time: int
    last_time_set: int
    timezone_offset: int
    dst_active: bool
    set_count: int


@dataclass(frozen=True, slots=True)
class SMAClockSyncResult:
    """Result of one guarded plant clock synchronization check."""

    before: SMAClockInfo
    after: SMAClockInfo | None
    difference_seconds: int
    adjusted: bool
    reason: str


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _fcs(data: bytes) -> int:
    checksum = 0xFFFF
    for value in data:
        checksum ^= value
        for _ in range(8):
            checksum = (checksum >> 1) ^ (0x8408 if checksum & 1 else 0)
    return checksum ^ 0xFFFF


def _escape(data: bytes) -> bytes:
    result = bytearray()
    for value in data:
        if value in RESERVED:
            result.extend((0x7D, value ^ 0x20))
        else:
            result.append(value)
    return bytes(result)


def _unescape(data: bytes) -> bytes:
    result = bytearray()
    escaped = False
    for value in data:
        if escaped:
            result.append(value ^ 0x20)
            escaped = False
        elif value == 0x7D:
            escaped = True
        else:
            result.append(value)
    if escaped:
        raise SMAProtocolError("Truncated SMA escape sequence")
    return bytes(result)


def _active_attribute(record: bytes) -> int | None:
    for offset in range(8, min(len(record), 40), 4):
        attribute = _u32(record, offset)
        tag = attribute & 0x00FFFFFF
        if tag == 0xFFFFFE:
            break
        if attribute >> 24 == 1:
            return tag
    return None


def _version(value: int) -> str:
    revision_types = "NEABRS"
    revision = value & 0xFF
    revision_name = revision_types[revision] if revision < len(revision_types) else "?"
    build = (value >> 8) & 0xFF
    minor = (value >> 16) & 0xFF
    major = (value >> 24) & 0xFF
    return f"{major >> 4}{major & 0x0F}.{minor >> 4}{minor & 0x0F}.{build:02d}.{revision_name}"


class SMAClassicClient:
    """One RFCOMM transport to an SMA Bluetooth plant."""

    def __init__(
        self,
        address: str,
        password: str,
        timeout: float = 10,
        connection_mode: str = CONNECTION_MODE_AUTO,
    ) -> None:
        if connection_mode not in {
            CONNECTION_MODE_AUTO,
            CONNECTION_MODE_SINGLE,
            CONNECTION_MODE_NETWORK,
        }:
            raise ValueError(f"Unsupported SMA connection mode: {connection_mode}")
        self.address = address
        self.password = password
        self.timeout = timeout
        self.connection_mode = connection_mode
        self.connection_address = bytes.fromhex(address.replace(":", ""))[::-1]
        self.root_address = self.connection_address
        self.local_address = b"\0" * 6
        self.app_serial = random.randint(900_000_000, 999_999_999)
        self.packet_id = random.randint(1, 0x7FFE)
        self.sock: socket.socket | None = None
        self.devices: list[_Device] = []
        self._session_active = False
        self._signals: dict[bytes, float] = {}
        self.net_id: int | None = None
        self.effective_mode: str | None = None
        self.root_serial: str | None = None

    @staticmethod
    def format_bluetooth_address(address: bytes) -> str:
        """Format SMA's little-endian Bluetooth address for display."""
        return ":".join(f"{part:02X}" for part in reversed(address))

    @property
    def current_root(self) -> str | None:
        """Return the root serial, or its address when it is not an inverter."""
        if self.root_serial is not None:
            return self.root_serial
        if self.root_address:
            return self.format_bluetooth_address(self.root_address)
        return None

    async def __aenter__(self) -> SMAClassicClient:
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise SMAProtocolError("Python has no Bluetooth socket support")
        self.sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
        )
        self.sock.setblocking(False)
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(self.sock, (self.address, 1)),
                self.timeout,
            )
        except (OSError, TimeoutError) as err:
            self.sock.close()
            self.sock = None
            raise SMATransportError(f"RFCOMM connection failed: {err}") from err
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self._session_active = False

    async def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise SMAProtocolError("RFCOMM socket is not connected")
        result = bytearray()
        while len(result) < length:
            try:
                chunk = await asyncio.wait_for(
                    asyncio.get_running_loop().sock_recv(
                        self.sock, length - len(result)
                    ),
                    self.timeout,
                )
            except TimeoutError as err:
                raise SMATransportError("Timeout receiving SMA packet") from err
            if not chunk:
                raise SMATransportError("SMA closed the RFCOMM connection")
            result.extend(chunk)
        return bytes(result)

    async def _send(self, packet: bytes) -> None:
        if self.sock is None:
            raise SMAProtocolError("RFCOMM socket is not connected")
        await asyncio.get_running_loop().sock_sendall(self.sock, packet)

    def _l1(self, control: int, destination: bytes, payload: bytes = b"") -> bytes:
        packet = bytearray(b"\x7e\0\0\0")
        packet.extend(self.local_address)
        packet.extend(destination)
        packet.extend(struct.pack("<H", control))
        packet.extend(payload)
        struct.pack_into("<H", packet, 1, len(packet))
        packet[3] = packet[0] ^ packet[1] ^ packet[2]
        return bytes(packet)

    def _l2_payload(
        self,
        longwords: int,
        control: int,
        control2: int,
        dest_susy: int,
        dest_serial: int,
        payload: bytes,
    ) -> bytes:
        while True:
            self.packet_id = (self.packet_id + 1) & 0x7FFF or 1
            body = bytearray(
                struct.pack(
                    "<IBBHIH",
                    L2_SIGNATURE,
                    longwords,
                    control,
                    dest_susy,
                    dest_serial,
                    control2,
                )
            )
            body.extend(struct.pack("<HIH", APP_SUSY_ID, self.app_serial, control2))
            body.extend(struct.pack("<HHH", 0, 0, self.packet_id | 0x8000))
            body.extend(payload)
            checksum = _fcs(body)
            low, high = checksum & 0xFF, checksum >> 8
            if low not in RESERVED and high not in RESERVED:
                return b"\x7e" + _escape(body) + bytes((low, high, 0x7E))

    async def _receive_packet(
        self, command: int | None, sender: bytes | None = None
    ) -> tuple[bytes, bytes]:
        fragments = bytearray()
        fragment_source: bytes | None = None
        while True:
            header = await self._recv_exact(18)
            if header[0] != 0x7E:
                raise SMAProtocolError("Invalid SMA packet marker")
            length = _u16(header, 1)
            if length < 18 or header[3] != header[0] ^ header[1] ^ header[2]:
                raise SMAProtocolError("Invalid SMA L1 header")
            payload = await self._recv_exact(length - 18)
            source = header[4:10]
            received_command = _u16(header, 16)
            if sender is not None and source != sender:
                continue
            if fragments:
                if source != fragment_source:
                    continue
                fragments.extend(payload)
                if command is not None and received_command != command:
                    continue
                inner = _unescape(bytes(fragments))
            elif payload.startswith(b"\x7e"):
                if command is not None and received_command != command:
                    # Large SMA L2 replies use command 8 for one or more
                    # leading L1 fragments and command 1 for the final part.
                    fragments.extend(payload)
                    fragment_source = source
                    continue
                inner = _unescape(payload)
            else:
                if command is not None and received_command != command:
                    continue
                return header + payload, source

            if inner:
                if not inner.endswith(b"\x7e"):
                    fragments = bytearray(payload)
                    fragment_source = source
                    continue
                if len(inner) < 8 or _u32(inner, 1) != L2_SIGNATURE:
                    raise SMAProtocolError("Invalid SMA L2 signature")
                if _fcs(inner[1:-3]) != _u16(inner, len(inner) - 3):
                    raise SMAProtocolError("Invalid SMA L2 checksum")
                return inner, source

    async def _identify_devices(self) -> None:
        """Identify the selected inverter or every inverter in the topology."""
        request = self._l2_payload(
            0x09,
            0xA0,
            0,
            ANY_SUSY_ID,
            ANY_SERIAL,
            struct.pack("<III", 0x00000200, 0, 0),
        )
        request_id = self.packet_id
        await self._send(self._l1(0x01, UNKNOWN_ADDRESS, request))
        identified: list[_Device] = []
        by_address = {device.address: device for device in self.devices}
        expected_devices = len(self.devices)
        while len(identified) < expected_devices:
            packet, source = await self._receive_packet(0x01)
            if (_u16(packet, 27) & 0x7FFF) != request_id:
                continue
            device = by_address.get(source)
            if device is None or device in identified or len(packet) < 61:
                continue
            device.susy_id = _u16(packet, 55)
            device.serial = _u32(packet, 57)
            device.inverter = SMAInverter(
                serial=str(device.serial),
                susy_id=str(device.susy_id),
                bluetooth_address=self.format_bluetooth_address(device.address),
            )
            identified.append(device)
        self.devices = identified
        if not self.devices:
            raise SMAProtocolError("No SMA inverter answered identification")

        root_device = next(
            (device for device in self.devices if device.address == self.root_address),
            None,
        )
        self.root_serial = str(root_device.serial) if root_device is not None else None

    async def _initialize_single(self, net_id: int) -> None:
        """Initialize one inverter using SMA's no-MIS compatibility procedure."""
        self.root_address = self.connection_address
        payload = (
            struct.pack("<I", 0x00700400) + bytes((net_id,)) + struct.pack("<II", 0, 1)
        )
        await self._send(self._l1(0x02, self.connection_address, payload))
        direct_reply, _ = await self._receive_packet(0x05, self.connection_address)
        if len(direct_reply) >= 32:
            self.local_address = direct_reply[26:32]
        self.devices = [_Device(self.connection_address)]
        await self._identify_devices()

    async def _initialize_network(self, net_id: int) -> None:
        """Initialize the full topology for a NetID 2-F Bluetooth network."""
        if net_id == 1:
            raise SMANetworkModeError(net_id)

        payload = (
            struct.pack("<I", 0x00700400) + bytes((net_id,)) + struct.pack("<II", 0, 1)
        )
        await self._send(self._l1(0x02, self.root_address, payload))
        root_reply, _ = await self._receive_packet(0x0A, self.root_address)
        if len(root_reply) > 30:
            if root_reply[24] == 2:
                self.root_address = root_reply[18:24]
            self.local_address = root_reply[25:31]

        topology, _ = await self._receive_packet(0x05, self.root_address)
        self.devices = self._topology_devices(topology, net_id)
        if len(self.devices) == 1:
            await self._build_network(net_id)
        if not self.devices:
            raise SMAProtocolError("No inverter found in SMA Bluetooth topology")
        await self._identify_devices()

    async def _initialize(self) -> None:
        """Detect NetID and initialize the selected effective connection mode."""
        self.root_address = self.connection_address
        await self._send(self._l1(0x0201, b"\x01\0\0\0\0\0", b"ver\r\n"))
        announcement, _ = await self._receive_packet(0x02, self.connection_address)
        if len(announcement) < 23 or announcement[19] < 4:
            raise SMAProtocolError("Unsupported SMA Bluetooth firmware")
        self.net_id = announcement[22]

        if self.connection_mode == CONNECTION_MODE_AUTO:
            self.effective_mode = (
                EFFECTIVE_MODE_SINGLE if self.net_id == 1 else EFFECTIVE_MODE_NETWORK
            )
        elif self.connection_mode == CONNECTION_MODE_SINGLE:
            self.effective_mode = EFFECTIVE_MODE_SINGLE
        else:
            self.effective_mode = EFFECTIVE_MODE_NETWORK

        if self.effective_mode == EFFECTIVE_MODE_SINGLE:
            await self._initialize_single(self.net_id)
        else:
            await self._initialize_network(self.net_id)

    @staticmethod
    def _topology_devices(packet: bytes, net_id: int) -> list[_Device]:
        devices: list[_Device] = []
        for offset in range(18, len(packet) - 7, 8):
            if _u16(packet, offset + 6) == 0x0101:
                devices.append(_Device(packet[offset : offset + 6]))
        return devices

    async def _build_network(self, net_id: int) -> None:
        for payload in (
            struct.pack("<HB", 0x000A, 0xAC),
            struct.pack("<H", 2),
            struct.pack("<HB", 1, 1),
        ):
            await self._send(self._l1(0x03, self.root_address, payload))
            await self._receive_packet(0x04, self.root_address)
        for _ in range(7):
            packet, _ = await self._receive_packet(None, self.root_address)
            packet_type = _u16(packet, 16)
            if packet_type == 0x1001:
                packet, _ = await self._receive_packet(0x05, self.root_address)
                packet_type = _u16(packet, 16)
            if packet_type == 0x0005:
                found = self._topology_devices(packet, net_id)
                known_addresses = {device.address for device in self.devices}
                self.devices.extend(
                    device for device in found if device.address not in known_addresses
                )
            if packet_type == 0x0006:
                return

    async def _logon(self) -> None:
        encoded = bytes(
            ((ord(char) + 0x88) & 0xFF) for char in self.password[:12]
        ).ljust(12, b"\x88")
        now = int(time.time())
        payload = (
            struct.pack("<IIIII", 0xFFFD040C, 0x00000007, 0x00000384, now, 0) + encoded
        )
        inner = self._l2_payload(0x0E, 0xA0, 0x0100, ANY_SUSY_ID, ANY_SERIAL, payload)
        request_id = self.packet_id
        await self._send(self._l1(0x01, UNKNOWN_ADDRESS, inner))
        replies = 0
        while replies < len(self.devices):
            packet, source = await self._receive_packet(0x01)
            if (_u16(packet, 27) & 0x7FFF) != request_id or _u32(packet, 41) != now:
                continue
            return_code = _u16(packet, 23)
            if return_code == 0x0100:
                raise SMAAuthenticationError("Invalid SMA user password")
            if return_code:
                raise SMAProtocolError(f"SMA logon returned {return_code:#x}")
            replies += 1
            for device in self.devices:
                if device.address == source:
                    device.susy_id = _u16(packet, 15)
                    device.serial = _u32(packet, 17)
                    break

    async def _query(
        self, device: _Device, command: int, first: int, last: int
    ) -> None:
        for attempt in range(3):
            inner = self._l2_payload(
                0x09,
                0xA0,
                0,
                device.susy_id,
                device.serial,
                struct.pack("<III", command, first, last),
            )
            request_id = self.packet_id
            await self._send(self._l1(0x01, UNKNOWN_ADDRESS, inner))
            try:
                while True:
                    packet, _ = await self._receive_packet(0x01, device.address)
                    if (_u16(packet, 27) & 0x7FFF) != request_id:
                        continue
                    status = _u16(packet, 23)
                    if status == 21:
                        return
                    if status:
                        raise SMAProtocolError(f"SMA data request returned {status}")
                    self._parse_records(device, packet)
                    if _u16(packet, 25) == 0:
                        return
            except SMAProtocolError as err:
                if attempt == 2 or "Timeout" not in str(err):
                    raise SMAProtocolError(
                        f"Query {first:#010x}-{last:#010x} for {device.serial} failed: {err}"
                    ) from err

    @staticmethod
    def _parse_records(device: _Device, packet: bytes) -> None:
        inverter = device.inverter
        if inverter is None or len(packet) < 44:
            return
        record_count = _u32(packet, 37) - _u32(packet, 33) + 1
        if record_count <= 0:
            return
        record_size = 4 * (packet[5] - 9) // record_count
        if record_size < 12:
            return
        for offset in range(41, len(packet) - 3, record_size):
            record = packet[offset : offset + record_size]
            if len(record) < record_size:
                break
            code = _u32(record, 0)
            # Apply records in transfer order and expose the source clock as-is.
            # A backwards timestamp must remain visible for diagnostics.
            inverter.record_timestamp = _u32(record, 4)
            inverter.record_received_at = time.time()
            lri = code & 0x00FFFF00
            channel = code & 0xFF
            raw_value = _u32(record, 16) if record_size > 19 else 0
            value = (
                None
                if record_size <= 19 or raw_value in (0x80000000, 0xFFFFFFFF)
                else _i32(record, 16)
            )
            raw_value64 = _u64(record, 8) if record_size == 16 else 0
            value64 = (
                None
                if record_size != 16
                or raw_value64 in (0x8000000000000000, 0xFFFFFFFFFFFFFFFF)
                else raw_value64
            )
            values = inverter.values
            if lri == LRI_ENERGY_TODAY:
                values["energy_today"] = value64 / 1000 if value64 is not None else None
            elif lri == LRI_ENERGY_TOTAL:
                values["energy_total"] = value64 / 1000 if value64 is not None else None
            elif lri == LRI_OPERATION_TIME:
                values["operation_time"] = (
                    value64 / 3600 if value64 is not None else None
                )
            elif lri == LRI_FEED_IN_TIME:
                values["feed_in_time"] = value64 / 3600 if value64 is not None else None
            elif lri == LRI_AC_POWER_TOTAL:
                values["ac_power_total"] = value
            elif lri == LRI_DC_POWER:
                values[f"dc_power_{channel}"] = value
            elif lri == LRI_DC_VOLTAGE:
                values[f"dc_voltage_{channel}"] = (
                    value / 100 if value is not None else None
                )
            elif lri == LRI_DC_CURRENT:
                values[f"dc_current_{channel}"] = (
                    value / 1000 if value is not None else None
                )
            elif lri in (LRI_AC_POWER_1, LRI_AC_POWER_2, LRI_AC_POWER_3):
                values[f"ac_power_{lri // 0x100 - LRI_AC_POWER_1 // 0x100 + 1}"] = value
            elif lri in (LRI_AC_VOLTAGE_1, LRI_AC_VOLTAGE_2, LRI_AC_VOLTAGE_3):
                values[f"ac_voltage_{lri // 0x100 - LRI_AC_VOLTAGE_1 // 0x100 + 1}"] = (
                    value / 100 if value is not None else None
                )
            elif lri in (
                LRI_AC_CURRENT_1,
                LRI_AC_CURRENT_2,
                LRI_AC_CURRENT_3,
                LRI_AC_CURRENT_ALT_1,
                LRI_AC_CURRENT_ALT_2,
                LRI_AC_CURRENT_ALT_3,
            ):
                base = (
                    LRI_AC_CURRENT_1
                    if lri <= LRI_AC_CURRENT_3
                    else LRI_AC_CURRENT_ALT_1
                )
                values[f"ac_current_{lri // 0x100 - base // 0x100 + 1}"] = (
                    value / 1000 if value is not None else None
                )
            elif lri == LRI_FREQUENCY:
                values["frequency"] = value / 100 if value is not None else None
            elif lri == LRI_TEMPERATURE:
                values["temperature"] = value / 100 if value is not None else None
            elif lri in (LRI_OPERATION_HEALTH, LRI_RELAY_STATUS):
                tag = _active_attribute(record)
                key = "status" if lri == LRI_OPERATION_HEALTH else "relay_status"
                values[key] = STATUS_TAGS.get(
                    tag, str(tag) if tag is not None else None
                )
            elif lri == LRI_DEVICE_NAME:
                inverter.name = record[8:].split(b"\0", 1)[0].decode(errors="replace")
            elif lri in (LRI_DEVICE_CLASS, LRI_DEVICE_MODEL):
                tag = _active_attribute(record)
                if lri == LRI_DEVICE_MODEL:
                    inverter.model = MODEL_TAGS.get(tag, f"SMA model {tag}")
                elif not inverter.model and tag in CLASS_TAGS:
                    inverter.model = CLASS_TAGS[tag]
            elif lri == LRI_SOFTWARE_VERSION and len(record) >= 28:
                inverter.software_version = _version(_u32(record, 24))

    async def _signal_strength(self, device: _Device) -> float | None:
        await self._send(self._l1(0x03, device.address, b"\x05\0"))
        packet, _ = await self._receive_packet(0x04, device.address)
        return packet[22] * 100 / 255 if len(packet) > 22 else None

    async def _read_clock(self) -> SMAClockInfo:
        """Read plant time, timezone and last synchronization metadata."""
        payload = struct.pack(
            "<IIIIIIIIII",
            0xF000020A,
            0x00236D00,
            0x00236D00,
            0x00236D00,
            0,
            0,
            0,
            0,
            1,
            1,
        )
        inner = self._l2_payload(0x10, 0xA0, 0, ANY_SUSY_ID, ANY_SERIAL, payload)
        await self._send(self._l1(0x01, UNKNOWN_ADDRESS, inner))
        while True:
            packet, _ = await self._receive_packet(0x01)
            if len(packet) < 65 or (_u32(packet, 41) & 0xFFFFFF00) != 0x00236D00:
                continue
            timezone_and_dst = _u32(packet, 57)
            return SMAClockInfo(
                current_time=_u32(packet, 45),
                last_time_set=_u32(packet, 49),
                timezone_offset=timezone_and_dst & 0xFFFFFFFE,
                dst_active=bool(timezone_and_dst & 1),
                set_count=_u32(packet, 61),
            )

    async def _set_clock(
        self,
        timestamp: int,
        timezone_offset: int,
        dst_active: bool,
        set_count: int,
    ) -> None:
        """Set the plant clock using SMA's plant-wide time command."""
        payload = struct.pack(
            "<IIIIIIIIII",
            0xF000020A,
            0x00236D00,
            0x00236D00,
            0x00236D00,
            timestamp,
            timestamp,
            timestamp,
            timezone_offset | int(dst_active),
            set_count,
            1,
        )
        inner = self._l2_payload(0x10, 0xA0, 0, ANY_SUSY_ID, ANY_SERIAL, payload)
        await self._send(self._l1(0x01, UNKNOWN_ADDRESS, inner))

    async def async_sync_clock_active(
        self,
        timezone_offset: int,
        dst_active: bool,
        *,
        lower_limit: int = 60,
        upper_limit: int = 3600,
        minimum_set_interval: int = 86400,
    ) -> SMAClockSyncResult:
        """Check and safely synchronize time on an authenticated session."""
        if not self._session_active:
            raise SMAProtocolError("SMA session is not authenticated")
        before = await self._read_clock()
        host_time = int(time.time())
        difference = before.current_time - host_time
        absolute_difference = abs(difference)
        if absolute_difference <= lower_limit:
            return SMAClockSyncResult(
                before, None, difference, False, "within_tolerance"
            )
        if absolute_difference >= upper_limit:
            return SMAClockSyncResult(
                before, None, difference, False, "unsafe_difference"
            )
        if (
            before.last_time_set > 0
            and host_time - before.last_time_set < minimum_set_interval
        ):
            return SMAClockSyncResult(
                before, None, difference, False, "recently_adjusted"
            )

        new_time = int(time.time())
        await self._set_clock(
            new_time,
            timezone_offset,
            dst_active,
            before.set_count + 1,
        )
        await asyncio.sleep(1)
        after = await self._read_clock()
        if abs(after.current_time - int(time.time())) > 5:
            raise SMAProtocolError("Plant clock verification failed")
        return SMAClockSyncResult(before, after, difference, True, "adjusted")

    async def _archive_day(
        self, device: _Device, start_timestamp: int, end_timestamp: int
    ) -> list[SMAArchivePoint]:
        """Read and validate one day of five-minute production history."""
        inner = self._l2_payload(
            0x09,
            0xE0,
            0,
            device.susy_id,
            device.serial,
            struct.pack(
                "<III",
                0x70000200,
                start_timestamp - 600,
                end_timestamp - 300,
            ),
        )
        request_id = self.packet_id
        await self._send(self._l1(0x01, device.address, inner))
        points: list[SMAArchivePoint] = []
        previous_time = 0
        previous_total = 0
        while True:
            packet, _ = await self._receive_packet(0x01, device.address)
            if (_u16(packet, 27) & 0x7FFF) != request_id:
                continue
            for offset in range(41, len(packet) - 3, 12):
                if offset + 12 > len(packet) - 3:
                    break
                timestamp = _u32(packet, offset)
                total_wh = _u64(packet, offset + 4)
                invalid = (
                    total_wh in (0x8000000000000000, 0xFFFFFFFFFFFFFFFF)
                    or timestamp % 300 != 0
                )
                if invalid:
                    continue
                power = None
                if previous_time and previous_total and timestamp != previous_time:
                    power = (
                        (total_wh - previous_total) * 3600 / (timestamp - previous_time)
                    )
                previous_time = timestamp
                previous_total = total_wh
                if start_timestamp <= timestamp < end_timestamp:
                    points.append(SMAArchivePoint(timestamp, total_wh / 1000, power))
            if _u16(packet, 25) == 0:
                return points

    async def _logoff(self) -> None:
        inner = self._l2_payload(
            0x08,
            0xA0,
            0x0300,
            ANY_SUSY_ID,
            ANY_SERIAL,
            struct.pack("<II", 0xFFFD010E, 0xFFFFFFFF),
        )
        await self._send(self._l1(0x01, UNKNOWN_ADDRESS, inner))

    async def async_start_session(self) -> None:
        """Initialize and authenticate one reusable daytime session."""
        if self._session_active:
            return
        await self._initialize()
        # Discovery runs anonymously. Close that logical session before login.
        await self._logoff()
        self._signals = {
            device.address: await self._signal_strength(device)
            for device in self.devices
        }
        await self._logon()
        self._session_active = True

    async def async_stop_session(self) -> None:
        """Log off while leaving transport cleanup to the context manager."""
        if not self._session_active:
            return
        try:
            await self._logoff()
        finally:
            self._session_active = False

    async def async_query_active(self) -> dict[str, SMAInverter]:
        """Read all inverters through an already authenticated session."""
        if not self._session_active:
            raise SMAProtocolError("SMA session is not authenticated")
        for device in self.devices:
            device.inverter = SMAInverter(
                serial=str(device.serial),
                susy_id=str(device.susy_id),
                bluetooth_address=self.format_bluetooth_address(device.address),
            )

        # Multi-inverter Bluetooth networks expect one query class to be
        # completed for every participant before moving to the next class.
        for command, first, last in QUERIES:
            for device in self.devices:
                await self._query(device, command, first, last)
        for device in self.devices:
            if device.inverter is not None:
                if self.effective_mode == EFFECTIVE_MODE_SINGLE:
                    device.inverter.network_role = NETWORK_ROLE_DIRECT
                elif device.address == self.root_address:
                    device.inverter.network_role = NETWORK_ROLE_ROOT
                else:
                    device.inverter.network_role = NETWORK_ROLE_PARTICIPANT
                device.inverter.values["bt_signal"] = self._signals[device.address]
                dc_power_values = [
                    value
                    for key, value in device.inverter.values.items()
                    if key.startswith("dc_power_")
                    and key != "dc_power_total"
                    and value is not None
                ]
                device.inverter.values["dc_power_total"] = (
                    sum(dc_power_values) if dc_power_values else None
                )
        return {
            str(device.serial): device.inverter
            for device in self.devices
            if device.inverter is not None
        }

    async def async_query(self) -> dict[str, SMAInverter]:
        """Discover, authenticate, and read all inverters in the plant."""
        await self.async_start_session()
        try:
            return await self.async_query_active()
        finally:
            await self.async_stop_session()

    async def async_read_archive_active(
        self, periods: list[int | tuple[int, int]]
    ) -> dict[str, list[SMAArchivePoint]]:
        """Read archive days through an already authenticated session."""
        if not self._session_active:
            raise SMAProtocolError("SMA session is not authenticated")
        result: dict[str, list[SMAArchivePoint]] = {
            str(device.serial): [] for device in self.devices
        }
        for period in periods:
            if isinstance(period, int):
                start, end = period, period + 86400
            else:
                start, end = period
            for device in self.devices:
                result[str(device.serial)].extend(
                    await self._archive_day(device, start, end)
                )
        return result

    async def async_read_archive(
        self, periods: list[int | tuple[int, int]]
    ) -> dict[str, list[SMAArchivePoint]]:
        """Read selected archive days without changing inverter settings."""
        await self.async_start_session()
        try:
            return await self.async_read_archive_active(periods)
        finally:
            await self.async_stop_session()

    async def async_sync_clock(
        self,
        timezone_offset: int,
        dst_active: bool,
        *,
        lower_limit: int = 60,
        upper_limit: int = 3600,
        minimum_set_interval: int = 86400,
    ) -> SMAClockSyncResult:
        """Synchronize time through one self-contained session."""
        await self.async_start_session()
        try:
            return await self.async_sync_clock_active(
                timezone_offset,
                dst_active,
                lower_limit=lower_limit,
                upper_limit=upper_limit,
                minimum_set_interval=minimum_set_interval,
            )
        finally:
            await self.async_stop_session()
