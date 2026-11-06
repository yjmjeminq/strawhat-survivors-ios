# Gomu Gomu Survivors iPhone builds

Public build helper for Gomu Gomu Survivors 1.10.0 (20). The owner authorized public conversion of this existing repository on 2026-11-06. Releases contain sealed Unity iOS exports and completed unsigned IPA files; older releases retain their original versions.

The manual workflow uses the standard `macos-15` GitHub-hosted runner and Xcode 16.4. It verifies the input archive SHA256, every exported file, ARM64 iOS-device binaries and all Unity game data before publishing an unsigned IPA. Apple signing credentials are unnecessary for this build. iLoader performs the user's signing and installation afterward.

No game, simulator or device is launched by the workflow. No Apple credentials, signing certificates, provisioning profiles or saved GitHub credentials are committed. GitHub's job token is limited to this repository and is not persisted in the checkout. No automatic push or pull-request build is configured.
