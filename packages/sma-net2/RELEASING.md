# Release setup

## Current release: 0.1.2

Published at https://pypi.org/project/sma-net2/0.1.2/ through Trusted Publishing.
Tag `sma-net2-v0.1.2` points to `3547e76f242ef70f8d687e247130620fed63f456`.
Publish/test run: https://github.com/CReimer/sma-net2-bluetooth/actions/runs/37757033154.

Published wheel and sdist were downloaded from PyPI and matched the tested CI
artifacts byte for byte. Both pass isolated strict source/API typing, rejection
of invalid callers, and all 42 runtime tests without Home Assistant installed.
Coverage: 692/693 lines (99.86%), 248/254 branches (97.64%), with separate
96% floors overall and per module. Python 3.11 and 3.14 source checks pass.

- Wheel SHA256: `a74b8dd972a751b7860607fee0c9e52525a2bba1f5579a2fe2b7d2c7b1d691e7`
- Sdist SHA256: `fb7a8cb17a18a4fcfc90c4a7439a914ff93d9315a35bdf24f8129178d5771275`

Integration 0.3.1 pins the published `sma-net2==0.1.2` package.

## Previous release: 0.1.1

Published at https://pypi.org/project/sma-net2/0.1.1/ by the existing Trusted
Publisher. Release tag `sma-net2-v0.1.1` points to
`f6948720e525e646cfa92b4d69e345238d345eea`; successful publish workflow:
https://github.com/CReimer/sma-net2-bluetooth/actions/runs/37754044993.

The workflow runs 41 standalone tests on Python 3.11 and 3.14 and enforces the
same >=96% lines and branches separately for every runtime module and overall.
Both isolated wheel and sdist installations pass the same gates without HA:
99.85% lines (674/675), 97.64% branches (248/254). PyPI downloads matched the
tested release artifacts byte-for-byte and were verified again in isolation.

- Wheel SHA256: `054dccb892564b8a178a9cf1923c94601db76098d770bcf2bea725d016d08869`
- Source SHA256: `6435205ddcc047b55c87b714c0259f50f0021bbc028970e4fbf52ad04cb82a56`

The integration pins `sma-net2==0.1.1`. The initial-release record below is
retained as history; subsequent releases must always get a new version/tag.

## Initial release: 0.1.0

Version `0.1.0` is published at https://pypi.org/project/sma-net2/0.1.0/.
The PyPI project owner is `C-Reimer`. GitHub Trusted Publishing uses the
publisher below; no API token is needed. The initial release tag is
`sma-net2-v0.1.0` and its successful workflow is
https://github.com/CReimer/sma-net2-bluetooth/actions/runs/37748199759.

Both PyPI files were compared byte-for-byte against the tested workflow
artifacts, installed in isolated environments without Home Assistant, and
checked with the standalone suite. The integration is verified against the
published pinned package.

## Publisher configuration (already completed)

1. Sign in to `C-Reimer` at https://pypi.org/ and complete any outstanding
   email verification and authentication required by PyPI.
2. On https://pypi.org/manage/account/publishing/, add a **pending publisher**
   for a new project with these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `sma-net2` |
   | GitHub owner | `CReimer` |
   | Repository | `sma-net2-bluetooth` |
   | Workflow filename | `sma-net2-library.yml` |
   | Environment | `pypi` |

   The PyPI username can differ from the GitHub owner. Do not create or share an
   API token: the workflow uses GitHub OIDC Trusted Publishing. Pending
   publishers allow the first upload to create the project. PyPI's naming rules
   and availability checks remain authoritative.
3. Merge the tested package workflow into the repository's default branch.
   The `pypi` GitHub environment may use deployment approvals if the repository
   owner wants them; the workflow only publishes versioned library tags.

Official instructions:
- https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
- https://docs.pypi.org/trusted-publishers/using-a-publisher/

## First release

From the reviewed commit containing the library:

```sh
python -m pip install build twine
python -m build packages/sma-net2
python -m twine check packages/sma-net2/dist/*
python tools/verify_library_dist.py
python tools/check_library_release.py sma-net2-v0.1.0
```

After the publisher is configured and all checks pass, the maintainer can
create and push the matching `sma-net2-v0.1.0` tag. The tag workflow tests Python
3.11 and 3.14, builds and verifies both distributions, uploads build artifacts,
and publishes those same artifacts to PyPI. The package version and tag are
checked before publication. Ordinary branch pushes, PRs and manual workflow
runs only test and build; they do not publish.

Confirm the published wheel and source distribution are available from PyPI,
match version 0.1.0, contain GPL attribution, and have public CI provenance.
Then validate installation in a fresh environment from PyPI, not the editable
checkout. Never overwrite a published version; bump the version and tag for
subsequent releases.

## Integration dependency

The custom integration imports the library API and shared constants and pins
`sma-net2==0.1.1` in its manifest. The bundled protocol/models/discovery modules
have been removed. Home Assistant scheduling, adapter recovery, ownership,
Recorder conversion and entity code remain in the integration. Integration CI
installs the published pinned package through `requirements-test.txt`.

For subsequent library versions, publish and verify the new version before
updating the integration manifest and test dependency. Run the full integration
suite and retain 100% config-flow coverage and the same per-module and overall gates.
This project remains a custom integration; no Core or brands submission is
intended. The local Bronze audit does not award an official Home Assistant tier.
The protocol remains GPL-3.0-or-later with its original attribution.
