# Contributing

Bug reports and pull requests are welcome. Before reporting a problem, update
to the latest release and include the Home Assistant version, inverter model,
configured NetID and connection mode. Redact passwords, Bluetooth addresses,
serial numbers and unrelated diagnostics.

Run the test suite before submitting a pull request:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -t .
```

Contributions must be compatible with `GPL-3.0-or-later`. Protocol changes
should include deterministic tests and document the hardware on which they were
verified.
