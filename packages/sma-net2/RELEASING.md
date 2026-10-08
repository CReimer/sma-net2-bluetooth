# Release setup

The library is not yet published. Its first release needs a PyPI project owner;
the maintainer currently has no PyPI account. Builds and local verification do
not require a PyPI account, token or credentials.

## One-time account and publisher setup

1. Create an account at https://pypi.org/account/register/, verify the email
   address, and configure the authentication required by PyPI.
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

## Integration transition after publication

The custom integration intentionally continues to use its existing local
protocol implementation while the library is unpublished. Do not add an
unresolvable PyPI requirement to a released integration.

After successful PyPI installation is verified:

1. Replace integration imports of protocol, model and discovery implementation
   with `sma_net2` imports, and remove the inline implementations.
2. Import shared connection-mode and role constants from `sma_net2.const`.
3. Pin `sma-net2==0.1.0` in the integration manifest. Keep Home Assistant-only
   scheduling, adapter recovery, ownership, archive-to-Recorder conversion and
   entity code in the integration.
4. Run the full existing integration/flow suite against the published package
   and retain the enforced 100% config-flow and existing overall coverage gates.
5. Recheck dependency transparency, upstream branding, core tests/documentation
   structure and contribution requirements before requesting the official tier.

This release preparation does not award a Home Assistant quality tier or
resolve Home Assistant core review. It does not change or relicense the
protocol's GPL-3.0-or-later notices.
