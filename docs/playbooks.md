# Playbooks

Fixed order of checks for the recurring "why does this not work" questions on a debloated device. Each one exists because it was rediscovered the slow way. Follow the order; the answer is usually in the first two steps.

Two rules apply to all of them:

- **Verify the artifact, not the command.** `Success`, exit 0, or a plausible log line proves nothing. Read the resulting state back: a screenshot, `dumpsys`, or the value itself.
- **Prefer the platform's own query over your model of the platform.** Ask `cmd package query-activities`, `dumpsys window`, `cmd appops get`. Guessing which component or permission is involved is how an hour disappears.

## The device answers in four places

| question | command |
| --- | --- |
| who can handle an intent | `cmd package query-activities --components -a <ACTION> [-d <uri>]` |
| is it installed, and enabled | `pm list packages -e` / `-d` / `-u` (includes uninstalled-but-recorded) |
| what permissions and appops it holds | `dumpsys package <pkg>` and `cmd appops get <pkg>` |
| what the window actually looks like | `dumpsys window windows` |

Add `-d` to `pm list packages` for disabled, `-u` for every known package including ones with only an install record. The three lists disagree in ways that matter: see "A successful command did nothing".

## Nothing handles an action any more

Symptom: share sheet, "set as", or "open with" shows no app, or only an unexpected one.

1. `cmd package query-activities --components -a <ACTION>` for the exact action. For files also pass the type or URI: `-a android.intent.action.VIEW -d "file:///...jpg" -t image/jpeg`.
2. If the expected app appears, check it is enabled: `pm list packages -e | grep -qx package:<pkg>`, then `pm list packages -d` to confirm it is not in the disabled set.
3. If it does not appear at all, it is either uninstalled or its intent filter never matched. Check `dumpsys package <pkg> | grep -A4 <ACTION>` to see the filters it declares before concluding it is gone.
4. Confirm the replacement is installed **before** disliking the result. A disabled app with no successor is a missing feature, not a bug.

Related: `pm list packages -3` lists third-party packages, which is the fastest way to see what the user actually installed versus what shipped.

## A control sits under the status bar

Symptom: in one app, the top row of buttons is partly hidden and hard to tap. Other apps are fine.

1. Measure the bar, do not estimate it: `dumpsys window windows | grep -A3 "Window{.*StatusBar"` and read `mAttrs={(0,0)(fillx<H>)`. `<H>` is the real height.
2. Compare with the cutout inset, which is usually smaller: `dumpsys window displays | grep -o "DisplayCutout{insets=Rect([^)]*)"`. Apps that pad by the cutout alone lose those few pixels to the bar.
3. Identify the component on screen: `dumpsys window | grep mCurrentFocus`, then dump the UI tree and read the topmost element's `bounds`. If its `y1` is less than the bar height, that app is the bug.
4. Decide scope by measurement, not by feel. Repeat step 3 across several apps. If only one is affected it is that app's layout; if many are, look at display overrides (`wm size`, `wm density`, `settings get secure display_cutout_emulation`) before blaming any of them.
5. Check the app's `targetSdk`. At 35 and above the platform forces edge-to-edge, and an app that never handled insets will draw under the bars. `dumpsys package <pkg> | grep targetSdk`.
6. The platform change is `ENFORCE_EDGE_TO_EDGE` (id 309578419, since target 35) plus `DISABLE_OPT_OUT_EDGE_TO_EDGE` (id 377864165, since 36). List them with `dumpsys platform_compat | grep -i edge`. Per-app opt-out is `am compat enable <id> <pkg>`, but it refuses on a user build: `Cannot override a change on a non-debuggable app and user build`. Do not spend time on it; the choice is that app's version or accept it.

There is no global setting that turns forced edge-to-edge off. If the answer is "one app's layout bug", say so and stop, rather than hunting for a device-level knob that does not exist.

## An app cannot see files

Symptom: an app's own file browser, or its export/import dialog, shows nothing. A system file picker on the same screen lists everything fine.

Those are two different mechanisms and only one is the picker:

- **The system picker** (DocumentsUI) goes through a document provider. If it lists files, storage and the providers are healthy.
- **The app's own browser** reads filesystem paths directly and needs storage grants of its own.

1. Check both gates, because a permission alone is not enough: `dumpsys package <pkg> | grep -E "READ_EXTERNAL_STORAGE:|WRITE_EXTERNAL_STORAGE:"` and `cmd appops get <pkg>` for the same two names plus `LEGACY_STORAGE`.
2. Grant the gates, then force-stop and relaunch. A running process does not pick up new grants.
3. If the picker itself is empty, test the provider directly: `content query --uri content://com.android.externalstorage.documents/root` for the roots, then `.../document/primary%3A` for a specific directory. Compare against `ls /storage/emulated/0` to see whether the provider or the files are missing.
4. `MANAGE_EXTERNAL_STORAGE` (all files access) is a third, separate gate. Apps that ask for it usually still work media-scoped, and the prompt is their own.

Grants do not survive a reinstall. Record the commands next to the app, not in a chat log.

## An OEM picker opens and immediately closes

Symptom: you pick the system handler and nothing happens, with no error.

1. Confirm the activity exists and is the declared handler: `dumpsys package <pkg> | grep -A4 <ACTION>` gives the component that declares the filter. Guessing `.SomethingActivity` from the package name wastes attempts; `Error type 3 ... does not exist` is that guess failing.
2. Launch that exact component: `am start -n <pkg>/<Activity>`. If it starts and then vanishes, clear the log buffer, reproduce, and read: `logcat -c`, launch, then grep for the package. Look for `FATAL`, `SecurityException`, `Permission Denial`, `result code=-92`.
3. Silence is the answer: no crash, no denial, activity destroyed right after creation means the OEM build is broken and not something to fix. Look for a maintained replacement that declares the same intent, and use it instead.
4. Record which handler to pick, since the resolver offers the broken one first. Telling the user "pick Gallery, not Wallpaper and style" is the deliverable.

## An app has no scripting interface

Symptom: the change you need can only be made in the app's UI. No API, no CLI, no deep link.

1. Check for an export/import format before automating anything. Settings screens usually export JSON, and importing a hand-written file is one operation instead of twenty taps. Copy the schema from a real export rather than inventing it; then strip credentials from the generated file.
2. If import exists but the app rejects a URL under a certain form, prefer importing the same object as a file. A file goes through the app's own parser, so it has to accept it.
3. Automate only when there is no file path. The loop is: `uiautomator dump /sdcard/ui.xml`, `adb pull`, parse `bounds`, tap the centre. Re-dump after every tap that changes the screen; a stale tree produces taps at coordinates that no longer mean anything.
4. Dismiss the keyboard before tapping anything low on the screen, or the tap lands on a key. `KEYCODE_BACK` or the IME action key, then confirm the element is still where the dump said.
5. `input text` mangles some strings. Type the shortest form that parses (`github.com/owner/repo`, not a URL with quotes), verify with a screenshot, and prefer `KEYCODE_DEL` loops over assuming the field was cleared.

## A successful command did nothing

Every one of these returns success and changes nothing. They are the reason "verify the artifact" is rule one.

| command | what it actually did |
| --- | --- |
| `pm uninstall <system pkg>` | Reports `Success`; the APK stays in place if it lives on a read-only partition or in an APEX. For a `/data/app` preload it **deletes the APK** and is unrecoverable. |
| `pm uninstall --user 0` | On a read-only-partition app, refused with `only root can delete system app for a particular user`. |
| `cmd package install-existing` | Restores the install record. It does not re-enable anything. |
| `settings put <any namespace> <key>` | Accepts any namespace. A key in the wrong store creates a row nothing reads. |
| `settings put system <ns>/<key>` | Creates a legitimate row whose name contains a slash. Looks right, is never read. |
| `am start` | Returns "Starting:" even when the activity then dies. The intent launching is not the UI appearing. |

The pattern: separate the store or path from the reader. Settings provider, DeviceConfig, and app-private storage are three different stores that fail differently, and only the one the framework reads matters.

## Confirming a destructive or system-level change

- Snapshot before, always. `restore` is worth more than any amount of care.
- Change one thing, then check the thing it could break. Two telemetry packages sharing `android.uid.system` are two separate experiments, not one batch.
- For anything touching telephony, roles, or home, check the actual service rather than assuming: `getprop gsm.sim.state`, `dumpsys telephony.registry | grep -o "mVoiceRegState=[^,]*"`, `cmd role get-role-holders android.app.role.<ROLE>`.
- For a changed intent handler, prove the intent resolves: query it after the change, not before.

## Writing the result down

When a workaround takes more than two attempts, the useful output is not the fix, it is the reason the obvious fix was wrong. Put it next to the artifact it explains: a comment in the policy file, a note in the app's section of the README, or a playbook here. Anything bought with an hour should not be bought twice.
