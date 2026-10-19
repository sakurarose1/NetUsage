# Changelog

## 1.4 (2026-10-19)

- **Compare periods**: a new tab on the dashboard that puts two periods side by side: download, upload, total and daily average with the change in GB and %, a plain-language "What changed" list (biggest increases and drops, new apps, apps you stopped using, busiest days, which network you used more), a running-total chart, and every app's and network's change.
- A period that has just started is compared fairly with the **same number of days** of the one before (or switch to whole periods).
- **Copy summary** puts the comparison on the clipboard as text, ready to paste into a chat or a note.
- **Renewal summary**: on the day your package renews, a notification tells you how much the period that just ended used, how it compares with the one before, and which app changed most. Clicking it opens the comparison.
- The renewal day is now its own setting ("Your internet package renews on day …") and is used for the dashboard's periods, the comparison and the data limit.

## 1.3 (2026-10-16)

- **Catches up right after your PC wakes**: NetUsage now also runs about 2 minutes after the PC wakes from sleep (including Modern Standby), after sign-in, and 3 minutes after start-up, instead of waiting for the next hourly run.
- **Clearer "last updated" notice**: when the page is a few hours old (usually because the PC was asleep or off), the dashboard says so plainly instead of suggesting the background task is broken.
- **The dashboard reloads itself** once newer numbers are likely ready, but never while you're using it. It keeps the month you were looking at.
- **"Update numbers now"** button in NetUsage Settings.
- Wake-up runs are skipped if NetUsage already ran in the last 5 minutes.

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
