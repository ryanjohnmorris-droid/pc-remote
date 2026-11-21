# Pocket Wallet for iPhone (native build)

This folder wraps the Wallet web app in `../wallet` as a native iPhone app using Capacitor.

The GitHub Actions workflow `.github/workflows/wallet-ios.yml` builds an **unsigned** `.ipa`
on a free macOS runner and attaches it to the `wallet-ios` release. Install it with
SideStore or AltStore, which sign it with your own Apple ID.
