# Shared setup

Apps, accounts and settings that both phones use. Debloat the stock phone first, see [Debloat](debloat.md).

## Apps

Use Obtainium as the only app store. Its export is the app list: set it up once, export it, and import it on the next phone. No second copy lives in this repository.

On a fresh phone, enable USB debugging, attach the cable, and run:

```sh
task init
```

`task init` reads `policy/init.txt` in order and installs Shizuku-Next, then Obtainium. It verifies each APK, then starts Shizuku's server over USB so the server holds adb permissions and Obtainium can install every app through it. Keep the cable attached until it prints `started`.

For one app:

```sh
task install -- dev.imranr.obtainium
task install -- dev.imranr.obtainium --dry-run
```

An app is one directory under `policy/data`. Its `install.json` holds the package id and release facts: the release URL must be a `.../releases/latest` endpoint, `assets` maps an ABI to a release file with a `universal` fallback, `checksum` says where the digest comes from (`asset` for a publisher's `.sha256`, `digest` for the digest GitHub records), and `start` is optional. `steps.py` is optional and drives backup and restore. Adding an app is adding a directory; no shared code changes.

After Obtainium starts, open it and import your export from Settings.

## Round Sync

Use the maintained [gulp79 fork](https://github.com/gulp79/Round-Sync) (`de.felixnuesse.extract`), not the unmaintained `newhinton/Round-Sync`. Grant storage permission before the first run; without `READ_EXTERNAL_STORAGE` it cannot see local storage.

## Accounts and pairing

1. Pair KDE Connect.
2. Restore Simple Contacts and Simple Messages. Turn on automatic backups for Contacts. Messages only has manual backup.
3. IronFox: set it as the default browser, then add Dark Reader, I still don't care about cookies, and uBlock Origin.
4. OsmAnd~: restore the backup and install the required maps.
5. Shizuku-Next and ShizuCallRecorder:
   - Run `task init` as above.
   - Grant Shizuku the auto-start permission, enable Start on boot in Shizuku, and allow Background processes in its permissions page. Android 12 and later otherwise kills the server started at boot.

     ```sh
     adb shell pm grant moe.shizuku.privileged.api android.permission.WRITE_SECURE_SETTINGS
     ```

   - Grant ShizuCallRecorder its permissions and app op, then enable automatic recording for incoming and outgoing calls:

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

`policy/profile.conf` holds device settings. `policy/apps.conf` holds per-app state. They are one declaration split by subject, and declaring the same key in both is an error. One snapshot covers both.

```sh
task apply            # dry run
task apply -- --yes   # write
task verify           # report drift
task revert           # restore the values captured before the last apply
```

The section decides the store:

- `system`, `secure`, `global`: the Settings provider, Android's settings database.
- `telephony` and the other listed DeviceConfig namespaces: DeviceConfig, a system settings store split into namespaces.
- `permissions`, `appops`, `role`, `deviceidle`, `standby`, `locale`, `applinks`, `applinks_domain`, `applinks_selection`: their own service.

An unknown section is an error, never a silent write.

App-scoped entries name the package in the key: `<package>:<permission>` for permissions, app ops and domains, and the package alone for the rest. An entry whose package is not installed for user 0 is skipped and counted, so one file covers both phones.

Every write is read back. Exit 0 does not prove a change: the platform accepts some writes and then ignores them. Only the stored value decides.

Each key reads one of three ways:

- `explicit`: a value somebody set. Apply writes it; revert puts it back.
- `implicit`: the platform derived it because nobody set one. Apply writes the target; revert resets the key so the derived value returns.
- `unset`: the store has nothing. This is how a typo in a package or domain shows up.

DeviceConfig rejects an unknown key loudly. The Settings provider accepts any key and echoes it back, so a vendor setting can read as matching on a phone whose UI ignores it. Keep a setting that matters on only one phone in a per-phone file.

An app's own preferences under `/data/data/<package>` need root, so a profile stops at platform-side state. Apps that keep files under `/sdcard/Android/data/<package>/` are the exception: adb can copy those directly, and that is how Obtainium's app list moves.

## App settings through their own UI

For app preferences, use the app's own export and import, driven by a recipe. Each app gets one folder:

```
policy/data/<app>/steps.py                committed: how to drive the app
policy/data/<app>/install.json            committed: release facts, when the app is installed here
policy/data/<app>/<export>.json           committed: the export, sensitive values zeroed
secrets/<app>/<export>.private.json       private: the values that were zeroed
```

A recipe is a Python file. Each step is a function call, built with `uiu.dsl`:

```python
from uiu.dsl import key, launch, pull, recipe, tap, wait

RECIPE = recipe(
    package="com.example.demo",
    artifact="demo.json",
    backup=[
        key("KEYCODE_HOME"),
        launch("com.example.demo/.MainActivity", clear=True),
        wait(desc="Settings"),
        tap(desc="Settings"),
        pull("/sdcard/Download/demo.json", "{{artifact}}"),
    ],
)
```

The module-level `RECIPE` is the whole contract. Selector arguments are `text`, `id`, `desc`, `text_contains`, `class_name` and `index`; `class_name` is the probe's `class`, renamed because Python reserves that word. A step that cannot run fails when the recipe loads, not halfway through a run.

There are two trees because a recipe is policy anyone may read, while an export describes one person's phone. The folder name maps one tree to the other. The private tree is `--secrets`, `ANDROID_SECRETS_DIR`, or `./secrets`, in that order. It is its own git repository.

Two lists decide what happens to a value that must not be published:

- `redact` throws it away. Every export zeroes it, and a restore resets the field to empty.
- `private` keeps it in the private tree. The public file keeps the zero of its type, and restore merges the private half back before pushing. Verify compares both halves, so a change is still reported. A missing private file is refused.

A path may appear in one list or the other, never both. `private` needs a JSON export. `volatile` names fields that change on their own, such as timestamps. `sort` names lists whose order means nothing, such as `apps`, so exports compare equal.

`private` can name a file. That is the safer default, because the list itself says which entries exist:

```json
"private": "private.json"
```

```json
[
  "settings.accountToken",
  "apps[id=com.example.bank]"
]
```

A plain path moves one value out. A path with an identity moves one list element out and leaves the others public. An identity that matches nothing fails, because publishing the entry it should hide is worse. A plain path the app may omit is only a warning. A path that would cross a list is refused; use an identity instead. Neither list covers a value whose path or shape changes with your data.

```sh
task ui-list                                  # recipes and whether an export is stored
task ui-backup -- --app eu.ottop.yamlauncher
task ui-verify -- --app eu.ottop.yamlauncher  # re-export and diff
task ui-restore -- --app eu.ottop.yamlauncher
```

The engine is `uiu`, run with `uv run uiu`; the `task ui-*` names call it. It drives the phone through uiautomator2, whose server runs as the shell user, so nothing is installed on the device. A step is one action: `launch`, `tap`, `type`, `clear`, `wait`, `key`, `scroll`, `sleep`, `pull`, `push`, `capture`, `assert`.

Selectors match on `text`, `id`, `desc` or `textContains`, plus `index` for duplicates. They never use coordinates. A `type` without a selector writes into the field the previous step focused, which is the way into a search box with no label. `launch` with `"clear": true` stops the app first, so a run starts at the root screen. `pull` with `"newest": true` takes the newest match of a device glob. A pull waits for the file instead of a recipe sleeping first.

The recipe's keys are `package`, `artifact`, `volatile`, `redact`, `private`, `sort`, `backup` and `restore`. Path lists are dotted and name the leaf, for example `data.locationRegion.value`. A list is walked in place, so `apps.lastUpdateCheck` reaches every element.

Restore is guarded. It exports the current state first, compares it with the stored export and overlay, and stops if they match. When they differ it prints the diff, writes an undo copy under `.state/ui/<package>/`, and refuses without `--force`. After the import it exports again and prints `restored` only when the phone matches; otherwise it reports a failure with the undo copy's path. Apps with a lock or biometrics gate will block a recipe, so leave those off on a driven phone.

Write a recipe by probing the screens, not by guessing:

```sh
task ui-probe -- --launch dev.imranr.obtainium/.MainActivity --clear \
  --tap '{"desc": "Settings"}' --tap '{"desc": "Import/export"}'
```

The probe prints one line per control with the exact selector a step takes. Flutter apps (Obtainium is one) put labels in `content-desc`, which the probe reports as `desc`, so a selector written against `text` will miss them. `--step` takes a step as JSON, for actions `--tap` cannot express. A failed step prints the step and the screen it saw.

Some apps have no export. Their recipe uses `capture` instead, which writes the settings screens as XML into `<secrets>/<app>/captures/`. That is a record to diff against, not something that can be replayed.

## Apps no longer installed

Kept for reference:

- AccuBattery
- Simple-Flag-Secure: screenshots and recording with FLAG_SECURE; caused a boot failure.
- Basic Call Recorder: stopped working on Android 15 and 16. Replaced by ShizuCallRecorder.
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
