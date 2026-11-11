# Android dotfiles

Tooling and notes for two Android phones: a personal device on a custom ROM, and a work device that stays on its stock firmware. Device models, codenames and serials are deliberately not recorded in this repository.

## Guides

- [Custom ROM](docs/custom-rom.md): bootloader, recovery and ROM for the personal phone.
- [Debloat](docs/debloat.md): what to disable on the stock work phone, and why only disable.
- [Shared setup](docs/shared-setup.md): apps, accounts and declarative settings both phones use.
- [App data backup](docs/app-data-backup.md): what app state is reachable without root, and how.
- [Playbooks](docs/playbooks.md): what to check when something on the phone stops working.

## Prerequisites

Everything below runs inside the devenv shell, which provides `adb`, `fastboot`, Go and `task`:

```sh
devenv shell
```

Enable USB debugging on the phone and authorize the host. Every command runs through `task`, never a built binary, because Go exists only inside devenv.

## Commands

```sh
task                      # list every task
task check                # gofmt, vet, tests
task report               # every package with the facts that decide a keep/kill call
task health               # launcher, keyboard, roles, crash buffer
task disable -- policy/packages.txt --dry-run
task disable -- policy/packages.txt
task restore              # re-enable everything the newest snapshot saw enabled
task obtainium            # install Obtainium, checksum verified
task apply                # dry run of profile.conf and apps.conf
task apply -- --yes       # write both
task verify               # report drift across both
task revert               # restore the values captured before the last apply
task ui-list              # app backup recipes and their stored artifacts
task ui-backup -- --app <package>    # export an app's own state
task ui-verify -- --app <package>    # re-export and diff
task ui-restore -- --app <package>   # replay the artifact, guarded
```

App recipes live in `policy/data/<app>/steps.json`, with that app's exports beside them. The folder is the app's normalized name, lowercase and path friendly, e.g. `yamlauncher`.

## License

[MIT](LICENSE)
