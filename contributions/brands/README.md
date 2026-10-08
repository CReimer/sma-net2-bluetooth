# Prepared Home Assistant branding contribution

This is a local contribution artifact, not an upstream submission or acceptance.
The proposed Core domain is `sma_bluetooth`. Existing SMA artwork can be reused
without duplicating binary files, using the relative symlink in the patch.

After personally reviewing the patch, prepare a branch from current
home-assistant/brands master and apply it:

```bash
git apply --check /path/to/contributions/brands/sma_bluetooth.patch
git apply /path/to/contributions/brands/sma_bluetooth.patch
```

Inspect `git diff --stat`, `git diff --summary`, and verify
`core_integrations/sma_bluetooth/icon.png` resolves to the existing SMA icon.
Use PR_BODY.md with the current upstream template, linking the corresponding
Core integration PR and its website documentation. Complete only checklist
items you personally verify. Submit this as a Core integration brand addition,
not a custom integration addition: new custom entries are no longer accepted.

The current integration is still custom and has not received official Bronze.
Its bundled local brand assets remain appropriate until Core inclusion.
The upstream brands rule stays TODO until the contribution is accepted.

References:
- https://github.com/home-assistant/brands#using-the-same-logo--icon-for-different-brands
- https://developers.home-assistant.io/docs/ai_policy/
- https://developers.home-assistant.io/docs/core/integration/contributing_to_core/
