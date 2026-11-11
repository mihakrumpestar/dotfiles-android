# App data backup without root

Target: SM-A566B, Android 17 (API 37), stock firmware, no root. Only `adb shell` (uid 2000) and a Shizuku-Next service (same uid) are available. Everything below was checked against AOSP `main` source, official docs, and each project's own repo, not blog posts.

## Ranked mechanisms

| # | Mechanism | Needs root? | Works on API 37? | What it actually restores | Automation cost |
|---|---|---|---|---|---|
| 1 | App's own export/import (SAF file, JSON, vCard, ZIP) | No | Yes | Exactly what the developer chose to serialise, usually settings and sometimes user content | Low, but almost always needs UI taps; no documented intents or broadcasts |
| 2 | `adb pull`/`push` of `/sdcard/Android/data/<pkg>/` | No | Yes, shell holds `AID_EXT_DATA_RW` | Whatever the app keeps in external app-specific storage (Obtainium JSON, configs, media) | Very low, plain file copy, scriptable |
| 3 | `bmgr` (`backupnow` only) + local transport with `is_device_transfer=true` | No | Source says yes; measured once on this device and rejected, but for a subject whose rules predict rejection | `allowBackup` is ignored for non-system apps targeting 31+; `dataExtractionRules` still apply when they define a `device-transfer` section; local quotas 25 MB full and 5 MB key/value | Low, one key, and it must stay set while restoring |
| 4 | `bmgr` + `com.android.localtransport` (default parameters) | No | Yes, measured: `Ever backed up` incremented for an eligible app | Full data stream and key/value data only for apps with `allowBackup=true`, quotas 25 MB and 5 MB | Low (`bmgr backupnow`, `bmgr restore 1 <pkg>`), weak coverage |
| 5 | `bmgr` + GMS `D2dTransport` | No | Probably, GMS side | Same rule relaxation as row 3 but GMS owns storage, so nothing to inspect locally | Low, needs `backup_enable_d2d_test_mode`; restores only if the restore set reports device `D2D` |
| 6 | `adb backup` / App Manager "ADB data" | No | Yes | Only apps with `android:debuggable="true"` (or privileged with an `allowAdbBackup` property) | Low, but useless for store apps |
| 7 | `run-as` | No | Yes | Private data of debuggable builds only | Low, blocked for every store app |
| 8 | `content query` / exported providers | No | Yes | Only what a provider exports; no app exposes SharedPreferences this way | Low, near zero coverage |
| 9 | Root-only tools (DataBackup, Neo Backup, Swift app data, App Manager internal data) | Yes | Yes | Everything, `/data/data` included | Low, but out of scope here |
| 10 | Samsung Smart Switch, `settings` keys, `cmd backup` | No | Yes | No per-app private data, or nothing at all | Not viable |

## bmgr with the built-in local transport

The local transport is a real backup transport, not write-only telemetry. `LocalTransport.java` implements the whole restore surface: `getAvailableRestoreSets()` returns token `1` named `"Local disk image"` (L611-631), `nextRestorePackage()` reports `TYPE_KEY_VALUE` from `_delta/<pkg>` and `TYPE_FULL_STREAM` from a non-empty `_full/<pkg>` (L651-696), `getRestoreData()` replays the key/value blobs (L710-754), and `getNextFullRestoreDataChunk()` streams the tarball (L830+). `bmgr restore <token> <package>` therefore does attempt a real restore of shared prefs, databases and files, and `1` is the token to pass.

Whether it restores anything is decided before that. `UserBackupManagerService.fullTransportBackup()` builds its rules with `BackupDestination.CLOUD` (L2853), and `requestBackup()` derives the destination from the transport flags: anything that is not `FLAG_DEVICE_TO_DEVICE_TRANSFER` counts as `CLOUD` (L4167-4183). Under `CLOUD` rules, `isAppBackupAllowed()` returns `allowBackup` verbatim (`BackupEligibilityRules.java` L210-265), and `PerformUnifiedRestoreTask` re-checks the same rule before restoring a package (L282-289). So with default local parameters, `allowBackup="false"` apps are skipped on both backup and restore. `bmgr backupnow` printing `Success` only means the eligible packages succeeded; a rejected package prints `Package <pkg> with result: Backup is not allowed`.

Restore also requires that the package is installed, that `@pm@` metadata exists in the set, and that the version and signature checks pass (`PerformUnifiedRestoreTask.java` L282-289, L536-548, L661-716). A full-stream restore clears the app data first unless the app ships its own backup agent (`FullRestoreEngine.java` L382-403), so a restore is destructive and should be preceded by an export. `LocalTransport.getAvailableRestoreSets()` builds `RestoreSet` with the 3-arg constructor, so `backupTransportFlags` is 0 and `FLAG_SKIP_RESTORE_FOR_LAUNCHED_APPS` is never set: it restores into an app that has already been opened, unlike a transport that reports that flag.

Where the cache lives: `LocalTransport.makeDataDirs()` sets `mDataDir = mContext.getFilesDir()` (L116), so key/value blobs sit in `/data/data/com.android.localtransport/files/1/_delta/<pkg>/` and full tarballs in `.../1/_full/<pkg>`. The transport runs as `android.uid.system` (`packages/LocalTransport/AndroidManifest.xml`), so shell cannot read those files. The directory is ordinary app data, so it survives reboot, and it is wiped by `bmgr init <transport>` and by `bmgr enable false`, which records an init for every registered transport (`UserBackupManagerService.updateStateOnBackupEnabled` L3189-3232).

One detail explains most of the confusion: `getAvailableRestoreSets()` always appends `CURRENT_SET_TOKEN`, which is the constant `1`, whether or not anything has been written (L608-631). So `bmgr list sets` printing `1 : Local disk image` is not evidence that a backup exists, and `bmgr restore 1 <pkg>` will then run, find no `_delta` or `_full` entry for the package, and finish without an error. `dumpsys backup` is the honest check: `Ever backed up:` lists packages the service actually stored (L4127-4131), and each transport row shows the size of its state files (L4084-4087).

The one way to make the local transport ignore `allowBackup` is to tell it that it is a D2D transport: `adb shell settings put secure backup_local_transport_parameters 'is_device_transfer=true'`. `LocalTransportParameters` reads that key, `LocalTransport.getTransportFlags()` sets `FLAG_DEVICE_TO_DEVICE_TRANSFER` (L184-186), and both `requestBackup()` and `ActiveRestoreSession` then classify the operation as `DEVICE_TRANSFER`, where `isAppBackupAllowed()` forces non-system apps through. Path selection then comes from the `device-transfer` section of `dataExtractionRules`; only when that section is absent does `IGNORE_FULL_BACKUP_CONTENT_IN_D2D` discard the legacy `fullBackupContent` rules and back up everything (`FullBackup.java` L588-614). Keep the key set during the restore as well: `ActiveRestoreSession.getBackupEligibilityRules()` decides from the restore set device name, and `getAvailableRestoreSets()` only reports `D2D` while `isDeviceTransfer()` is true (L624-628), so the same flag both writes the set and authorises reading it back. Two catches: `bmgr fullbackup` still builds `CLOUD` rules because `UserBackupManagerService.fullTransportBackup()` hardcodes them (L2853), so only `bmgr backupnow` benefits; and the local quotas still apply, so any app whose data exceeds 25 MB (full) or 5 MB (key/value) is rejected by `checkFullBackupSize()`/`performBackup()`. The flag is gated by `FeatureFlagUtils.SETTINGS_USE_NEW_BACKUP_ELIGIBILITY_RULES`, whose AOSP default is `true` (`FeatureFlagUtils.java` L203). This is the only local mechanism in this document that restores data of an `allowBackup="false"` app without root (the GMS D2D transport is the same trick with vendor storage), and it is exactly the case that "bmgr reports Success but nothing comes back" reports miss.

`com.android.encryptedlocaltransport` is not needed and is not encryption. Both services are whitelisted transports in `data/etc/framework-sysconfig.xml` L49-52. Current `EncryptedLocalTransport.java` extends `LocalTransport` and its `performBackup()` just copies the raw file descriptor into `_delta/<pkg>` with `Files.copy`, with no cipher anywhere; the package is a leftover name for a privileged system-uid app. Historically it routed through `IntermediateEncryptingTransport` and `EncryptionKeyHelper`, where the key was a recoverable-keystore secondary key plus a per-package tertiary key; that framework code was deleted upstream ("encryption logic lives in GMS now", efoundation commit `2d8b102e`), so there is no key derivation to replicate today.

Practical limits: `is_encrypted=true` in `backup_local_transport_parameters` only sets `FLAG_CLIENT_SIDE_ENCRYPTION_ENABLED`, and `fake_encryption_flag` sets `FLAG_FAKE_CLIENT_SIDE_ENCRYPTION_ENABLED` (`LocalTransportParameters.java` L31-33, `LocalTransport.getTransportFlags()` L177-190); neither encrypts anything, they exist so agents that demand encryption still run. The official test script sets `is_encrypted=true` for exactly that reason (`developer.android.com/identity/data/testingbackup`, cloud section). Quotas are 25 MB full and 5 MB key/value (`LocalTransport.java` L86-87), `bmgr fullbackup` refuses to run before a key/value pass has written `@pm@` (`fullBackupAllowable()` L2075-2109), and the docs note `bmgr restore` does not work for encrypted backups (`developer.android.com/tools/bmgr`).

Evidence: `packages/LocalTransport/src/com/android/localtransport/LocalTransport.java`, `LocalTransportService.java`, `LocalTransportParameters.java`, `services/backup/java/com/android/server/backup/UserBackupManagerService.java`, `services/backup/java/com/android/server/backup/utils/BackupEligibilityRules.java`, `services/backup/java/com/android/server/backup/restore/{PerformUnifiedRestoreTask,FullRestoreEngine,ActiveRestoreSession}.java`, `cmds/bmgr/src/com/android/commands/bmgr/Bmgr.java`. No open Google issue tracker entry is needed to explain the "Success but nothing restored" reports: they are the eligibility and quota rules above, plus restoring an app that was never in the set.

## bmgr with the GMS D2D transport

This is the one bmgr path that ignores `allowBackup`. `BackupEligibilityRules.isAppBackupAllowed()` returns true for non-system apps when the destination is `DEVICE_TRANSFER` and the app targets API 31 or higher (L213-219, change `IGNORE_ALLOW_BACKUP_IN_D2D`, `EnabledSince(S)`); an app targeting 30 or lower still honours `allowBackup="false"` there, and system apps are never forced through. `ActiveRestoreSession.getBackupEligibilityRules()` picks `DEVICE_TRANSFER` when the restore set's device name is `D2D` (L305-323). Google documents driving it from a single device: set `backup_enable_d2d_test_mode 1`, select `com.google.android.gms/.backup.migrate.service.D2dTransport`, `bmgr init`, `bmgr backupnow`, reinstall the APK, then restore. Where the GMS transport stores the set is GMS internals and not auditable from AOSP, and the restore set's device name is whatever GMS writes, so treat the last step as "try it and check `bmgr list sets` for a `D2D` device". Source: `developer.android.com/identity/data/testingbackup` (D2D section), AOSP `BackupEligibilityRules.java`, `ActiveRestoreSession.java`.

## adb backup and App Manager "ADB data"

`adb backup` is still implemented (`cmds/bu/src/com/android/commands/bu/Backup.java` calls `IBackupManager.adbBackup`), and the shell uid holds the signature|privileged `android.permission.BACKUP` via `data/etc/privapp-permissions-platform.xml`. Since Android 12 the `ADB_BACKUP` eligibility branch only accepts debuggable non-privileged apps (`BackupEligibilityRules.java` L220-259, change `RESTRICT_ADB_BACKUP`), which no Play or F-Droid release build is. App Manager 4.1.0 added an "ADB data" backup flag that calls exactly this API (`BackupCompat.adbBackup`), so over Shizuku it inherits the same restriction. Its own README still lists "back up/restore apps with data" as root-only, and `BackupFlags.java` gates internal data on `SelfPermissions.canWriteToDataData()` and the ADB flag on holding `BACKUP` (L92-101). Maintainer statement: "That would require a rooted device" (issue #1550). Sources: `developer.android.com/about/versions/12/behavior-changes-12#adb-backup-restrictions`, `github.com/MuntashirAkon/AppManager` README, releases v4.1.0, issue 1550, `BackupFlags.java`.

## run-as, instrumentation, providers

`run-as` rejects any non-debuggable package (`system/core/run-as/run-as.cpp` L216-219). Instrumentation is worse: `ActivityManagerService.startInstrumentation()` requires the instrumentation APK to be signed with the target's signature (L14498-14513), so an `androidx.test` helper cannot be pointed at a foreign app. `content query` goes through `getContentProviderExternal()`, which needs `ACCESS_CONTENT_PROVIDERS_EXTERNALLY` (declared `protectionLevel="signature"` in `core/res/AndroidManifest.xml` L7142-7143, and shell holds it via `packages/Shell/AndroidManifest.xml`), and then the ordinary provider check: an unexported provider is always refused (`ContentProviderHelper.checkContentProviderPermissionLocked()` L1599-1647). So a non-exported settings provider is unreachable even for shell, and no mainstream app exports a preference store; the settings that do live in providers are the system ones (contacts, telephony, call log) that the apps themselves export.

## Files under /sdcard/Android/data

The shell user is started with `AID_EXT_DATA_RW` and `AID_EXT_OBB_RW` in its supplementary groups (`packages/modules/adb/daemon/main.cpp` L101-121), so `/sdcard/Android/data/<pkg>/files` is readable and writable over adb without root. This is the cheapest real mechanism and it is what makes Obtainium's whole app list copyable. Caveat: at least one Samsung build (S21, API 30) is reported to refuse `adb pull` of that path, so verify per device with `adb shell ls /sdcard/Android/data/<pkg>/files`.

## Other options that do not work

`adb shell settings` holds platform settings only; no app mirrors its private prefs into it. Samsung Smart Switch backs up APKs and its own curated data, not arbitrary app data ("Smart Switch mobile does not support application data backup or restore", Samsung support). There is no `cmd backup` service; backup is reached through `bmgr` and `bu` only. `/data/data` stays unreadable even for shell, so no `cmd` or `service call` path exists into another app's private directory.

## Backup rules and who opts out

Three attributes decide bmgr coverage. `android:allowBackup="false"` blocks cloud backup outright, and under `ADB_BACKUP` it blocks everything but debuggable builds; it is only ignored for non-system apps targeting API 31+ when the operation is classified as `DEVICE_TRANSFER` (`IGNORE_ALLOW_BACKUP_IN_D2D`). `android:dataExtractionRules` (API 31+) and the older `android:fullBackupContent` list what survives; an `<exclude domain="..." path="."/>` for every domain in the `device-transfer` section is the same as opting out even when `allowBackup` is true. Verified from source: FairEmail excludes all five domains in both sections; KDE Connect sets `allowBackup="false"` and excludes everything in both sections; Fenix sets `allowBackup="false"` and excludes all domains, and IronFox is Fenix plus patches with no manifest or backup-rules patch, so it inherits that; mpv-android sets `allowBackup="true"` with no rules, so it is fully covered; Yam Launcher, Shizuku-Next (only `sharedpref/settings.xml`), ShizuCallRecorder (empty cloud-backup section means everything) and App Manager are covered too. Discord, X, Obsidian and Notion are closed source or ship no public manifest, so they cannot be settled from source; check on the device with `adb shell dumpsys package <pkg> | grep -m1 pkgFlags` (look for `ALLOW_BACKUP`), and for rules dump the manifest with `aapt2 dump xmltree --file AndroidManifest.xml <base.apk>`. Banking apps are the near-certain opt-outs, and so are most apps whose state is really a server session.

## Measured on the SM-A566B

Four things were checked on the device itself, and they narrow the source reading above.

With the default parameters, backup of an eligible app really does store data: `bmgr backupnow com.kitsumed.shizucallrecorder` printed `Success` and `dumpsys backup` then reported `Ever backed up: 1`. That rules out the "write-only transport" theory for the key/value pass.

`bmgr list sets` printed `1 : Local disk image` both before and after that backup, which matches the source: the token is a constant, so the listing proves nothing.

The D2D flag did not get an ineligible app through on this device. With `backup_local_transport_parameters=is_device_transfer=true`, after `bmgr init` so the transport re-read it, `bmgr backupnow org.kde.kdeconnect_tp` printed `Package org.kde.kdeconnect_tp with result: Transport rejected package because it wasn't able to process it at the time` and `Ever backed up` stayed `0`. KDE Connect is the worst possible subject for this test, because its `dataExtractionRules` excludes every domain in the `device-transfer` section as well as in `cloud-backup`, so a rejection is what the rules predict. The flag is therefore still unproven in either direction on API 37, and the first app to try it on should be one that sets `allowBackup="false"` without a `device-transfer` exclusion.

The restore half was not measured. Reading the result needs the app's own UI, and drive-by tap automation on this device was not reliable enough to call the outcome either way.

Why the 25 MB full-backup quota bites here in practice: OsmAnd's external files are 101 MB, and ShizuCallRecorder's recordings are already outside app data, so the apps with interesting state are either over quota or already stored where `adb pull` reaches them.

## How to verify this on the device

These were not run here, but they are the exact sequence the source above implies:

```
adb shell dumpsys package <pkg> | grep -m1 pkgFlags
adb shell bmgr enable true
adb shell bmgr transport com.android.localtransport/.LocalTransport
adb shell settings put secure backup_local_transport_parameters 'is_device_transfer=true'
adb shell bmgr backupnow <pkg>        # look for "Package <pkg> with result: Success"
adb shell bmgr list sets              # expect "  1 : Local disk image"
adb shell bmgr restore 1 <pkg>
```

Then confirm data actually arrived before opening the app. `dumpsys backup` prints an `Ever backed up:` list (`UserBackupManagerService.dumpInternal` L4127-4131) and the state file sizes per transport (L4084-4087), so a package missing from that list was rejected rather than backed up. `adb shell run-as <pkg> ls -l files databases shared_prefs` works only on debuggable builds; for a release app check the app's own behaviour, or compare `adb shell dumpsys package <pkg> | grep -A2 dataDir`. If restore prints `No available restore sets` the token is wrong; if the backup prints `Backup is not allowed`, the app is not eligible under the current destination; if `dumpsys backup` says `Backing up to debug-only private cache`, the local transport is selected and its 25 MB and 5 MB quotas are in force. A successful `bmgr restore` that leaves the app unchanged means the stored set has no `_delta` or `_full` entry for that package, which the `Ever backed up:` list will confirm.

## Maintenance state and Android 17

App Manager: active, v4.1.1 released 2026-09-04, ADB data backup landed in 4.1.0, and its restore path had an API break on Android 16 QPR2 (`clearApplicationUserData` signature change) fixed in May 2026, so check the release notes before relying on restore for API 37. DataBackup: maintained but slow, 2.0.12 from 2025-09-20 with the maintainer noting 2.x updates are delayed, root only. Neo Backup: active, 8.3.18 in May 2026, root mandatory. Swift Backup: active, Shizuku covers APKs, permissions, Wi-Fi and batch work, app data still root. Seedvault: AOSP integrated only. Obtainium: active, Shizuku used for silent installs, not for data. OsmAnd, FairEmail, Fossify suite, KDE Connect, mpv-android, Round Sync, OSS Document Scanner and Yam Launcher (which explicitly shipped API 37 support in 2.3) are all maintained; IronFox tracks Firefox releases and is current; Discord and X are server-side products where local backup was never supported.

## Root-only tools, for contrast

DataBackup: README states "Root needed, support Magisk, KernelSU, APatch"; Shizuku request issue #115 was closed with "adb shell level permissions are not enough". Swift Backup: "Backing up and restoring private app data requires root", Shizuku only enables batch APK work, permissions and Wi-Fi restore. Neo Backup: "Android device with root access (mandatory)". OAndBackupX: same, it is Neo's ancestor. Seedvault: "Needs to be integrated in your Android ROM and can not be installed as a regular app", because it needs `android.permission.BACKUP`. App Manager: internal data and prefs editing are root-only, as above.

## Per-app mechanisms

| App | Documented export | Documented import | Where the data goes | Automatable? |
|---|---|---|---|---|
| Obtainium | Settings > Import/Export, JSON with apps plus optional settings, auto export on change (`lib/providers/apps_provider_import_export.dart`) | Same page | Chosen SAF dir, plus one JSON per app at `/sdcard/Android/data/dev.imranr.obtainium/files/app_data/<id>.json` (`apps_provider_lifecycle.dart` L45) | Yes, file copy and diff of `app_data` |
| OsmAnd | Settings > Import/export, `.osf` for settings, profiles, favourites, tracks; or Settings > Local backup | Import the same `.osf` | Chosen SAF/file location; data dir can be moved to `/sdcard/Android/data/net.osmand.plus/files` | Partial, UI only unless the data dir is app-specific storage. It also ships `OsmandBackupAgent` (prefs, favourites, map markers, OSM bugs), so bmgr covers those files |
| Yam Launcher | Settings > backup, writes `yamlauncher_backup_<ts>.json` via `ACTION_CREATE_DOCUMENT` (`SettingsActivity.kt` L134-150) | Settings > restore via `ACTION_OPEN_DOCUMENT`, validates `app_id` and `schema_version` (L205-230) | Chosen SAF location | UI only, no custom intent action or broadcast |
| FairEmail | Backup tab, encrypted settings file with accounts, messages excluded | Backup tab import with password | Chosen SAF location | UI only; `data_extraction_rules.xml` excludes every domain for cloud and D2D, so bmgr restores nothing |
| Obsidian | Vault is plain files, `.obsidian/` holds config and plugins (help page "Configuration folder") | Copy the vault back | Vault folder in shared storage | Yes, `adb pull`/`push` of the vault |
| Fossify Contacts | Export vCard `.vcf` | Import `.vcf`, choose target account | Chosen SAF location; records land in ContactsProvider | UI only |
| Fossify Messages | Export SMS/MMS to JSON (`ExportMessagesDialog.kt` uses kotlinx JSON) | Import JSON, or XML whose root tag is `smses` (`MessagesImporter.kt` L118-149) | Chosen SAF location | UI only |
| Aves Libre | Settings export to a JSON file, optional auto export on change (FAQ: "auto settings export") | Settings import of the same JSON | Chosen SAF folder; Aves keeps ownership of the file it creates | UI only, but one JSON file is easy to diff |
| Fossify Phone | Export call history to `.json` (`SettingsActivity.kt` L426-437) | Import the same JSON (L401-415) | Chosen SAF location | UI only |
| KDE Connect | None found | None found, pairing must be redone | n/a | No; `allowBackup="false"` plus `data_extraction_rules.xml` excludes every domain in both `cloud-backup` and `device-transfer`, so bmgr cannot carry it either |
| Shizuku-Next | None | None, but `allowBackup="true"` and `data_extraction_rules.xml` includes only `sharedpref/settings.xml` | `settings.xml` in app data | bmgr can move that one file; the server must be restarted after every reboot anyway |
| ShizuCallRecorder | None, recordings are written through SAF to a chosen folder | None | Recording folder is user chosen, so already outside app data; prefs in `shizucallrecorder_prefs`, and the empty `<cloud-backup>` section in `data_extraction_rules.xml` means everything is included | bmgr yes, settings UI no |
| IronFox | No bookmark HTML/XML export on Android (upstream bug 209 open, so file based import/export is not possible) | No | n/a | No; it is Fenix plus a patch set, and none of the 155 patches touches `AndroidManifest.xml` or the backup rules, so it inherits Fenix's `allowBackup="false"` and exclude-all `data_extraction_rules.xml`. Firefox Sync is the only path. Inference from the patch tree, not read from an IronFox-built manifest |
| mpv-android | None, config editable only in the in-app editor | Same editor | `mpv.conf` and `input.conf` at `/data/data/is.xyz.mpv/files` (`Utils.kt` L96), root only, `--config-dir` does not redirect it (issue 1091) | Config no, but `allowBackup="true"` with no rules means bmgr carries the whole data dir, `mpv.conf` included |
| Discord | Server side data package request only (support article 360004027692) | n/a | Email link, up to 30 days | No local export |
| X | Settings > Your X data > Request data, archive ZIP of JSON | n/a | Email or in-app download link | No local export |
| Notion | Workspace export from web or desktop only, Markdown/HTML/CSV (help "Back up your data") | Not restorable as a workspace | Download link | No; content is cloud side anyway |
| Document Scanner (`com.akylas.documentscanner`) | Settings > Backup creates a ZIP of documents and folders; settings are a separate export action (`docs-site/docs/sync-and-backup.md`) | Settings > Restore backup | Chosen file location | UI only |
| Round Sync | Drawer > Export Config, ZIP with `rcx.prefs`, `rcx.json`, `rclone.conf` (`roundsync.com/usage/export.html`) | Drawer > Import Config, ZIP or bare `rclone.conf` | Chosen SAF location | UI only, but the ZIP is auditable and editable |

## Sources

- AOSP, local transport: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/packages/LocalTransport/src/com/android/localtransport/LocalTransport.java (also `LocalTransportParameters.java`, `LocalTransportService.java`)
- AOSP, encrypted local transport: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/packages/EncryptedLocalTransport/src/com/android/encryptedlocaltransport/EncryptedLocalTransport.java
- AOSP, backup service: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/backup/java/com/android/server/backup/ (`UserBackupManagerService.java`, `utils/BackupEligibilityRules.java`, `restore/PerformUnifiedRestoreTask.java`, `restore/FullRestoreEngine.java`, `restore/ActiveRestoreSession.java`, `fullbackup/PerformAdbBackupTask.java`, `../am/ContentProviderHelper.java` in `services/core`)
- AOSP, bmgr and bu: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/cmds/bmgr/src/com/android/commands/bmgr/Bmgr.java and `cmds/bu/src/com/android/commands/bu/Backup.java`
- AOSP, run-as: https://android.googlesource.com/platform/system/core/+/refs/heads/main/run-as/run-as.cpp
- AOSP, adb shell groups: https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/daemon/main.cpp
- AOSP, full backup rules: https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/java/android/app/backup/FullBackup.java
- Backup encryption code removal: https://gitlab.e.foundation/e/os/android_frameworks_base/-/commit/2d8b102ef175be9dbf3351314052ff28515f623a
- bmgr docs: https://developer.android.com/tools/bmgr
- Backup testing (local and D2D scripts): https://developer.android.com/identity/data/testingbackup
- adb backup restriction: https://developer.android.com/about/versions/12/behavior-changes-12#adb-backup-restrictions
- Application element (allowBackup, dataExtractionRules): https://developer.android.com/guide/topics/manifest/application-element
- App Manager: https://github.com/MuntashirAkon/AppManager (README, releases v4.1.0 and v4.1.1, issues 1550 and 1940, `backup/BackupFlags.java`, `compat/BackupCompat.java`)
- DataBackup: https://github.com/XayahSuSuSu/Android-DataBackup (README, issue 115)
- Swift Backup: https://www.swiftapps.org/ and https://play.google.com/store/apps/details?id=org.swiftapps.swiftbackup
- Neo Backup: https://neoapplications.gitlab.io/projects/neo-backup/
- Seedvault: https://github.com/seedvault-app/seedvault
- Obtainium: https://github.com/ImranR98/Obtainium (`lib/providers/apps_provider_lifecycle.dart`, `docs/DEVELOPER_GUIDE.md`, wiki Import/Export page)
- OsmAnd: https://osmand.net/docs/user/personal/import-export/
- FairEmail: https://m66b.github.io/FairEmail/ and `app/src/main/res/xml/data_extraction_rules.xml`
- Yam Launcher: https://github.com/thomasboom/YAM-Launcher (`settings/SettingsActivity.kt`, release v2.3)
- KDE Connect: https://github.com/KDE/kdeconnect-android (`src/main/AndroidManifest.xml`, `src/main/res/xml/data_extraction_rules.xml`)
- Shizuku: https://shizuku.rikka.app/guide/setup/ (reboot behaviour)
- ShizuCallRecorder: https://github.com/kitsumed/ShizuCallRecorder and https://kitsumed.github.io/ShizuCallRecorder/SUPPORT
- IronFox: https://ironfoxoss.org/releases/ and https://codeberg.org/ironfox-oss/bugs/issues/209
- Fenix manifest: https://github.com/mozilla-firefox/firefox/blob/main/mobile/android/fenix/app/src/main/AndroidManifest.xml
- mpv-android: https://github.com/mpv-android/mpv-android (`app/src/main/java/is/xyz/mpv/Utils.kt`, issues 31 and 1091)
- Round Sync: https://roundsync.com/usage/export.html
- OSS Document Scanner: https://github.com/ossappscollective/OSS-DocumentScanner (`docs-site/docs/sync-and-backup.md`, discussion 468)
- Discord data package: https://support.discord.com/hc/en-us/articles/360004027692
- X archive: https://help.x.com/en/managing-your-account/how-to-download-your-x-archive (steps also summarised at https://xautopilot.app/blog/download-your-x-archive)
- Notion export: https://www.notion.com/help/back-up-your-data
- Samsung Smart Switch: https://www.samsung.com/za/support/mobile-devices/does-smart-switch-backup-restore-data-from-an-application-like-game-data-or-chatting-log/

## Verdict

The local transport is a genuine two-way transport, but with default parameters it is gated by `allowBackup` and by 25 MB and 5 MB quotas, which is why `bmgr backupnow` can report `Success` and a later `bmgr restore 1 <pkg>` still deliver nothing: the app was never eligible, or its data was over quota, or it was absent from the set. It is not write-only, and no Google issue is required to explain the reports.

`com.android.encryptedlocaltransport` is not needed for restore and today contains no encryption at all: it inherits `LocalTransport` and copies raw streams. The 2019 framework encryption plan it was built for was abandoned and its code deleted, with encryption now living in GMS. Its old key model was a recoverable-keystore secondary key wrapping a per-package tertiary key, unreproducible without GMS and irrelevant to the current source.

Two levers actually change the outcome without root. Reading and writing `/sdcard/Android/data/<pkg>/files` is the only path that reliably reaches real app state cheaply and repeatably, and it is how Obtainium's app list is already being copied in this repo. Setting `is_device_transfer=true` on the local transport is *supposed* to make `bmgr backupnow` and `bmgr restore` treat the operation as D2D, which on paper is the only way to carry an `allowBackup="false"` app, subject to quotas and to a `device-transfer` exclusion section; the one attempt on this device was rejected, so treat it as a hypothesis to test on an app that does not exclude every domain rather than as a working lever. The GMS `D2dTransport` is the same rule relaxation with GMS-owned storage, so it is strictly worse for a host-side workflow: nothing can be inspected, copied, or verified locally.

Everything else is per-app work. Apps with an export are the reliable route; apps that both opt out of backup and ship no export have no non-root path, and no tool changes that, because `android.permission.BACKUP` is signature|privileged and `/data/data` is 0700 even for shell.
