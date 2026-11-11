# Shared setup

Apps, accounts and settings both phones use. Debloat the stock phone first, see [Debloat](debloat.md); the custom ROM phone starts with far less preinstalled software.

## Apps

Use Obtainium as the only app store. Its export is the app list: set it up once, export, re-import on the next phone. No second copy lives in this repository.

```sh
task obtainium            # latest release, checksum verified, installed
task obtainium -- --dry-run
```

The installer picks the asset matching the device's `ro.product.cpu.abilist`, checks the publisher's `.sha256`, and refuses to install on a mismatch. Import your export from Obtainium's Settings.

## Round Sync

Use the maintained [gulp79 fork](https://github.com/gulp79/Round-Sync) (`de.felixnuesse.extract`), not the unmaintained upstream `newhinton/Round-Sync`. Grant storage permission before the first run; without `READ_EXTERNAL_STORAGE` it cannot see local storage.

## Accounts and pairing

1. Pair KDE Connect.
2. Restore Simple Contacts and Simple Messages. Set automatic backups on Contacts; Messages has manual backup only.
3. IronFox: set as default browser, then add Dark Reader, I still don't care about cookies, and uBlock Origin.
4. OsmAnd~: restore the backup and install the required maps.
5. ShizuCallRecorder, with Shizuku from [Shizuku-Next](https://github.com/rushiranpise/Shizuku-Next) (`moe.shizuku.privileged.api`) and [ShizuCallRecorder](https://github.com/Kitsumed/ShizuCallRecorder) (`com.kitsumed.shizucallrecorder`). Setup, once:

   1. Developer options, enable USB debugging and Wireless debugging.
   2. Shizuku, pair with wireless debugging, start it. Grant Nearby devices and Local network when it asks, they gate the pairing discovery. Automatic pairing reads the code off the screen, so it also needs Shizuku's accessibility service turned on.
   3. Grant the auto-start permission so it survives reboots, then enable Start on boot in Shizuku:

      ```sh
      adb shell pm grant moe.shizuku.privileged.api android.permission.WRITE_SECURE_SETTINGS
      ```

      Also allow Background processes in Shizuku's permissions page, Android 12 and later otherwise kills the server started at boot.
   4. ShizuCallRecorder, grant notifications, phone state, call log and contacts, allow manage ongoing calls, then enable automatic recording for incoming and outgoing calls:

      ```sh
      adb shell pm grant com.kitsumed.shizucallrecorder android.permission.READ_CALL_LOG
      adb shell pm grant com.kitsumed.shizucallrecorder android.permission.READ_PHONE_STATE
      adb shell pm grant com.kitsumed.shizucallrecorder android.permission.READ_CONTACTS
      adb shell pm grant com.kitsumed.shizucallrecorder android.permission.POST_NOTIFICATIONS
      adb shell cmd appops set com.kitsumed.shizucallrecorder MANAGE_ONGOING_CALLS allow
      ```

## Backups

- Keep pictures and downloads backed up.
- Run Round Sync to copy new backups to the server.

## Settings

`policy/profile.conf` holds the device settings and `policy/apps.conf` holds the app scoped state. Both are applied, verified and reverted by the same command, in one run with one snapshot, so they behave as a single declaration that is simply split by subject. Declaring the same key in both files is an error.

The section decides the store. `system`, `secure` and `global` go to the Settings provider, `telephony` and the other listed DeviceConfig namespaces reach DeviceConfig, and the app scoped sections (`permissions`, `appops`, `role`, `deviceidle`, `standby`, `locale`, `applinks`, `applinks_domain`, `applinks_selection`) reach their own service. An unknown section is a parse error rather than a silent DeviceConfig write.

App scoped entries name the package in the key (`<package>:<permission>` for permissions, app ops and domains, the package alone for the rest). An entry whose package is not installed for user 0 is skipped and counted, so one file still covers both phones.

```sh
task apply      # dry run unless --yes is in CLI_ARGS
task verify     # report drift
task revert     # restore the values captured before the last apply
```

Every write is read back, in `apply` and in `revert` alike, because the platform accepts writes it then ignores: a DeviceConfig read-only flag, `pm grant` of a permission the app never declared, `cmd locale` on a package that is not installed (prints `Unknown package` and exits 0), `cmd deviceidle` on an unknown package, and `pm set-app-links` on a domain the app does not declare all exit 0 having changed nothing. Only the stored value decides success.

Reads are tri-state. `explicit` means the device holds a value somebody set: it is written, and revert writes it back. `implicit` means the platform derived the value because nobody set one (an app op with no row, a doze-whitelisted app pinned at standby bucket `exempted`, a ROM-shipped whitelist row, a `system_configured` domain state); apply writes the target and the readback decides whether it stuck, and revert resets the key so the derived value comes back rather than leaving the row apply created. `unset` means the store has nothing to say about the key, which is how a typo in a package or domain name shows up instead of reading as success.

The two stores differ in how much they can lie to you. DeviceConfig refuses an unknown flag with a `SecurityException`, so a key that does not exist on a phone fails loudly. The Settings provider accepts any key and echoes it back, so a vendor setting (`blue_light_filter*`, `aod_mode`, `protect_battery`, the Samsung vibration keys) reads as matching on a phone whose UI never consults it. `verify` cannot tell that apart from a real setting, so a profile entry that matters only on one phone is best kept in a per-phone file.

An app's own preferences (shared preferences, databases under `/data/data/<package>`) are not reachable over adb without root, so the profile stops at the platform-side state. Apps that keep their settings in external storage are the exception, because `adb` can read and write those files directly: Obtainium stores one JSON file per app under `/sdcard/Android/data/dev.imranr.obtainium/files/app_data/`, so its whole app list can be copied or diffed outside the app.

## App settings through their own UI

For everything else, the reproducible path is the app's own export and import screens, driven from a recipe. Each app gets one folder named as a normalized app name (lowercase, no spaces), holding `steps.json` and the blobs that app exports:

```
policy/data/yamlauncher/steps.json        the recipe, the only file naming the package
policy/data/yamlauncher/yamlauncher.json  the export it produces
```

The same recipe runs the export for both backup and restore, so the two can never drift apart. Steps name their blobs with a bare file name, which resolves inside the app's own folder, so no step ever repeats a path.

Nothing is cached beside the artifact. Its hash is computed when it is needed, so a hand-edited artifact simply becomes the new baseline instead of disagreeing with a stale digest file.

```sh
task ui-list                                  # recipes and whether an artifact is stored
task ui-backup -- --app eu.ottop.yamlauncher
task ui-verify -- --app eu.ottop.yamlauncher   # re-export and diff
task ui-restore -- --app eu.ottop.yamlauncher
```

A step is one action: `launch`, `tap`, `type`, `clear`, `wait`, `key`, `scroll`, `sleep`, `pull`, `push`, `capture`, `assert`. Selectors match on `text`, `id`, `desc` or `textContains`, plus `index` for duplicates, and never on coordinates, so a layout shift in the app does not silently tap the wrong control. Every `tap`, `type` and `assert` takes a fresh `uiautomator dump` first, because the screen changes between steps and a stale view is how a recipe taps nothing.

The recipe's own keys are `package`, `artifact`, `volatile`, `redact`, `backup` and `restore`. Both path lists use the same dotted form and name the leaf whose value is overwritten, e.g. `data.locationRegion.value`.

Restore is guarded. It exports the app's current state first, compares it with the stored artifact, and stops if they match. When they differ it prints the diff, writes the current export to `.state/ui/<package>/` as the undo copy, and refuses without `--force`. The undo folder is keyed on the package id rather than the readable folder name, so renaming a folder cannot mix two apps' copies. Apps that gate their settings screen behind a lock or biometrics will block the recipe, so leave those locks off on a phone that this tooling drives.

An export is a snapshot of one phone, so a committed artifact leaks whatever the app happened to hold. `redact` names the JSON paths that must not be committed, and every `pull` zeroes them before the file is stored or compared:

```json
"volatile": ["created_at"],
"redact": ["data.location.value", "data.locationRegion.value"]
```

The value is replaced by the zero of its own type (`""`, `false`, `0`) rather than deleted, because restore clears preferences and puts back only the keys the file names: a deleted key would disappear from the phone instead of resetting, and a type change would throw in the app's typed getter and fail the whole restore.

Neither list hides real drift; each hides exactly what it names. `volatile` compares equal, `redact` compares empty, so once a field is redacted `ui verify` cannot see it change at all. That is the trade: the value is not in the repository, so there is nothing left to compare against. Redaction also only reaches values at a fixed path. A field whose shape varies with your data, a list that grows as you search, is not covered by either list.

Some apps have no export at all. Their recipe uses `capture` instead, which writes the settings screens as XML beside the recipe; with no artifact to attach to, those captures live in `captures/`. That is a record to diff against, not something that can be replayed.

If nothing of the app's state is reachable, `docs/app-data-backup.md` lists what each app actually offers.

## Apps no longer installed

Kept for reference, so a past decision is not reopened from memory:

- Simple-Flag-Secure: screenshots and recording with FLAG_SECURE; caused a boot failure.
- Basic Call Recorder: stopped working on recent Android versions (v15-16). Replaced by ShizuCallRecorder.
- VSCodroid: VS Code on Android.
- Niimbot: companion app for a label printer.
- 1688 and Taobao idlefish.
- Termux: no use for it.
- mLauncher: buggy search; replaced by EasyLauncher, then Yam Launcher.
- KeePassDX and Key Driver: the hardware key driver did not work properly.
- AdAway: does not work with KernelSU.
- Super Backup: used for scheduled contact, message and call history backups.
- APKPure
- Cinema-FV5
- Filmic Pro
- Lucky Patcher
- RHVoice: download the English voice, then Settings, Accessibility, Text-to-speech output, Preferred engine, select it again to activate.
- BetterBatteryStats
- Shelter: work profile sandboxing, now built in.
- Island: unmaintained, replaced by Shelter.
