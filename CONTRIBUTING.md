# Contributing

Bug reports and pull requests are welcome. Before reporting a problem, update
to the latest release and include the Home Assistant version, inverter model,
configured NetID and connection mode. Redact passwords, Bluetooth addresses,
serial numbers and unrelated diagnostics.

Run the test suite before submitting a pull request:

```bash
python -m pip install -r requirements-test.txt
python tools/run_tests.py
```

Contributions to the adapter files marked `Apache-2.0` must use Apache-2.0.
The communication library, bundled communication modules and other files
without an explicit exception remain GPL-3.0-or-later. Preserve the SPDX
identifier and upstream notices of each file; see the component's LICENSE.md
for the boundary. Protocol changes should include deterministic tests and
document the hardware on which they were verified.
