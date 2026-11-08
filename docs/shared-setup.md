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

## Backups

- Keep pictures and downloads backed up.
- Run Round Sync to copy new backups to the server.

## Settings

`policy/profile.conf` is the declarative settings profile. The namespace decides the store: `system`, `secure` and `global` go to the Settings provider, everything else goes to DeviceConfig. Some keys are vendor specific, so on a phone that does not have them `task verify` reports them and they should be dropped from that phone's profile rather than left to drift forever.

```sh
task apply      # dry run unless --yes is in CLI_ARGS
task verify     # report drift
task revert     # restore the values captured before the last apply
```

Every apply snapshots the keys it is about to change, writes them, then reads each one back. Both stores can accept a write and ignore it (a DeviceConfig read-only flag prints `Error: cannot write read-only flag` and still exits 0), so the stored value decides success, not the exit code. `revert` puts the captured values back and deletes the keys that were unset.

Not every setting belongs in the profile: wallpapers, vibration patterns stored by the vendor UI, and anything another app writes at runtime will drift back. Those are set once by hand.

## Apps no longer installed

Kept for reference, so a past decision is not reopened from memory:

- Simple-Flag-Secure: screenshots and recording with FLAG_SECURE; caused a boot failure.
- Basic Call Recorder: stopped working on recent Android versions (v15-16).
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
