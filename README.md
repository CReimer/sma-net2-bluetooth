# SMA-Net2 Bluetooth for Home Assistant

An unofficial Home Assistant custom integration for reading legacy SMA solar
inverters over the local SMA-Net2 Bluetooth Classic protocol.

This project is independent and is not affiliated with, endorsed by, or
supported by SMA Solar Technology AG.

## Features

- automatic discovery through a local BlueZ adapter;
- direct NetID 1 connections and complete NetID 2–F inverter networks;
- power, energy, temperature, grid, DC/AC channel and status sensors when
  supplied by the inverter;
- warning, fault and grid-connection events;
- automatic daylight scheduling and serialized RFCOMM sessions;
- stable inverter devices and entity identities across topology changes;
- guarded Bluetooth-adapter recovery and Home Assistant Repairs issues;
- original five-minute inverter archive access;
- deterministic import of completed archive days into Recorder statistics.

## Requirements and limitations

- Home Assistant 2026.8.0 or newer;
- a local Linux Bluetooth adapter available to Home Assistant through BlueZ;
- an SMA inverter with the legacy SMA-Net2 Bluetooth interface;
- the SMA user password;
- a correctly configured Home Assistant location for sunrise and sunset.

This integration uses Bluetooth Classic RFCOMM, not Bluetooth Low Energy.
ESPHome and other BLE proxies cannot carry these connections. Home Assistant
Container installations must expose the host D-Bus socket and grant the
Bluetooth permissions required by Home Assistant. The integration intentionally
does not contact SMA cloud services.

The currently verified setup consists of two **SMA Sunny Boy SB 3000HF-30**
inverters in one NetID 2 network, running with Home Assistant Container 2026.9.0.
Other SMA-Net2 Bluetooth models may work, but should be treated as unverified
until their exact model and available measurements are reported.

Modern SMA devices with WebConnect should normally use Home Assistant's
[official SMA Solar integration](https://www.home-assistant.io/integrations/sma/).

## Installation with HACS

Until the repository is included in HACS by default, add it as a custom
repository:

1. Open HACS in Home Assistant.
2. Select **Integrations**.
3. Open the menu and select **Custom repositories**.
4. Add `https://github.com/CReimer/sma-net2-bluetooth` as an **Integration**.
5. Install **SMA-Net2 Bluetooth** and restart Home Assistant.
6. Go to **Settings > Devices & services > Add integration** and select
   **SMA-Net2 Bluetooth**.

For manual installation, copy `custom_components/sma_bluetooth` into the
`custom_components` directory of the Home Assistant configuration and restart
Home Assistant.

## Connection modes

- **Automatic** selects direct mode for NetID 1 and network mode for NetID 2–F.
- **Selected inverter only** talks only to the explicitly selected inverter.
- **Complete Bluetooth network** discovers the full NetID 2–F topology.

The setup flow probes the selected physical inverter and shows the detected
NetID, effective mode, root node and inverter count before creating the entry.
It never assumes that unrelated nearby SMA devices belong to the same plant.

## Archive actions

`sma_bluetooth.get_archive` returns original five-minute points without
interpolation or storage. `sma_bluetooth.import_archive` imports completed days
into the hourly Recorder statistics belonging to each total-energy sensor.
Existing timestamps are replaced deterministically rather than duplicated.

Archive and clock operations write Home Assistant statistics or inverter time.
Review the action description and diagnostics before invoking them manually.

## Bluetooth recovery

Every operation opens a fresh RFCOMM session behind one adapter-wide lock. After
repeated transport failures, the integration may power-cycle Home Assistant's
default local Bluetooth adapter and then retry once. This can briefly interrupt
other devices using that adapter. A persistent Repairs issue is created if
recovery fails.

## Privacy and bug reports

Diagnostics redact the SMA password. Before posting diagnostics or logs, also
review Bluetooth addresses, inverter serial numbers, plant names and topology
information. Never include passwords or unrelated Home Assistant configuration
in a public issue.

## Development

Run the tests against the pinned Home Assistant release:

```bash
python -m pip install -r requirements-test.txt
python tools/run_tests.py
```

The integration was developed with substantial assistance from generative AI.
All changes are reviewed, tested and released under the responsibility of the
maintainer.

## License and attribution

The independently authored Home Assistant adapter Python files marked
`SPDX-License-Identifier: Apache-2.0` use [Apache-2.0](LICENSES/Apache-2.0.txt),
matching Home Assistant Core. See the explicit
[component license boundary](custom_components/sma_bluetooth/LICENSE.md).

The standalone `packages/sma-net2` communication library and the temporarily
bundled `protocol.py`, `models.py` and `discovery.py` remain
[GPL-3.0-or-later](LICENSE). Files without an explicit exception also remain
under GPL-3.0-or-later. This repository currently has mixed licenses.

The Python SMA protocol implementation adapts work from
[sma-bluetooth/sma-bluetooth](https://github.com/sma-bluetooth/sma-bluetooth),
copyright Wim Hofman and Stephen Collier, 2010–2011. Its GPL license and
attribution remain intact. Package extraction does not remove GPL obligations
for combined distributions. The repository does not distribute the upstream
C source, reference binaries or private installation data.

## Trademark legal notice

All product names, trademarks and registered trademarks referenced by this
project or depicted in its images belong to their respective owners. Names,
marks and product imagery are used only to identify compatible products. Their
use does not imply endorsement of or affiliation with this project.

This notice follows the convention used by
[Home Assistant Brands](https://github.com/home-assistant/brands#trademark-legal-notices).

## Tests and coverage

Use Python 3.14 and the pinned Home Assistant test dependencies:

```bash
python -m pip install -r requirements-test.txt
python tools/run_tests.py
```

The suite mocks Bluetooth and external service access and exercises config
flows through Home Assistant’s real flow manager. Every integration Python module is included in coverage, including
modules not imported by tests. The test command and GitHub Actions both require
at least **91% line coverage and 91% branch coverage**, checked separately without
rounding. HTML, XML and JSON reports are written to `coverage-report/` and uploaded
as the `coverage` artifact by CI. Config-flow line and branch coverage must each
be 100%, enforced separately from the overall gate.

## Set up the connection

Set up during daylight: the inverters shut down at night and connection tests
intentionally wait until sunrise.

1. After installation, open **Settings > Devices & services > Add integration**
   and select **SMA-Net2 Bluetooth**.
2. Select the physical inverter from discovery, or enter its Bluetooth Classic
   address manually if discovery fails (format `AA:BB:CC:DD:EE:FF`).
3. Enter the **SMA user password**. This is not a Bluetooth pairing PIN and not
   the installer password. The client logs in with SMA's user role.
4. Select **Automatic**, unless you specifically want one inverter or a full
   network. Full network mode requires NetID 2–F.
5. Set a plant display name and polling interval. The default and minimum
   interval are 60 seconds. Longer intervals reduce radio traffic. Only daylight
   polling uses this interval; at night the next poll is scheduled for sunrise.
6. Review the detected NetID, mode, inverter serials and current root node,
   then submit the confirmation. Assign the devices to areas as desired.

If connection, authentication or discovery fails, correct the settings and
submit again. A failed connection test does not create an entry. Inverters
already owned by an existing entry cannot be configured a second time, even
through another Bluetooth node. For NetID 1, add each separate inverter in its
own entry.

To change settings, use the entry menu under **Settings > Devices & services >
SMA-Net2 Bluetooth > Reconfigure**. An empty password retains the stored
password. The new topology is tested and confirmed before changes are applied.
Connection settings are stored in entry data; display name, polling interval
and the identity cache are stored in entry options. Existing entries migrate
automatically without changing entity unique IDs or statistics.

## Use archive actions

Open **Developer tools > Actions** and select one of these actions. Operations
require daylight and a reachable inverter. If `config_entry_id` is omitted,
**all loaded SMA entries** are queried. Supply an entry ID to select one plant.
An unavailable or unknown selection produces an error rather than a silent
empty response.

### Read original archive points

`sma_bluetooth.get_archive` requires `start` (inclusive) and `end` (exclusive),
each with a timezone and aligned to a five-minute boundary. End must be after
start and the range may cover at most 62 days (allowing a daylight-saving hour).
`config_entry_id` is optional.

```yaml
action: sma_bluetooth.get_archive
data:
  start: "2026-10-01T00:00:00+02:00"
  end: "2026-10-02T00:00:00+02:00"
response_variable: archive
```

The required response contains `start`, `end` and `plants`, keyed by entry ID.
Each plant includes `requested_slots`, `complete` and `inverters`, keyed by
serial. Each inverter contains `count` and `points`; each point contains a Unix
`timestamp`, `total_energy_kwh` and `power_w` (which can be null). No statistics
are written. Different timestamp series between inverters cause an error;
`complete` reports whether all expected slots were returned. For today's data,
the expected series extends only through its latest returned point.

### Import completed days

`sma_bluetooth.import_archive` accepts `days` from 1 to 62 (default 3), counting
backwards from yesterday, and optional `config_entry_id`.

```yaml
action: sma_bluetooth.import_archive
data:
  days: 3
response_variable: imported
```

The optional response is `imported_hourly_statistics`, mapping each total-energy
entity ID to its imported hourly count. This action writes Recorder statistics;
existing hourly timestamps are replaced. It requires a complete five-minute
series for every managed inverter and registered total-energy sensors. It does
not fabricate missing readings. The previous completed day is also reconciled
automatically after a successful daylight poll. That reconciliation checks the
plant clock and can correct it within the protocol's safety limits.

## Triggers and conditions

The integration provides an **Events** entity for each inverter, with
`warning`, `fault`, `grid_connected` and `grid_disconnected` event types. Events
are emitted on observed status changes, not on every poll; short changes
between polls may not be observed. Warning/fault events carry `previous_status`
and `status`; grid events carry `previous_status` and the current `status`.

Use a Home Assistant state trigger on the inverter's event entity and check
its `event_type` attribute. Replace the example entity ID with your actual one.

```yaml
triggers:
  - trigger: state
    entity_id: event.sma_123_events
conditions:
  - condition: template
    value_template: "{{ trigger.to_state is not none and trigger.to_state.attributes.get('event_type') == 'fault' }}"
actions:
  - action: persistent_notification.create
    data:
      title: SMA inverter fault
      message: "Check the inverter and its status sensor."
```

There are no integration-specific device triggers or conditions. Standard
Home Assistant state, numeric-state and template conditions can use the power,
energy and status sensors. Sleeping inverter entities become unavailable.

## Remove the integration

1. Open **Settings > Devices & services > SMA-Net2 Bluetooth**.
2. Open the menu of the entry to remove and select **Delete**. Repeat for each
   plant entry if removing the entire integration.
3. Remove or update dashboards, automations and Energy dashboard references
   that use its entities. Deleting an entry does not erase Recorder history.
4. To uninstall the custom integration, remove it through HACS, or remove the
   `custom_components/sma_bluetooth` folder for a manual installation, and
   restart Home Assistant.

Removal ends polling and cancels the entry's background work. It does not
reset inverter settings or change the physical NetID. When resolving duplicate
legacy entries, follow the Repairs instructions to preserve registry ownership.

## Quality scale status

The implementation is being checked against the Home Assistant
[Bronze checklist](https://developers.home-assistant.io/docs/core/integration-quality-scale/checklist/).
See [quality_scale.yaml](custom_components/sma_bluetooth/quality_scale.yaml)
for rule-by-rule evidence and remaining upstream work. This is a **custom
integration**, not an officially awarded Bronze integration. The official
rating requires Home Assistant core inclusion and review. Local brand assets
are supplied for modern Home Assistant; an upstream `sma_bluetooth` brands
entry still needs to be accepted for the official checklist. Core also requires
a published communication library: [packages/sma-net2](packages/sma-net2) now
prepares the independent client, tests and release pipeline, but publication and
the integration dependency switch are still outstanding.
