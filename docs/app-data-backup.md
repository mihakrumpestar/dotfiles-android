# App data backup without root

Target: Android 17, stock firmware, no root. The tools we have are `adb shell` (runs as the `shell` user, uid 2000) and Shizuku-Next, which gives apps the same shell-level access. Neither is root: `/data/data/<pkg>` stays unreadable.

Short version:

- The reliable way to back up an app is the app's own export and import.
- The reliable automatic way is copying the files the app keeps in `/sdcard/Android/data/<pkg>/`.
- Android's system backup (`bmgr`) is real, but it only covers apps that allow it, within small size limits. A printed `Success` does not prove data was stored.
- Root-only tools are the only way to copy everything.

## Options, best first

| # | Method | Root | What it restores | Automation |
|---|---|---|---|---|
| 1 | The app's own export/import (JSON, ZIP, vCard, SAF file) | No | Exactly what the developer chose to save, usually settings and sometimes content | Low, but needs UI taps; no standard intents |
| 2 | `adb pull` / `push` of `/sdcard/Android/data/<pkg>/` | No | Files the app keeps in external app storage (Obtainium's app list, configs, media) | Very low, plain file copy |
| 3 | `bmgr` with the built-in local transport | No | Apps with `allowBackup=true`, up to 25 MB of full data or 5 MB of settings | Low, one command, poor coverage |
| 4 | `bmgr` with the D2D flag (`is_device_transfer=true`) | No | On paper, also apps that opt out of backup, subject to their device-transfer rules | Low, but unproven on this phone |
| 5 | `bmgr` with the GMS D2D transport | No | Same idea as 4, but stored inside GMS and not inspectable | Low, and cannot be checked locally |
| 6 | `adb backup`, or App Manager's "ADB data" | No | Debuggable apps only since Android 12, which no store build is | Low, useless for real apps |
| 7 | `run-as`, exported content providers | No | Debuggable apps, or only what a provider chooses to expose | Low, near zero coverage |
| 8 | Root-only tools (DataBackup, Neo Backup, Swift Backup, App Manager internal data) | Yes | Everything, `/data/data` included | Low, but out of scope here |

## bmgr in plain terms

`bmgr` is Android's backup command. A transport is where the backup is stored. The phone has a built-in local transport (`com.android.localtransport`) that writes into its own app data, and `bmgr` can both write and read it:

```sh
adb shell bmgr backupnow <pkg>     # store a backup
adb shell bmgr restore 1 <pkg>     # restore it; the token is always 1
```

Why it often looks broken:

- `android:allowBackup="false"` in the app's manifest skips it on both backup and restore. This is the main gate.
- The local transport has small limits: 25 MB for full data and 5 MB for settings. An app over the limit is rejected.
- `bmgr list sets` always prints `1 : Local disk image`, even when nothing is stored. The line proves nothing.
- A full restore clears the app's data before writing (unless the app ships its own backup agent), so export first.
- A package only restores if it is installed, was actually stored, and passes version and signature checks.
- The honest status check is `dumpsys backup`: it lists `Ever backed up:` packages and the size of each transport's state.

Three more details:

- `bmgr fullbackup` still uses the normal cloud rules, so the D2D flag only helps `bmgr backupnow`.
- `com.android.encryptedlocaltransport` is a misleading name. It extends the local transport and copies raw bytes; the old encryption code was removed upstream. `is_encrypted=true` only sets a flag so apps that demand encryption agree to run.
- A restore is destructive. The local transport wipes the app's data first.

### The D2D trick

Setting this tells Android the backup is a phone-to-phone transfer:

```sh
adb shell settings put secure backup_local_transport_parameters 'is_device_transfer=true'
```

On paper, a device transfer ignores `allowBackup` for non-system apps targeting Android 12 or newer, as long as the app's `device-transfer` rules allow the files. Keep the setting in place during the restore too, because it also decides whether the stored set can be read. Quotas still apply.

It is still a hypothesis here. The one attempt on this phone used KDE Connect, which excludes every domain from device transfer anyway, so the rejection proves nothing. The first fair test is an app that sets `allowBackup="false"` without excluding everything.

The GMS `D2dTransport` is the same rule relaxation with storage owned by Google Play services. Nothing about it can be inspected, copied or verified from the host, so it is worse for a scripted workflow.

## adb backup, run-as, providers

- `adb backup` still exists, but since Android 12 it only backs up debuggable apps. Store builds are not debuggable.
- App Manager's "ADB data" calls the same API, so running it through Shizuku inherits the same restriction. App Manager's own README still lists app data as root-only.
- `run-as` only works for debuggable builds.
- `content query` can read exported providers, but unexported providers are always refused and no mainstream app exposes its preferences that way.

## Files under /sdcard/Android/data

The adb shell user is a member of `AID_EXT_DATA_RW`, so it can read and write `/sdcard/Android/data/<pkg>/files`. This is the cheapest real mechanism, and it is how Obtainium's app list is copied in this repo. At least one reported vendor build refuses it, so check first with `adb shell ls /sdcard/Android/data/<pkg>/files`.

## What decides bmgr coverage

- `android:allowBackup="false"` blocks cloud backup and ADB backup. It is ignored only for non-system apps targeting Android 12+ during a device transfer.
- `android:dataExtractionRules` (Android 12+) and the older `android:fullBackupContent` list what is included. Excluding every domain counts as opting out, even with `allowBackup="true"`.
- Check any app on the device: `dumpsys package <pkg> | grep -m1 pkgFlags` for `ALLOW_BACKUP`, and `aapt2 dump xmltree --file AndroidManifest.xml <base.apk>` for the rules.

## Measured on this phone

- With default settings, `bmgr backupnow com.kitsumed.shizucallrecorder` printed `Success`, and `dumpsys backup` then showed `Ever backed up: 1`. The local transport really stores data for an eligible app.
- `bmgr list sets` printed the same line before and after, as the source predicts.
- The D2D flag did not get KDE Connect through: `Transport rejected package`, and `Ever backed up` stayed 0. KDE Connect excludes every domain, so this was not a fair test.
- The restore half was not measured; judging it needs the app's own UI.
- The 25 MB limit is the practical blocker: OsmAnd's external files are 101 MB, and ShizuCallRecorder's recordings are already outside app data.

## Testing it on a phone

```sh
adb shell bmgr enable true
adb shell bmgr transport com.android.localtransport/.LocalTransport
adb shell bmgr backupnow <pkg>
adb shell dumpsys backup            # look for the package under "Ever backed up:"
adb shell bmgr restore 1 <pkg>
```

For a D2D attempt, set `backup_local_transport_parameters='is_device_transfer=true'` first, then `bmgr init` so the transport rereads it.

What the results mean:

- `Backup is not allowed`: the app is not eligible for this destination.
- `No available restore sets`: the token is wrong.
- Restore succeeds but nothing changes: the package was never stored. `Ever backed up:` confirms it.

## Other things that do not work

- `adb shell settings` holds platform settings only. No app mirrors its private preferences there.
- Samsung Smart Switch does not back up arbitrary app data.
- There is no `cmd backup` service; backup goes through `bmgr` and `bu`.
- `/data/data` is unreadable even for shell, so no command or service call reaches another app's private files.

## Root-only tools, for contrast

DataBackup, Neo Backup, Swift Backup and App Manager's internal-data features all require root for app data; their own docs say so. Seedvault needs to be built into the ROM because it holds `android.permission.BACKUP`. Shizuku gives shell-level access, not root, so it cannot cross that line.

## Bottom line

- If an app has an export, use it. `uiu` drives those exports and imports.
- If an app writes files under `/sdcard/Android/data`, copy them.
- `bmgr` is not a general backup tool. `allowBackup`, the 25 MB and 5 MB quotas, and a package that was never stored explain almost every "Success but nothing restored" report.
- The D2D flag is the only non-root idea that might carry an opted-out app, and it is not proven yet.
- Everything else is per-app work. An app that both opts out of backup and ships no export has no non-root path.

Checked against AOSP `main`, official Android docs and each project's repository in November 2026. App rules change, so verify the two manifest attributes on the device before relying on bmgr.

## Sources

- AOSP local transport and parameters: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/packages/LocalTransport/src/com/android/localtransport/
- AOSP backup service and eligibility rules: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/backup/java/com/android/server/backup/
- AOSP full backup rules: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/java/android/app/backup/FullBackup.java
- AOSP run-as: https://android.googlesource.com/platform/system/core/+/refs/heads/main/run-as/run-as.cpp
- AOSP adb shell groups: https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/daemon/main.cpp
- bmgr docs: https://developer.android.com/tools/bmgr
- Backup testing, including D2D: https://developer.android.com/identity/data/testingbackup
- adb backup restriction: https://developer.android.com/about/versions/12/behavior-changes-12#adb-backup-restrictions
- Manifest backup attributes: https://developer.android.com/guide/topics/manifest/application-element
- App Manager: https://github.com/MuntashirAkon/AppManager
- Shizuku: https://shizuku.rikka.app/guide/setup/
