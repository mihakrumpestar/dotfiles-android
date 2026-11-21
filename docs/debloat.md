# Debloat

Debloating means disabling preinstalled apps. It applies to the stock work phone. The custom-ROM phone ships fewer apps, but the tool is the same for both: it reports facts and applies only what you approve.

## Reading the device

```sh
task report     # what is installed, with the facts that decide a keep/kill call
task health     # launcher, keyboard, roles, crash buffer
```

## Disabling

Run the list against the phone:

```sh
task disable -- policy/packages.txt --dry-run   # preview
task disable -- policy/packages.txt             # apply
task restore                                    # roll back the newest snapshot
```

`disable` snapshots first and prints the device's own message when a package refuses. `restore` puts back everything the snapshot had enabled, so packages you disabled yourself stay disabled.

`skills/android-debloat/SKILL.md` has the guidance an agent follows to read the report and explain packages to you. You make the call.

### Package lists

`policy/packages.txt` holds the packages disabled beyond the main pass, grouped by reason with a comment per group. Apply it with `task disable -- policy/packages.txt`. One list, because they are always applied together; splitting one or two entries per file was overhead with no payoff.

The list names only packages that exist on the firmware it targets. An absent package is reported as drift, not a failure. Delete it rather than leaving it to fail forever.

Samsung Keyboard is not in the list. Samsung protects the package: `pm disable-user` fails and the app's Disable button is greyed out. The system also re-adds its IME to `secure/enabled_input_methods`, so the keyboard stays listed; HeliBoard is the default. Its clipboard access is denied in `policy/data/samsungkeyboard/app.py`, which stops the "pasted from your clipboard" notice. The same profile entry keeps Google voice input out of the enabled list.

### Reading the report

```
state    tier             shared uid         sdk  rating       role    package
enabled  system/priv-app  android.uid.system  37  Recommended  -       com.samsung.android.fmm
enabled  system/priv-app  -                   37  Advanced     -       com.samsung.android.themestore
```

**shared uid** matters most. On a stock Samsung, 87 packages share `android.uid.system`, so disabling one changes behaviour for all of them and the symptom will not name it. The two rows above share nothing except a rating list that ranks them backwards.

**rating** is an [upstream](https://github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation) opinion, fetched from `main` and revalidated on each run. Good for descriptions, not a verdict.

## Why only disable

Measured on Android 17:

| Package lives in | `pm uninstall --user 0` | Result |
| --- | --- | --- |
| `/system`, `/system_ext`, `/product`, `/vendor`, apex | refused | `only root can delete system app for a particular user` |
| `/data/app` (removable preload) | succeeds | **APK deleted**, `install-existing` cannot restore it |

`uninstall` either does nothing or destroys a removable preload beyond recovery. `disable-user` is reversible, so it is the only verb in the toolchain.

## When something on the phone stops working

[Playbooks](playbooks.md) gives the fixed order of checks for failures that recur after debloating. The cases: nothing handles an action any more, a control sits under the status bar, an app cannot see files, an OEM picker opens and closes, an app has no scripting interface, and commands that report success and do nothing. Start there before requerying anything by hand.
