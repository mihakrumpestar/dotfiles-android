# Debloat

Debloating applies to the stock work phone. The personal phone on a custom ROM ships far less, but the tool is the same for both: it reports facts, and applies only what you approve.

## Reading the device

```sh
task report     # what is installed, with the facts that decide a keep/kill call
task health     # launcher, keyboard, roles, crash buffer
```

## Disabling

`policy/packages.txt` holds the packages disabled beyond the main pass, one per line with comments:

```sh
task disable -- policy/packages.txt --dry-run   # preview
task disable -- policy/packages.txt             # apply
task restore                                    # roll back the newest snapshot
```

`disable` snapshots first and prints the device's own message when a package refuses. `restore` puts back everything the snapshot had enabled, so packages you disabled yourself stay disabled.

`skills/android-debloat/SKILL.md` has the guidance an agent follows to read the report and explain packages to you. You make the call.

### Package lists

`policy/packages.txt` holds the packages disabled beyond the main pass, grouped by reason with a comment per group, and run with `task disable -- policy/packages.txt`. A single list because they are always applied together; splitting one or two entries per file was overhead with no payoff.

It names only packages that exist on the firmware it targets. An absent package is reported as drift rather than counted as a failure, and should be deleted rather than left to fail forever.

### Reading the report

```
state    tier             shared uid         sdk  rating       role    package
enabled  system/priv-app  android.uid.system  37  Recommended  -       com.samsung.android.fmm
enabled  system/priv-app  -                   37  Advanced     -       com.samsung.android.themestore
```

**shared uid** is what matters most. 87 packages on a stock Samsung share `android.uid.system`, so disabling one changes behaviour for all of them and the symptom will not name it. The two rows above share nothing except a rating list that ranks them backwards.

**rating** is an [upstream](https://github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation) opinion, fetched from `main` and revalidated on each run. Good for descriptions, not a verdict.

## Why only disable

Measured on Android 17:

| Package lives in | `pm uninstall --user 0` | Result |
| --- | --- | --- |
| `/system`, `/system_ext`, `/product`, `/vendor`, apex | refused | `only root can delete system app for a particular user` |
| `/data/app` (removable preload) | succeeds | **APK deleted**, `install-existing` cannot restore it |

`uninstall` either does nothing or destroys a removable preload beyond recovery. `disable-user` is reversible, so it is the only verb in the toolchain.

## When something on the phone stops working

[Playbooks](playbooks.md) is the fixed order of checks for the recurring failures after debloating: nothing handles an action any more, a control sits under the status bar, an app cannot see files, an OEM picker opens and closes, an app has no scripting interface, and the commands that report success and do nothing. Start there before requerying anything by hand.
