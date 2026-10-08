# Public API and operational contracts

This client is independent of Home Assistant. Import public types from
`sma_net2`; discovery is in `sma_net2.discovery`, constants in `sma_net2.const`.
The maintainer is [CReimer](https://github.com/CReimer); report reproducible
problems at the [issue tracker](https://github.com/CReimer/sma-net2-bluetooth/issues).
Python 3.11–3.14 are supported; CI runs the oldest and newest versions.

## Client and session lifecycle

`SMAClassicClient(address, password, timeout=10, connection_mode="auto")` takes
one physical Classic Bluetooth address (`AA:BB:CC:DD:EE:FF`), SMA user password,
per-socket-operation timeout in seconds, and `auto`, `single` or `network`.
`auto` selects direct mode on NetID 1 and network mode on NetID 2–F. `single`
always queries the selected inverter; `network` rejects NetID 1 with a typed
configuration error. `SMANetworkModeError.net_id` identifies the detected NetID.
Invalid mode/address syntax raises `ValueError` before any connection attempt.
Use a positive finite timeout and a valid six-byte address.

The async context manager opens a nonblocking RFCOMM socket (channel 1) and
closes it on exit, including exceptions. Failed/cancelled connection attempts
also close their socket. Each send/receive is bounded by `timeout`; individual
protocol queries can involve multiple packets and retries, so applications
needing a whole-operation deadline should additionally use `asyncio.timeout`.
Cancellation propagates; it is never turned into a connection/authentication
failure. Do not call operations concurrently on one client or share an adapter
between unsynchronized clients. There is no background polling or global state.

`async_start_session()` initializes the selected topology, identifies inverters,
reads per-inverter signal and authenticates. It is idempotent while active.
`async_stop_session()` logs off, resetting authentication even if logoff fails;
it leaves transport closure to the context manager. The three convenience
methods below start a session and stop it in `finally`. Their `_active` variants
require an already authenticated session and support grouped operations.

| Method | Result / side effects |
| --- | --- |
| `async_query()` / `async_query_active()` | `dict[str, SMAInverter]`, indexed by actual serial. Reads all participants of the current session; fresh models are produced each query. |
| `async_read_archive(periods)` / `async_read_archive_active(periods)` | `dict[str, list[SMAArchivePoint]]`. Reads original five-minute archive records, without statistics, interpolation or counter repair. |
| `async_sync_clock(timezone_offset, dst_active, *, lower_limit=60, upper_limit=3600, minimum_set_interval=86400)` / active variant | `SMAClockSyncResult`. Explicitly checks and may write plant time, then verifies the result. |
| `diagnostics()` | JSON-serializable summary of modes, NetID, transport/session state, device count, models, software and available measurement keys. Excludes credentials, names, serials, addresses and measurement values. No I/O. |

After initialization, `net_id`, `effective_mode`, `current_root` and `devices`
reflect the session. `current_root` is a serial or Bluetooth address and is
sensitive; do not include it in public diagnostics. `devices` contains internal
protocol state; use the returned public models for application code. Root
changes never change inverter serial identity. Start a new session to refresh
topology; applications must reconcile added/absent inverters from the returned
serial mapping. Radio absence alone does not prove permanent removal.

## Measurements and units

`SMAInverter` includes `serial`, optional `susy_id`, `name`, `model`,
`software_version`, `bluetooth_address`, `network_role`, `record_timestamp`,
`record_received_at`, and `values`. Roles are `direct`, `root_node`, or
`participant`. All metadata can be missing until reported by the device.
`record_clock_difference` is signed source timestamp minus receive timestamp,
or `None` if either observation is missing. Timestamps are Unix seconds.

| Value keys | Units / interpretation |
| --- | --- |
| `ac_power_total`, `dc_power_total` | W; DC total sums available MPPT power readings. |
| `energy_today`, `energy_total` | kWh; raw cumulative counters may roll back or reset. |
| `temperature` | °C |
| `frequency` | Hz |
| `operation_time`, `feed_in_time` | h |
| `dc_power_N`, `dc_voltage_N`, `dc_current_N` | W / V / A per reported MPPT channel |
| `ac_power_N`, `ac_voltage_N`, `ac_current_N` | W / V / A per phase 1–3 |
| `status`, `relay_status` | SMA tag text; unknown active tags remain numeric text. |
| `bt_signal` | Percent per inverter, not dBm. |

A missing key is unsupported/not yet reported; a `None` value is unknown or an
SMA missing-value sentinel. Do not replace either with zero. Source timestamps,
counter decreases and record order are preserved. The library does not decide
which values an application displays, stores, translates or disables.

## Archive and clock examples

Archive `periods` is a list of `(start, end)` Unix timestamp pairs (inclusive
start, exclusive end), or integer starts representing 24 hours. Use timezone-
aware datetimes, five-minute alignment, positive timestamps and bounded day
ranges. Firmware retention and gaps vary. The caller validates a requested
whole range and decides whether partial results are acceptable.
`SMAArchivePoint(timestamp, total_energy_kwh, power_w)` preserves transfer order.
Power is derived from adjacent valid counters/timestamps and can be `None`;
backwards counters are not hidden. Duplicate/missing slots are not filled.

```python
from datetime import datetime, timezone
from sma_net2 import SMAClassicClient, SMAProtocolError

async def read_day(address: str, password: str):
    start = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    try:
        async with SMAClassicClient(address, password) as client:
            return await client.async_read_archive([(start, start + 86400)])
    except SMAProtocolError:
        # The caller decides when to retry or report an error; never fabricate data.
        raise
```

`timezone_offset` is the standard UTC offset in seconds, with DST supplied
separately. Clock changes are explicitly invoked; reads never synchronize time.
`SMAClockInfo` contains `current_time`, `last_time_set`, `timezone_offset`,
`dst_active`, `set_count`. `SMAClockSyncResult` contains `before`, optional
`after`, `difference_seconds`, `adjusted`, `reason`. The absolute difference is
compared to the default bounds: <=60 seconds returns `within_tolerance`, >=3600
returns `unsafe_difference`, and a previous change less than 86400 seconds ago
returns `recently_adjusted`. Otherwise time is written and reread after one
second; a remaining difference over five seconds raises `SMAProtocolError`.
Changing these limits is an application decision with physical side effects.

## Discovery

`await async_discover_sma_devices(timeout=8)` performs an active BlueZ BR/EDR
inquiry on local adapters for the given wait duration in seconds. It returns a
sorted mapping of uppercase addresses to names beginning with `SMA` (including
cached BlueZ devices), not proof of authenticated compatibility or shared
NetID. The caller must test the selected inverter with its password. No local
adapter or D-Bus permission yields `SMADiscoveryError`.
The discovery routine stops every successfully started adapter on success,
failure or cancellation, even if starting a later adapter fails, and disconnects
its D-Bus connection. Serialize discovery with other adapter work. There are no
BLE advertisements, ESPHome proxy support, IP discovery or cloud connections.
A manual address remains a valid fallback when inquiry is unavailable.

## Errors, availability and troubleshooting

| Exception / symptom | Meaning and caller action |
| --- | --- |
| `SMAAuthenticationError` | Incorrect user password; request updated credentials, do not power-cycle the adapter. |
| `SMAConfigurationError`, `SMANetworkModeError` | Incompatible mode/topology; obtain user confirmation before changing configuration. |
| `SMATransportError` | Closed connection, socket error or timeout. Retry with a fresh client under a bounded application policy. Socket errors preserve `__cause__`. |
| `SMAProtocolError` | Base class of the above; invalid framing/checksum, unsupported firmware, query errors or unverified clock write. Do not treat arbitrary protocol errors as transport faults. |
| `SMADiscoveryError` | Independent discovery error with cause; check local BlueZ/D-Bus permissions or allow manual address selection. |
| `asyncio.CancelledError` | Caller cancellation; propagates after cleanup. |
| No measurements at night | Many legacy inverters sleep without solar power. The caller should schedule daylight operation rather than continuous retries. |
| Missing measurement/model support | Check the known SB 3000HF-30 evidence in README; other devices may report different records. Unsupported values remain absent/unknown. |
| Archive gaps or unsafe clock difference | Inspect physical device time/history; do not fabricate archive records or bypass bounds to make results appear complete. |

The library emits no polling success/failure log on every operation and never
logs passwords or raw authenticated packets. The caller owns availability
transitions, localized user messages, retry/backoff, adapter recovery and
permanent device removal. Typed exceptions support this without parsing English
text. `diagnostics()` supplies a safe initial report; review separately captured
logs and raw public model data for serials/addresses/names before posting them.
Include library/Python versions, inverter model, mode, NetID, daylight state,
exception class and a minimal reproduction in an issue. Never include passwords.

## Uninstallation and verification

`python -m pip uninstall sma-net2` removes the library. Stop its calling service
and close clients first. There are no persistent library settings or database
records; uninstalling does not reset inverter settings or undo clock writes.

From the repository root run `python tools/run_library_tests.py`. This tests
actual package source, without importing HA, and enforces >=96% lines and
branches separately per module and overall, using unrounded counts. Build with
`python -m build packages/sma-net2`; then `python tools/verify_library_dist.py`
installs wheel and sdist separately without HA and runs the same tests and gates
against each installed artifact. CI runs Python 3.11 and 3.14 before publishing.
