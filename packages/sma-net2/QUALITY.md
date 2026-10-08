# Standalone library mapping of the same Bronze/Silver/Gold checklist

This is a local quality audit, not a Home Assistant tier award. The same
engineering and documentation requirements apply to this package. Rules that
literally depend on HA frontend/registries are assigned to the adapter, with
the corresponding library contract made explicit rather than importing HA.

All runtime modules and both installed distributions must satisfy >=96% line
and branch coverage separately. The protocol public API, discovery, cancellation
and source-fidelity tests run without HA on Python 3.11 and 3.14.

| Rule | Library evidence / boundary |
| --- | --- |
| `action-setup` | No global registration or I/O on import; methods explicitly invoked on the client. |
| `appropriate-polling` | No background polling; API.md documents daylight, serialization and caller retry/whole-operation deadlines. |
| `brands` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `common-modules` | Protocol, models, constants and BlueZ discovery are separate; no HA import. |
| `config-flow-test-coverage` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `config-flow` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `dependency-transparency` | GPL-3.0-or-later source, attribution, dbus-fast dependency, typed package, wheel/sdist and public CI. |
| `docs-actions` | API.md documents read/archive/clock operations, parameters, returns and write effects. |
| `docs-triggers` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `docs-conditions` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `docs-high-level-description` | README describes legacy SMA-Net2 local Classic client and feature boundaries. |
| `docs-installation-instructions` | README covers Python/Linux/Classic/BlueZ requirements and pinned installation. |
| `docs-removal-instructions` | API.md documents pip uninstall, resource closure and inverter side effects. |
| `entity-event-setup` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `entity-unique-id` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `has-entity-name` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `runtime-data` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `test-before-configure` | Constructor validates mode/address syntax without I/O; caller explicitly authenticates the selected physical inverter before saving application configuration. |
| `test-before-setup` | Session startup validates firmware, topology, identity and credentials before operations; tests/test_protocol_sessions.py. |
| `unique-config-entry` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `action-exceptions` | Typed protocol/auth/configuration/transport/discovery failures retain causes. Cancellation propagates; session wrapper tests. |
| `config-entry-unloading` | Corresponding library lifecycle: async context manager closes sockets on exit and failed/cancelled connects; discovery stops all started adapters and closes D-Bus; quality tests. |
| `docs-configuration-parameters` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `docs-installation-parameters` | README and API.md list constructor defaults, modes, timeouts, credentials and Linux/BlueZ prerequisites. |
| `entity-unavailable` | Corresponding data contract: no fabricated zero/cache recovery; failures raise, sentinels are None, absent values are omitted. Caller owns HA availability. |
| `integration-owner` | CReimer is named in API.md and repository CODEOWNERS with issue tracker. |
| `log-when-unavailable` | No background availability state; typed errors returned to caller, no repeated polling log. Privacy-safe diagnostics are explicit. |
| `parallel-updates` | API.md requires serialization per client and shared adapter. No hidden tasks or concurrent packet consumers. |
| `reauthentication-flow` | Corresponding credential contract: SMAAuthenticationError is distinct, a new client accepts new credentials, stable serial metadata does not depend on password; session tests. |
| `test-coverage` | Same >=96% lines AND branches per module and overall, enforced by tools/run_library_tests.py and on both installed distributions by tools/verify_library_dist.py; Python 3.11/3.14 CI. |
| `devices` | SMAInverter exposes serial-based identity, model, software, address and root/participant roles; protocol/query tests. HA registry creation belongs to adapter. |
| `diagnostics` | SMAClassicClient.diagnostics() gives JSON-compatible state without credentials, serials, addresses, names or measurement values; quality tests. |
| `discovery-update-info` | Each new session redetects topology/root by actual identity. Classic hardware addresses do not rotate; endpoint selection is explicit. |
| `discovery` | Active local BlueZ Classic inquiry with manual-address fallback, authentication still required; tests cover success/error/cancellation/partial-adapter cleanup. No HA BLE discovery API is used. |
| `docs-data-update` | API.md documents explicit fresh sessions/queries, missing keys vs None, raw source counters/times, caller polling and dynamic topology. |
| `docs-examples` | README async measurements and API.md bounded archive example; use cases and safety limits documented. |
| `docs-known-limitations` | README/API.md document verified model, local Classic-only, night sleep, no proxy/cloud, archive gaps and caller concurrency. |
| `docs-supported-devices` | README names SB 3000HF-30; other legacy models explicitly unverified. |
| `docs-supported-functions` | API.md enumerates every public method/model, measurements/units, archive and guarded clock writes. |
| `docs-troubleshooting` | API.md symptom/error table, daylight/BlueZ checks and privacy-safe issue reporting. |
| `docs-use-cases` | README/API.md cover production monitoring, archive reading and explicitly guarded clock maintenance. |
| `dynamic-devices` | Every new authenticated session rebuilds topology and queries current participants; returned serial maps support application reconciliation. No static library-side registry or cached success. |
| `entity-category` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `entity-device-class` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `entity-disabled-by-default` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `entity-translations` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `exception-translations` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `icon-translations` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `reconfiguration-flow` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `repair-issues` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
| `stale-devices` | HA-specific: implemented and tested in the adapter audit. Library exposes typed models/exceptions and explicit lifecycle/configuration; it does not create HA flows, entities, registries, translations or Repairs. See API.md for the caller contract. |
