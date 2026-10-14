# Changelog

## 1.2 (2026-10-14)

- **Update check**: once a day NetUsage asks GitHub for the latest release. When there is a newer version, the dashboard shows a Download button and you get a notification. Can be switched off.
- **Spike alerts**: a notification when an app uses at least 2 GB in a day (adjustable) and more than 3× its usual daily amount.
- **Optional monthly data limit** (off by default): set your package size, what it counts, the reset day and a warning level. The dashboard shows a progress bar, and you get notifications at the warning level and at 100%.
- **App history**: click any app in the dashboard to see its usage month by month and day by day.
- New **NetUsage Settings** window (desktop and Start menu) with a test-notification button and "Check for updates now".
- Recent alerts are listed at the top of the dashboard.
- New background task **NetUsage Notifier** shows the notifications in your own sign-in session.

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
