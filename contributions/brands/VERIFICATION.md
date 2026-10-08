# Branding patch verification

Source: home-assistant/brands master commit `ae8930cf5f9afbabe8314b9c102a91b2b1793d18`.

The patch adds exactly one relative directory symlink:
`core_integrations/sma_bluetooth -> sma`. No existing file changes.
`git apply --check` and application succeeded in a fresh Git fixture.
All four files resolved through the new symlink and matched their source bytes.
This checks the patch and target assets; it does not replace upstream CI or review.

SHA-256 of existing upstream SMA assets:

- `icon.png`: `f6cfb7bd8adf166d1165465e3ed41d8b617aef196a9ceb629f2ce00703d20500`
- `icon@2x.png`: `85774b4574460ddc32bb689c09b2c30286fa20cdb078ff7bdc084ecb0b661374`
- `logo.png`: `33e8991a44b6c2451a095bef0747b43479344c300ae5ada16bf31bf206186e1e`
- `logo@2x.png`: `87bb9863c5a04377996af7afe15e83c1e7ddb488228a40f0d10fde62de0042fb`
