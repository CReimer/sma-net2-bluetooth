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
python -m unittest discover -s tests -t .
```

The integration was developed with substantial assistance from generative AI.
All changes are reviewed, tested and released under the responsibility of the
maintainer.

## License and attribution

The source code is licensed under the
[GNU General Public License v3.0 or later](LICENSE) (`GPL-3.0-or-later`).

The Python SMA protocol implementation adapts work from
[sma-bluetooth/sma-bluetooth](https://github.com/sma-bluetooth/sma-bluetooth),
copyright Wim Hofman and Stephen Collier, 2010–2011, also licensed under
GPL-3.0-or-later. The repository does not distribute the upstream C source,
reference binaries or private installation data.

## Trademark legal notice

All product names, trademarks and registered trademarks referenced by this
project or depicted in its images belong to their respective owners. Names,
marks and product imagery are used only to identify compatible products. Their
use does not imply endorsement of or affiliation with this project.

This notice follows the convention used by
[Home Assistant Brands](https://github.com/home-assistant/brands#trademark-legal-notices).
