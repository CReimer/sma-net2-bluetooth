# sma-net2

An asynchronous Python client for the legacy SMA-Net2 Bluetooth Classic
protocol. It discovers and authenticates SMA inverters and reads measurements
and the inverter's five-minute archive. It has no Home Assistant dependency.

This is the communication library extracted from
[SMA-Net2 Bluetooth for Home Assistant](https://github.com/CReimer/sma-net2-bluetooth).

## Requirements

- Python 3.11 or newer, on Linux with Bluetooth Classic RFCOMM support.
- A local Bluetooth adapter and SMA-Net2-capable inverter within radio range.
- The SMA **user** password, not an installer password or Bluetooth pairing PIN.
- For optional discovery: access to BlueZ on the system D-Bus.

ESPHome BLE proxies do not support this protocol. The known tested hardware is
SMA Sunny Boy SB 3000HF-30; other legacy Bluetooth models may provide different
measurement sets. This project is not affiliated with or endorsed by SMA.

## Installation

Install a published release from PyPI:

```sh
python -m pip install sma-net2==0.1.1
```

## Development installation

For development, install from the package source directory:

```sh
python -m pip install -e '.[test]'
```

## Read measurements

```python
import asyncio
import os

from sma_net2 import SMAClassicClient


async def main():
    async with SMAClassicClient(
        os.environ["SMA_BT_ADDRESS"],
        os.environ["SMA_USER_PASSWORD"],
        connection_mode="auto",
    ) as client:
        inverters = await client.async_query()
        for serial, inverter in inverters.items():
            print(serial, inverter.model, inverter.values)


asyncio.run(main())
```

`auto` uses direct mode for NetID 1 and network mode for NetID 2–F. `single`
reads only the selected inverter. `network` requires NetID 2–F. The result maps
serial-number strings to `SMAInverter` objects. Missing measurements remain
unknown; energy counters and source timestamps are not altered to hide resets
or backwards readings.

Use one client per session and serialize clients sharing a Bluetooth adapter.
The library does not apply Home Assistant's daylight scheduling, retry policy,
adapter recovery, entity ownership or Recorder import policy; the caller owns
those decisions. Inverters are normally unreachable at night.

`async_query()`, `async_read_archive(periods)` and `async_sync_clock(...)` each
start and stop an authenticated session inside the open RFCOMM context. For
multiple operations in one session, call `async_start_session()`, use the
corresponding `*_active()` methods, and call `async_stop_session()` in `finally`.
Clock synchronization writes to the plant clock and is explicitly invoked,
with bounds and a minimum interval checked by the client.

`async_read_archive()` accepts Unix start/end timestamp pairs (end exclusive),
and returns a mapping from each serial to a list of `SMAArchivePoint` objects. No Home Assistant statistics
are written. Original source ordering is preserved. Discovery is available as
`from sma_net2.discovery import async_discover_sma_devices` and returns a mapping
of Bluetooth addresses to display names.

Catch `SMAAuthenticationError` for incorrect credentials,
`SMAConfigurationError`/`SMANetworkModeError` for incompatible topology, and
`SMATransportError` for connection failures. These derive from `SMAProtocolError`.
The async context manager closes the socket on exit.

## Offline tests and distribution build

```sh
python ../../tools/run_library_tests.py
python -m build
python -m twine check dist/*
```

The tests use mock sockets and D-Bus objects; they never contact an inverter or
change its clock. The CI workflow tests Python 3.11 and 3.14, builds a wheel and
source distribution, checks metadata, and installs both artifacts in isolated
environments without Home Assistant. Release tags use `sma-net2-v<VERSION>` and
must match the version in `pyproject.toml`.

## License and attribution

GPL-3.0-or-later. The protocol adapts the GPLv3-or-later work of Wim Hofman and
Stephen Collier (2010–2011) in
[sma-bluetooth/sma-bluetooth](https://github.com/sma-bluetooth/sma-bluetooth).
All original notices and the complete GPL license are retained. This extraction
does not relicense the protocol. The optional BlueZ discovery uses the
OSI-licensed `dbus-fast` package.

## API and quality requirements

See [API.md](https://github.com/CReimer/sma-net2-bluetooth/blob/main/packages/sma-net2/API.md) for every public method, parameter/default, return model,
measurement/unit, error class, cancellation/cleanup guarantee, diagnostics,
use case, limitation, troubleshooting step and removal procedure.
[QUALITY.md](https://github.com/CReimer/sma-net2-bluetooth/blob/main/packages/sma-net2/QUALITY.md) maps the same Bronze/Silver/Gold/Platinum requirements used for
the HA adapter, including explicit frontend-only boundaries. Both use >=96%
line and branch coverage per runtime module and overall. Wheel and sdist must
pass these gates separately in environments without Home Assistant.

Version 0.1.1 adds privacy-safe `client.diagnostics()`, bounds socket sends by
the configured timeout, wraps socket errors consistently and closes cancelled
connections. It also stops already-started discovery on partial adapter failure.
The tests cover these lifecycle and error contracts without radio access.
