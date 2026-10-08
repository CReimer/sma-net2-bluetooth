# Upstream contribution preparation

These artifacts are prepared for the maintainer's personal review. They have
not been submitted to Home Assistant and do not establish an official tier.
Home Assistant's AI policy requires contributors to understand and review their
changes before submission and forbids autonomous contributions:
https://developers.home-assistant.io/docs/ai_policy/

## Branding

`brands/sma_bluetooth.patch` adds a relative symlink to the existing SMA assets.
See its README and verification evidence before using the preserved PR template.

## Core submission requirements still outstanding

1. Configure the pending PyPI publisher for owner `C-Reimer`, publish the reviewed
   `sma-net2` release, and test the integration against the published artifact.
   See `packages/sma-net2/RELEASING.md`.
2. Prepare the Core contribution in the current Core development tree and run
   its own checks. Existing custom-component tests and hassfest success do not
   prove that the Core contribution passes. Adapt tests to Core fixtures and
   module paths and include generated dependency/CODEOWNERS changes.
3. Follow the initial integration scope: one sensor platform, without custom
   actions, diagnostics, event platform, reauthentication or reconfiguration in
   the first PR. This is an upstream submission sequence; preserve the full
   custom integration and add the remaining features in subsequent Core PRs.
4. Prepare matching home-assistant.io documentation for that actual initial
   sensor-only scope, including Bluetooth Classic requirements, setup, location
   and daylight behavior, available sensors, limitations and removal. Do not
   advertise custom-only actions/events as implemented in the initial Core PR.
5. Personally review and understand the actual patches and PR descriptions.
   Submit Core, website and brands contributions with cross-links and respond
   to maintainer questions in the contributor's own words.
6. Obtain upstream acceptance and verify the resulting Core quality-scale tier.
   The custom repository's local audit alone cannot award official Bronze.

Source for initial PR scope and requirements:
https://developers.home-assistant.io/docs/core/integration/contributing_to_core/
