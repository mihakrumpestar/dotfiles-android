# Android dotfiles

Tooling and notes for two Android phones: a personal phone on a custom ROM (a community-built Android image) and a work phone on its stock firmware (what the manufacturer ships). Device models, codenames and serials are deliberately not recorded here.

## Guides

- [Custom ROM](docs/custom-rom.md): bootloader, recovery and ROM for the personal phone.
- [Debloat](docs/debloat.md): what to disable on the stock work phone, and why only disable.
- [Shared setup](docs/shared-setup.md): apps, accounts and declarative settings both phones use.
- [App data backup](docs/app-data-backup.md): what app state is reachable without root, and how.
- [Playbooks](docs/playbooks.md): what to check when something on the phone stops working.

## Prerequisites

Run everything inside the devenv shell, which provides `adb` (the Android Debug Bridge, a command line link to the phone), `fastboot`, Python, `uv` and `task`:

```sh
devenv shell
```

Enable USB debugging on the phone and authorize the host. Run every command through `task`, never a built binary: the toolchains exist only inside devenv. Two Python commands live here: `androidctl` applies device state (packages, settings, app installs), and `uiu` replays per-app export/import recipes. The `ui-*` tasks run the second one.

## Commands

```sh
task                      # list every task
task check                # ruff, format check, pytest
task report               # every package with the facts that decide a keep/kill call
task health               # launcher, keyboard, roles, crash buffer
task disable -- policy/packages.txt --dry-run
task disable -- policy/packages.txt
task restore              # re-enable everything the newest snapshot saw enabled
task init                 # install Shizuku-Next and Obtainium, start Shizuku
task install -- dev.imranr.obtainium   # install an app from its release, checksum verified
task apply                # dry run of profile.conf and every app file
task apply -- --yes       # write both
task verify               # report drift across every declared entry
task revert               # restore the values captured before the last apply
task ui-list              # app backup recipes and their stored artifacts
task ui-setup -- --app <package>     # run the in-app setup walk
task ui-backup -- --app <package>    # export an app's own state
task ui-verify -- --app <package>    # re-export and diff
task ui-restore -- --app <package>   # replay the artifact, guarded
```

An app directory lives in `policy/data/<app>/`; the folder is the app's normalized name, lowercase and path friendly, e.g. `yamlauncher`. One `app.py` holds everything about that app: its package id, its release facts when `task install` or `task init` needs them, the device state it needs, and its walks for setup, backup and restore. Each export is a snapshot of one phone. The artifact commits with the values the walk marks `private` zeroed; only those live in `./secrets/<app>/`, its own repository, ignored here.

## License

[MIT](LICENSE)
