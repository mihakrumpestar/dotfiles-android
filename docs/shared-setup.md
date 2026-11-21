# Shared setup

Apps and settings that both phones use. Debloat the stock phone first, see [Debloat](debloat.md).

## Apps

Use Obtainium as the only app store. Its export is the app list: set it up once, export it, and import it on the next phone. `task ui-backup -- --app dev.imranr.obtainium` stores it in `policy/data/obtainium/obtainium.json`. `uiu` drops installed versions, changelogs, update timestamps, asset sizes and the extra asset lists, and orders the apps by package id, so the file records the app list and its per-app settings rather than the phone's update history. Obtainium refills the dropped state on its next check.

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

An app is one directory under `policy/data`, holding one `app.py`. The file holds the package id, the release facts when it is installed from a release, the device state it needs, and its screen walks. The package id is written once, at the top.

```python
from uiu.dsl import app

APP = app(
    package="org.ironfoxoss.ironfox",
    install={
        "release": "https://api.github.com/repos/ironfox-oss/IronFox/releases/latest",
        "checksum": "asset",
        "assets": {"universal": "IronFox.apk"},
    },
    roles={"android.app.role.BROWSER": "IronFox is the browser"},
    permissions={"android.permission.POST_NOTIFICATIONS": ("granted", "update notices")},
)
```

The release URL must be a `.../releases/latest` endpoint, `assets` maps an ABI to a release file with a `universal` fallback, `checksum` says where the digest comes from (`asset` for a publisher's `.sha256`, `digest` for the digest GitHub records), and `start` is optional. An app with no `install` arrives some other way. Adding an app is adding a directory; no shared code changes.

The app list comes back with `task ui-restore -- --app dev.imranr.obtainium`, and each app then installs from it.

## Settings

`policy/profile.conf` holds device settings, and each app's own file holds what belongs to that app. They are one declaration split by subject, and declaring the same key twice is an error. One snapshot covers both.

```sh
task apply            # dry run
task apply -- --yes   # write
task verify           # report drift
task revert           # restore the values captured before the last apply
```

`task apply -- --yes` grants every permission, app op, role and doze entry declared across `policy/data/*/app.py` in one run with one snapshot, and enables the accessibility and notification-listener services named in `policy/profile.conf`.

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

## Steps no command can do

These are the only things left by hand once per phone. Everything else is a command above, or a walk in the app's own file: `task ui-setup -- --app <package>` for in-app settings and `task ui-restore -- --app <package>` for a stored export.

### Shizuku-Next

Grant the auto-start permission, enable Start on boot in Shizuku, and allow Background processes in its permissions page. Android 12 and later otherwise kills the server started at boot. `WRITE_SECURE_SETTINGS` is an install permission, so `apply` cannot verify it:

```sh
adb shell pm grant moe.shizuku.privileged.api android.permission.WRITE_SECURE_SETTINGS
```

### Mindful

Set the screen time limits and the bedtime.

### MicroG

Run the self-check.

### Orbot

Start it, and turn on VPN mode.

### Aurora Store

Tap **Anonymous** on the splash screen. The status moves through "Requesting new session" to "Verifying new session" and the app lands on its grid. Nothing is typed and no Google account is involved.

### IronFox

An add-on page is a GeckoView with no controls the engine can read, so the two extra add-ons are installed by hand. Open each link inside IronFox, tap **Add to Firefox**, then **Add**:

- `https://addons.mozilla.org/android/addon/istilldontcareaboutcookies/`
- `https://addons.mozilla.org/android/addon/darkreader/`

They are listed under **Extensions**, in the `⋮` menu and in Settings, not under an "Add-ons" label. The switch that allows the installation, and Brave as the default search engine, are set by the app's setup walk.

### OsmAnd

Restore a `.osf` from the phone-sync folder: map screen > the bottom-left menu button > Settings > **Import/export** > **Import from file** > pick the `.osf` > tick the three rows on "Select the data to be imported." (**Continue** stays disabled until at least one is ticked) > **Continue** > **Replace all** > **Restart**.

### Round Sync

The config and the sync triggers come back with `task ui-restore -- --app de.felixnuesse.extract`. The stored zip is `secrets/roundsync/roundsync.zip` because `rclone.conf` inside it carries the WebDAV password. A phone that never saw the backup has the remote without that password until it is typed once in the app.

## Backups

- Keep pictures and downloads backed up.
- Run Round Sync to copy new backups to the server.

## App settings through their own UI

For app preferences, use the app's own export and import, driven by the walks in the app's own file:

```
policy/data/<app>/app.py                  committed: package, install facts, device state, walks
policy/data/<app>/<export>.json           committed: the export, sensitive values zeroed
policy/data/<app>/<payload>.json          committed: a file the app imports, untouched
secrets/<app>/<export>.private.json       private: the values that were zeroed, SOPS-encrypted
secrets/<app>/<export>.zip                private: an export that cannot be split, SOPS-encrypted
```

The walk is Python, built with `uiu.dsl`:

```python
from uiu.dsl import app, ensure, key, launch, pull, push, tap, wait

APP = app(
    package="com.example.demo",
    artifact="demo.json",
    setup=[
        push("demo-settings.json", "/sdcard/Download/demo-settings.json"),
        launch("com.example.demo/.MainActivity", clear=True),
        wait(text="Settings"),
        tap(text="Settings"),
        wait(text="Import from file"),
        tap(text="Import from file"),
        ensure(True, text="Show updates"),
    ],
    backup=[
        key("KEYCODE_HOME"),
        launch("com.example.demo/.MainActivity", clear=True),
        pull("/sdcard/Download/demo.json", "{{artifact}}"),
    ],
    restore=[
        push("{{artifact}}", "/sdcard/Download/demo.json"),
    ],
)
```

The module-level `APP` is the whole contract. Selector arguments are `text`, `id`, `desc`, `text_contains`, `class_name` and `index`; `class_name` is the probe's `class`, renamed because Python reserves that word. A step that cannot run fails when the app file loads, not halfway through a run.

There are two trees because most recipes are policy anyone may read, while an export describes one person's phone and some apps are themselves private. The folder name maps one tree to the other. The private tree is `--secrets`, `ANDROID_SECRETS_DIR`, or `./secrets`, in that order. It is its own git repository. An app that must not be public keeps its file there as `<app>/app.py`, plaintext because the repository is private; the store reads app files from both trees.

Every export file in the private tree is a SOPS envelope encrypted to one age key, in SOPS's binary store so the plaintext returns byte for byte. App files and the selector list are policy, not export data, so they stay plaintext because the repository is private. `uiu` decrypts in memory on read and encrypts before writing, so a plaintext export never lands there. The age private key is the `SOPS_AGE_KEY` secret in `secretspec.toml`, which keeps it in the system keyring:

```sh
secretspec set SOPS_AGE_KEY    # once per machine
```

A missing key stops any command that needs an encrypted file, naming the file. `uiu list` reads no selectors, so it needs no key, and it appends `plaintext` to a stored export that is not a SOPS envelope. The keyring entry is the only copy, so back the age key up somewhere offline; `secrets/README.md` has the recovery and rotation commands.

Some files stay plaintext on purpose: app files and selector lists, because they are policy and the repository is private; undo copies under `.state/ui/<package>/`, which hold the pre-restore export at 0600 and must not be synced or committed; and a verify or restore export in a temp directory until it is published.

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
task ui-list                                  # app files and whether an export is stored
task ui-setup -- --app com.aurora.store       # run the in-app setup walk
task ui-setup -- --app com.sec.android.app.clockpackage  # 05:40 weekday alarm
task ui-setup -- --app com.android.settings              # Sleeping Do Not Disturb rule
task ui-setup -- --app md.obsidian                       # Nextcloud sync plugin, so the vault can sync itself
task ui-backup -- --app eu.ottop.yamlauncher  # export and store
task ui-verify -- --app eu.ottop.yamlauncher  # re-export and diff
task ui-restore -- --app eu.ottop.yamlauncher # push the stored export back
```

Obsidian has one walk and one prerequisite: create the vault with the same name as the desktop's, because that name is the remote folder. The walk installs the Nextcloud sync plugin, and the first sync pulls the notes, the attachments and the other plugin settings.

The engine is `uiu`, run with `uv run uiu`; the `task ui-*` names call it. It drives the phone through uiautomator2, whose server runs as the shell user, so nothing is installed on the device. A step is one action: `launch`, `tap`, `type`, `clear`, `wait`, `key`, `scroll`, `sleep`, `pull`, `push`, `capture`, `assert`, `shell`, `ensure` and `if_absent`.

Selectors match on `text`, `id`, `desc` or `textContains`, plus `index` for duplicates. They never use coordinates. A `type` without a selector writes into the field the previous step focused, which is the way into a search box with no label. `launch` with `"clear": true` stops the app first, so a run starts at the root screen. `pull` with `"newest": true` takes the newest match of a device glob. A pull waits for the file instead of a recipe sleeping first.

A control a webview draws without any label cannot be selected, so a step reaches it with `shell` and `input tap` at a point. Only Obsidian does this, for the settings gear at the bottom right of its drawer; the point assumes a 1080x2340 screen.

Some steps exist because a run has to be safe to repeat. `ensure` taps a row only when its checked state differs. `if_absent` runs a block of steps only when its selector matches nothing, which is how a one-time item is created without creating a second copy. `wait` takes `checked` to wait on a switch, `absent` to wait for a dialog to go away, and `scroll` takes `until` to stop as soon as the target appears.

Restore is guarded when the recipe has a backup. It exports the current state first, compares it with the stored export and overlay, and stops if they match. When they differ it prints the diff, writes an undo copy under `.state/ui/<package>/`, and refuses without `--force`. After the import it exports again and prints `restored` only when the phone matches; otherwise it reports a failure with the undo copy's path. A recipe with no backup has nothing to compare, so it pushes the stored file as it is. Apps with a lock or biometrics gate will block a recipe, so leave those off on a driven phone.

Two exports compare by content, not by bytes. A JSON export has its `volatile` fields masked and its `sort` lists ordered; a zip is compared member by member, ignoring the archive's own timestamps, so `task ui-verify` stays green for the apps whose export is an archive.

Write a walk by probing the screens, not by guessing:

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
