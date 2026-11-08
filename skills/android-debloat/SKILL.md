---
name: android-debloat
description: >
  Help a human decide which preinstalled Android apps to disable on a connected
  device over adb, then apply exactly what they approve. Use for "debloat my
  phone", "what is this package", "is it safe to disable X", "which of these are
  bloat", "review my disabled apps". You research and explain; the human decides.
---

# Android debloat

Your job is to make a human's decision easy and well-informed. Not to make it
for them. They are holding the phone and know what they use; you can read the
package list and explain what each thing does.

**You never disable anything the user has not approved, one package at a time.**
No "apply all recommended". No silent cleanup. If you catch yourself thinking a
package is obviously bloat, that is the moment to ask.

## Tools

Context lives in [`../../docs/debloat.md`](../../docs/debloat.md) and [`../../docs/shared-setup.md`](../../docs/shared-setup.md); this file is the judgement an agent applies while reading the report. Run everything inside the devenv shell (`devenv shell`, or prefix with `devenv shell --`). Go is not installed on the host, and there is no prebuilt binary. Use the Taskfile, never a raw `go run`: `Taskfile.yml` is the only place a command is defined, so a task name is the interface and the invocation stays correct if it ever changes.

Reading the device, changes nothing:

```sh
task report                                  # every package with the deciding facts
task report -- --enabled                     # only currently enabled ones
task report -- --json                        # machine-readable
task health                                  # launcher, keyboard, roles, crash buffer
```

Applying, only after approval, from `policy/packages.txt`:

```sh
task disable -- policy/packages.txt --dry-run   # preview
task disable -- policy/packages.txt             # apply
task restore -- --dry-run                                 # what a rollback would bring back
task restore                                              # roll back the newest snapshot
```

Note the `--` before flags: `task disable -- <file> --dry-run` passes `<file>` as the list and `--dry-run` through `CLI_ARGS`.

When something on the device stops working rather than needing a decision, read [`../../docs/playbooks.md`](../../docs/playbooks.md). It gives the fixed order of checks for missing intent handlers, controls under the status bar, apps that cannot see files, dead OEM pickers, apps with no scripting interface, and the commands that report success and change nothing. Work the steps in order and verify the resulting state, not the command's output.

`disable` snapshots first and prints the device's own reply per package, with the
reason extracted from the stack trace rather than its last line. Never wrap it in
a loop that hides output.

## Never

- **Never uninstall.** No `pm uninstall`, no `pm clear`. Measured on Android 17:
  `uninstall` is refused for read-only partitions and *deletes the APK* of a
  removable preload in `/data/app`, unrecoverable by `install-existing`. Two
  packages on this device were destroyed that way. Disable is reversible;
  uninstall is not.
- **Never batch-approve.** Read the approval as covering exactly the packages
  listed, this time.
- **Never disable** the only launcher, keyboard, dialer, SMS app or emergency
  app, or anything holding a role in the report's `role` column.
- **Never guess at a package's purpose.** If the rating has no description and
  the name is opaque, say you do not know and leave it enabled.

## Reading the report

One line per package:

```
state     tier                shared uid                    sdk  rating       role       package
enabled   system/priv-app     android.uid.system             37  Recommended  -          com.samsung.android.fmm
enabled   system/priv-app     -                              37  Advanced     -          com.samsung.android.themestore
```

Columns that change a verdict, in order of weight:

- **shared uid**: the strongest signal. `android.uid.system` is shared by 87
  packages on a stock Samsung; disabling one changes behaviour for all of them
  and the eventual symptom will not name the package. Alone on its uid (a `-`)
  is a genuinely low-risk package.
- **role**: `HOME`, `DIALER`, `SMS`, `EMERGENCY` holders are load-bearing.
- **tier**: `data/app` is a removable preload; `apex` cannot be disabled at all.
- **sdk**: well below the OS version means abandoned, not that it is safe.
- **rating**: upstream opinion, useful as a description source, unreliable as a
  verdict. It is fetched from upstream `main` on every run (the header line says
  whether it was fetched, revalidated or served from cache), so it may differ
  from what you read last time. It is anti-correlated with real risk on this
  device: `fmm`
  (Find My Mobile) is `Recommended` while sharing `android.uid.system`;
  `themestore` is `Advanced` while alone on its own uid. Weight the device
  facts over the rating every time.

## How to present a recommendation

Group by confidence, not by rating tier. For each package give one sentence on
what stops working if it goes. Then stop and wait.

> **Low risk, alone on their uid, nothing depends on them**
> - `com.samsung.android.themestore` - Galaxy Themes store. Installed themes keep
>   working; you lose the storefront and its Samsung account nag.
>
> **Would work, but you lose a feature**
> - `com.samsung.android.app.sharelive` - Quick Share. Disappears from the share
>   sheet; Nearby Share still works.
>
> **Not touching, and why**
> - `com.samsung.android.fmm` - Find My Mobile. Rated `Recommended` upstream, but
>   it shares `android.uid.system` with 86 packages. Losing remote lock and wipe
>   is not worth it unless you say otherwise.
>
> **I do not know what these do**, so they stay enabled:
> - `com.samsung.android.qmdservice`, `com.sec.cpagent`

Ask which groups they want. Apply per package. Run `task health`, then ask
them to actually try the phone: open settings, type, place a call, take a photo.
Then report what you disabled and what you left, and why.

## When asked "is X safe"

Answer with the device facts, not the rating: uid, role, dependencies, tier,
what visibly breaks. If you do not know, say so. "Probably fine" is not an answer
worth giving when the failure mode is a bootloop.
