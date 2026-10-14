# Changelog

## 1.1 (2026-10-14): first public release

- Released as free software under the GNU AGPL v3 or later.
- Fixed double counting: Windows' hourly per-network total rows are no longer added on top of the per-app rows. Totals now match Windows Settings.
- Mobile Hotspot and Wi-Fi Direct adapters are treated as virtual and start unticked, so the same traffic isn't counted twice.
- If the shadow copy of the usage database fails, NetUsage retries by briefly restarting the Diagnostic Policy Service.
- Works with 32-bit Python on 64-bit Windows.
- The dashboard footer shows the license and a link to the source code.
- `Install.bat` downloads the helper libraries if they are missing (the release zip already includes them).

## 1.0 (2026-10-08)

- First version: hourly collector, permanent history, dashboard with months, apps and networks.
