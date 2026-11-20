"""The eight device commands.

Each function takes a device, does the work, prints what a person needs and
raises `CommandError` when the work did not succeed. The CLI turns that into
`androidctl: <message>` and exit 1. Nothing here touches `sys.argv` or a real
phone, so every path can be tested with a fake device.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import TextIO, TypedDict

from uiu import artifacts
from uiu import spec as app_module
from uiu.store import StoreError, app_files

from . import apps, debloat, profile, ratings, snapshots
from .adb import AdbError, Device

# The bootstrap list, package ids in install order.
INIT_FILE = "policy/init.txt"

# A starter that has not shown its process by now is not going to.
START_TIMEOUT = 30.0

# Wait between process probes.
POLL = 0.5


class CommandError(RuntimeError):
    """A command that could not do what it was asked, with nothing left to try."""


class ReportBase(TypedDict):
    """The fields every row carries."""

    pkg: str
    state: str
    tier: str


class ReportRow(ReportBase, total=False):
    """One package row: the JSON field names are a contract for agent prompts."""

    shared_user: str
    target_sdk: int
    rating: str
    rating_list: str
    description: str
    role: str


# ---------------------------------------------------------------- report


def report(
    device: Device,
    state: Path | str = ".state",
    *,
    as_json: bool = False,
    enabled_only: bool = False,
    out: TextIO | None = None,
) -> int:
    """One row per package with the facts that decide a keep or remove call."""
    stream: TextIO = out or sys.stdout
    states = debloat.states(device)
    uids, sdks = debloat.package_facts(device)
    tiers = debloat.tiers(device)
    upstream = ratings.load_ratings(state)
    holders = debloat.role_holders(device)

    role_of: dict[str, str] = {}
    for role in sorted(holders):
        holder = holders[role]
        if holder:
            role_of[holder] = role

    names = sorted(states)
    enabled = sum(1 for name in names if states[name] == debloat.ENABLED)
    rows: list[ReportRow] = []
    for name in names:
        if enabled_only and states[name] != debloat.ENABLED:
            continue
        rating = upstream.by_package.get(name, ratings.Rating())
        description = rating.description.split("\n", 1)[0]
        row: ReportRow = {
            "pkg": name,
            "state": states[name],
            "tier": tiers.get(name, ""),
        }
        if uids.get(name):
            row["shared_user"] = uids[name]
        if sdks.get(name):
            row["target_sdk"] = sdks[name]
        if rating.removal:
            row["rating"] = rating.removal
        if rating.list:
            row["rating_list"] = rating.list
        if description:
            row["description"] = description[:120]
        if role_of.get(name):
            row["role"] = role_of[name]
        rows.append(row)

    if as_json:
        # The field names are a contract for agent prompts. Key order is not;
        # this encoder sorts keys and escapes HTML characters.
        stream.write(artifacts.dumps(rows).decode())
        return 0

    print(f"# {device.serial}: {len(rows)} shown, {enabled} enabled", file=stream)
    print(f"# ratings: {upstream} ({ratings.UAD_BRANCH})", file=stream)
    _table(
        ["state", "tier", "shared uid", "sdk", "rating", "role", "package"],
        [
            [
                row["state"],
                row["tier"] or "-",
                str(row.get("shared_user", "")) or "-",
                str(row.get("target_sdk", "")) or "-",
                row.get("rating", "") or "-",
                row.get("role", "") or "-",
                row["pkg"],
            ]
            for row in rows
        ],
        stream,
    )
    return 0


def _table(header: list[str], rows: list[list[str]], out: TextIO) -> None:
    """Print columns padded to their widest cell, two spaces apart."""
    widths = [len(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    for row in [header, *rows]:
        line = "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        print(line.rstrip(), file=out)


# ---------------------------------------------------------------- health


def health(device: Device, *, out: TextIO | None = None) -> int:
    """Check boot, launchers, keyboard, roles and the crash buffer."""
    stream: TextIO = out or sys.stdout
    states = debloat.states(device)
    holders = debloat.role_holders(device)
    launchers = debloat.launchers(device)
    keyboard = debloat.keyboard(device)
    crashes = debloat.crashes(device)
    booted = debloat.boot_completed(device)

    active = [pkg for pkg in launchers if states.get(pkg) != debloat.DISABLED]

    problems = []
    if not booted:
        problems.append("boot not completed")
    if not active:
        problems.append("no enabled launcher: no home screen")
    if keyboard and states.get(keyboard) == debloat.DISABLED:
        problems.append(f"default keyboard {keyboard} is disabled")
    for role in sorted(holders):
        holder = holders[role]
        if holder and states.get(holder) == debloat.DISABLED:
            problems.append(f"{role} role is held by disabled {holder}")

    print(f"launcher   {', '.join(active) or 'none'}", file=stream)
    print(f"keyboard   {keyboard or 'unset'}", file=stream)
    for role in sorted(holders):
        print(f"{role.lower():<10} {holders[role] or 'unset'}", file=stream)
    print(f"crashes    {crashes}", file=stream)

    if problems:
        print(f"\n{len(problems)} problem(s):", file=stream)
        for problem in problems:
            print(f"  {problem}", file=stream)
        raise CommandError("health check failed")
    print("\nok", file=stream)
    return 0


# ---------------------------------------------------------------- disable


def disable(
    device: Device,
    packages: list[str],
    state: Path | str = ".state",
    *,
    dry_run: bool = False,
    no_snapshot: bool = False,
    out: TextIO | None = None,
) -> int:
    """Disable the named packages for user 0, snapshotting first."""
    stream: TextIO = out or sys.stdout
    if not packages:
        raise CommandError("no packages to disable")
    for pkg in packages:
        if not debloat.valid_package(pkg):
            raise CommandError(f"not a package name: {json.dumps(pkg)}")

    states = debloat.states(device)
    uids, _ = debloat.package_facts(device)

    if not dry_run and not no_snapshot:
        path = snapshots.write_package_snapshot(state, device.serial, states)
        print(f"snapshot: {path}", file=stream)

    failed = 0
    absent = 0
    for pkg in packages:
        note = f"  [shares {uids[pkg]}]" if pkg in uids else ""
        current = debloat.state_of(states, pkg)
        # Absence is checked before the dry-run branch, so a preview cannot
        # claim it would change a package this device does not have.
        if current == debloat.ABSENT:
            print(f"absent    {pkg}  not installed", file=stream)
            absent += 1
            continue
        if current == debloat.DISABLED:
            print(f"skipped   {pkg}  already disabled", file=stream)
            continue
        if dry_run:
            print(f"would     pm disable-user --user 0 {pkg}{note}", file=stream)
            continue
        ok, message = debloat.disable(device, pkg)
        if not ok:
            failed += 1
        verdict = "ok" if ok else "FAILED"
        print(f"{verdict:<8}  {pkg}  {message}{note}", file=stream)

    if failed:
        raise CommandError(f"{failed} of {len(packages)} did not disable; read the messages above")
    if absent:
        print(
            f"\n{absent} package(s) are not on this device; trim the list if they are gone for good",
            file=stream,
        )
    return 0


# ---------------------------------------------------------------- package restore


def restore_packages(
    device: Device,
    state: Path | str = ".state",
    *,
    from_path: Path | str | None = None,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    """Re-enable what the newest snapshot saw enabled and the device disabled."""
    stream: TextIO = out or sys.stdout
    path = Path(from_path) if from_path else snapshots.newest_package_snapshot(state, device.serial)
    snapshot = snapshots.read_package_snapshot(path)
    if snapshot.serial != device.serial:
        raise CommandError(f"snapshot is for {snapshot.serial}, device is {device.serial}")

    states = debloat.states(device)
    print(snapshot.summary(path), file=stream)

    to_enable = [pkg for pkg in snapshot.enabled_in() if states.get(pkg) == debloat.DISABLED]
    if not to_enable:
        print("nothing to restore", file=stream)
        return 0
    for pkg in to_enable:
        if not debloat.valid_package(pkg):
            raise CommandError(f"not a package name: {json.dumps(pkg)}")

    failed = 0
    for pkg in to_enable:
        if dry_run:
            print(f"would     pm enable --user 0 {pkg}", file=stream)
            continue
        ok, message = debloat.enable(device, pkg)
        if not ok:
            failed += 1
        verdict = "ok" if ok else "FAILED"
        print(f"{verdict:<8}  {pkg}  {message}", file=stream)

    if failed:
        raise CommandError(f"{failed} of {len(to_enable)} did not re-enable")
    return 0


# ---------------------------------------------------------------- profile apply


def apply(
    device: Device,
    files: list[str] | None = None,
    state: Path | str = ".state",
    *,
    yes: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Report every profile entry, and write the ones that differ with --yes."""
    stream: TextIO = out or sys.stdout
    errors: TextIO = err or sys.stderr
    entries = load_policy(files)
    installed = debloat.installed(device)

    applicable = []
    skipped: dict[str, str] = {}
    for entry in entries:
        missing = _missing_package(entry, installed)
        if missing:
            skipped[profile.key(entry)] = missing
            continue
        applicable.append(entry)

    before = {
        name: profile.SnapshotEntry(state=state, value=value)
        for name, (value, state) in profile.read_many(device, applicable).items()
    }

    changed: dict[str, profile.SnapshotEntry] = {}
    for entry in entries:
        key = profile.key(entry)
        if key in skipped:
            print(f"  {'skip':<6} {key}  ({skipped[key]} not installed)", file=stream)
            continue
        current = before.get(key, profile.SnapshotEntry(state=profile.State.UNSET))
        # A derived value that already matches is left alone: the platform
        # reasserts it. An unset entry is attempted, so a write that cannot
        # work shows up as IGNORED instead of a silent skip.
        same = current.state != profile.State.UNSET and profile.satisfied(
            entry, current.value, current.state
        )
        marker = "same" if same else ("write" if yes else "would")
        if yes and not same:
            changed[key] = current
        print(f"  {marker:<6} {key} = {entry.value}  ({entry.reason})", file=stream)

    note = _skip_note(len(skipped))
    if not yes:
        print(f"\ndry run; re-run with --yes to write{note}", file=stream)
        return 0
    if not changed:
        print(f"\nnothing to do; every applicable entry already matches{note}", file=stream)
        return 0

    path = snapshots.write_profile_snapshot(state, device.serial, changed)
    print(f"\nsnapshot: {path}", file=stream)

    failed = 0
    for entry in entries:
        if profile.key(entry) not in changed:
            continue
        ok, message = profile.store_for(entry).write(device, entry)
        if not ok:
            failed += 1
            print(f"  FAILED  {profile.key(entry)}  {message}", file=errors)
            continue
        ok, message = _readback(device, entry)
        if not ok:
            failed += 1
            print(f"  IGNORED {profile.key(entry)}  {message}", file=errors)
    if failed:
        raise CommandError(f"{failed} of {len(changed)} writes did not take effect")
    print(f"applied {len(changed)} entries{note}", file=stream)
    return 0


# ---------------------------------------------------------------- profile verify


def verify(
    device: Device,
    files: list[str] | None = None,
    *,
    out: TextIO | None = None,
) -> int:
    """Report where the device differs from the profiles. Writes nothing."""
    stream: TextIO = out or sys.stdout
    entries = load_policy(files)
    installed = debloat.installed(device)

    applicable = []
    skipped: dict[str, str] = {}
    for entry in entries:
        missing = _missing_package(entry, installed)
        if missing:
            skipped[profile.key(entry)] = missing
            continue
        applicable.append(entry)

    # One read per store family instead of one per entry: the settings list, the
    # package dumps and the app-op tables each answer all their entries at once.
    current_values = profile.read_many(device, applicable)

    drift = 0
    for entry in entries:
        key = profile.key(entry)
        if key in skipped:
            print(f"  {'skip':<6} {key}  ({skipped[key]} not installed)", file=stream)
            continue
        current, state = current_values[key]
        if state == profile.State.UNSET:
            print(f"  unset   {key} (want {_quote(entry.value)})", file=stream)
            drift += 1
        elif profile.satisfied(entry, current, state):
            print(f"  ok      {key} = {current}", file=stream)
        else:
            print(
                f"  drift   {key} is {_quote(current)} ({state.value}), want {_quote(entry.value)}",
                file=stream,
            )
            drift += 1
    if drift:
        raise CommandError(f"{drift} entr(ies) differ from the profile")
    print(f"\nall entries match{_skip_note(len(skipped))}", file=stream)
    return 0


# ---------------------------------------------------------------- profile revert


def revert(
    device: Device,
    state: Path | str = ".state",
    *,
    from_path: Path | str | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Write back the values captured before the last apply."""
    stream: TextIO = out or sys.stdout
    errors: TextIO = err or sys.stderr
    path = (
        Path(from_path)
        if from_path
        else snapshots.newest_file(Path(state) / "profile", device.serial)
    )
    snapshot = snapshots.read_profile_snapshot(path)
    before = snapshot.before()
    if snapshot.serial != device.serial:
        raise CommandError(f"snapshot is for {snapshot.serial}, device is {device.serial}")

    print(f"snapshot {path.name} ({snapshot.created})", file=stream)
    failed = 0
    skipped = 0
    restored = 0
    for key in sorted(before):
        if "/" not in key:
            raise CommandError(f"malformed snapshot key {key!r}")
        namespace, _, name = key.partition("/")
        store = profile.section_store(namespace)
        if store is None:
            failed += 1
            print(f"  FAILED  {key}  unknown section [{namespace}]", file=errors)
            continue
        entry = profile.Entry(namespace=namespace, key=name)
        previous = before[key]
        if previous.state == profile.State.IMPLICIT:
            # The platform derives the value, but apply may have written over
            # it. Reset so that explicit row does not remain.
            ok, message = store.reset(device, entry)
            if not ok:
                skipped += 1
                print(f"  skip    {key} ({message})", file=stream)
                continue
            ok, message = _reset_readback(device, store, entry)
            if not ok:
                failed += 1
                print(f"  FAILED  {key}  {message}", file=errors)
                continue
            restored += 1
            print(f"  reset   {key} (was {previous.value})", file=stream)
        elif previous.state == profile.State.EXPLICIT:
            entry = profile.Entry(namespace=namespace, key=name, value=previous.value)
            ok, message = store.write(device, entry)
            if ok:
                ok, message = _readback(device, entry)
            if not ok:
                failed += 1
                print(f"  FAILED  {key}  {message}", file=errors)
                continue
            restored += 1
            print(f"  restore {key} = {previous.value}", file=stream)
        else:
            ok, message = store.reset(device, entry)
            if not ok:
                skipped += 1
                print(f"  skip    {key} ({message})", file=stream)
                continue
            ok, message = _reset_readback(device, store, entry)
            if not ok:
                failed += 1
                print(f"  FAILED  {key}  {message}", file=errors)
                continue
            restored += 1
            print(f"  unset   {key}", file=stream)

    if failed:
        raise CommandError(f"{failed} of {len(before)} could not be restored")
    print(f"reverted {restored} entr(ies){_skip_note(skipped)}", file=stream)
    return 0


# ---------------------------------------------------------------- install


def install_app(
    device: Device,
    spec: apps.AppSpec,
    state: Path | str = ".state",
    *,
    asset: str = "",
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    """Resolve, verify and install an app from its release."""
    stream: TextIO = out or sys.stdout
    release = apps.latest_release(spec)
    name = asset
    if not name:
        result = device.shell("getprop ro.product.cpu.abilist")
        if not result.ok or not result.out.strip():
            raise CommandError("could not read ro.product.cpu.abilist from the device")
        name = apps.asset_for(spec, result.out.strip().split(","))
    apk = release.find(name)
    want = apps.expected_hash(spec, release, apk)

    print(apps.describe(release, apk, want), file=stream)
    if dry_run:
        print("\ndry run: nothing downloaded, nothing installed", file=stream)
        return 0

    path = apps.download(apk, want, Path(state) / spec.name)
    print(f"verified  {path}", file=stream)
    device.run("install", "-r", str(path))

    states = debloat.states(device)
    if debloat.state_of(states, spec.package) != debloat.ENABLED:
        raise CommandError(f"install reported success but {spec.package} is not enabled")
    print(f"installed {spec.package}", file=stream)
    if spec.hint:
        print(f"\nNext: {spec.hint}", file=stream)
    return 0


# ---------------------------------------------------------------- bootstrap

# Android ABI to the directory name a native library lives under on the device.
_LIB_DIR = {
    "arm64-v8a": "arm64",
    "armeabi-v7a": "arm",
    "armeabi": "armeabi",
    "x86": "x86",
    "x86_64": "x86_64",
    "mips": "mips",
    "mips64": "mips64",
    "riscv64": "riscv64",
}

# An app directory carries a base64 hash, so "=" is part of a real path.
_STARTER_RE = re.compile(r"^/[A-Za-z0-9._~+/=-]+$")


def start_app(
    device: Device,
    spec: apps.AppSpec,
    *,
    out: TextIO | None = None,
    timeout: float = START_TIMEOUT,
) -> None:
    """Run the app's bundled starter and wait until its process is up.

    The starter kills any server it finds first, so a run after an app update
    replaces the old server instead of leaving it in place.
    """
    stream: TextIO = out or sys.stdout
    start = spec.start
    if start is None:
        return

    apk = _apk_path(device, spec)
    libdir = apk.parent / "lib"
    abis = [
        abi for abi in device.shell("getprop ro.product.cpu.abilist").out.strip().split(",") if abi
    ]

    candidate = ""
    for abi in abis:
        libabi = _LIB_DIR.get(abi)
        if libabi is None:
            continue
        listed = device.shell(f"ls {libdir / libabi / start.library}")
        lines = [line.strip() for line in listed.out.splitlines() if line.strip()]
        if lines:
            candidate = lines[0]
            break
    if not candidate:
        raise CommandError(f"could not find {start.library} under {libdir} for {', '.join(abis)}")
    if not _starter_path(candidate, start.library):
        raise CommandError(f"unexpected starter path {_quote(candidate)}")

    result = device.shell(candidate)
    if not result.ok:
        message = result.err.strip() or result.out.strip()
        raise CommandError(f"{spec.name} starter failed: {message}")

    deadline = time.monotonic() + timeout
    while True:
        if _pid(device, start.process):
            print(f"started   {spec.name} ({start.process})", file=stream)
            return
        # The deadline is checked before sleeping, so timeout=0 fails at once.
        if time.monotonic() >= deadline:
            raise CommandError(f"{spec.name} started but {start.process} is not running")
        time.sleep(POLL)


def _apk_path(device: Device, spec: apps.AppSpec) -> Path:
    result = device.shell(f"pm path {spec.package}")
    lines = [line.strip() for line in result.out.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("package:"):
        raise CommandError(f"could not find the APK path for {spec.package}")
    return Path(lines[0][len("package:") :])


def _pid(device: Device, process: str) -> bool:
    result = device.shell(f"pidof {process}")
    return result.ok and bool(result.out.strip())


def _starter_path(candidate: str, library: str) -> bool:
    return bool(
        _STARTER_RE.match(candidate) and ".." not in candidate and candidate.endswith("/" + library)
    )


def init_device(
    device: Device,
    specs: list[apps.AppSpec],
    state: Path | str = ".state",
    *,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    """Install each bootstrap app in order, then start its service."""
    for spec in specs:
        install_app(device, spec, state, dry_run=dry_run, out=out)
        if not dry_run:
            start_app(device, spec, out=out)
    return 0


# ---------------------------------------------------------------- shared helpers


def load_policy(
    files: list[str] | None = None, apps: Path | str = "policy/data"
) -> profile.Profile:
    """Every declared entry: the phone's own settings plus one file per app.

    The app files are what used to be `policy/apps.conf`, one per app, so a row
    sits next to the app it belongs to. App files come from both the policy and
    the private trees, so an app that must not be public still declares its
    device state. A key declared twice is an error, because two sources
    disagreeing about one value is a bug nobody would see.
    """
    sources: list[tuple[str, profile.Profile]] = []
    for path in files or ["policy/profile.conf"]:
        sources.append((path, load_profiles([path])))
    try:
        paths = app_files(apps)
    except StoreError as error:
        raise CommandError(str(error)) from error
    for path in paths:
        sources.append((str(path), _app_rows(path)))
    merged: profile.Profile = []
    seen: dict[str, str] = {}
    for source, entries in sources:
        for entry in entries:
            name = profile.key(entry)
            if name in seen:
                raise CommandError(f"{source}: {name} is also declared in {seen[name]}")
            seen[name] = source
            merged.append(entry)
    if not merged:
        raise CommandError(f"no entries in {', '.join(files or ['policy/profile.conf'])}")
    return merged


def _app_rows(path: Path) -> profile.Profile:
    """One app file's device-state rows, as profile entries.

    The namespace and key are checked here, so a typo in an app file fails
    before the device is opened, exactly as it would in a `.conf` profile.
    """
    try:
        app = app_module.parse(path)
    except app_module.RecipeError as error:
        raise CommandError(str(error)) from error
    rows: profile.Profile = []
    for namespace, entry_key, value, reason in app.rows():
        if profile.section_store(namespace) is None:
            raise CommandError(f"{path}: unknown section [{namespace}]")
        rows.append(profile.Entry(namespace, entry_key, value, reason))
    return rows


def load_profiles(paths: list[str]) -> profile.Profile:
    """Read the named profiles and merge them into one declaration."""
    merged: profile.Profile = []
    seen: dict[str, str] = {}
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise CommandError(f"reading {path}: not UTF-8: {error}") from error
        except OSError as error:
            raise CommandError(f"reading {path}: {error}") from error
        try:
            entries = profile.parse(text)
        except ValueError as error:
            raise CommandError(f"{path}: {error}") from error
        for entry in entries:
            key = profile.key(entry)
            if key in seen:
                raise CommandError(f"{path}: {key} is also declared in {seen[key]}")
            seen[key] = path
            merged.append(entry)
    if not merged:
        raise CommandError(f"no entries across {', '.join(paths)}")
    return merged


def _missing_package(entry: profile.Entry, installed: set[str]) -> str:
    try:
        packages = profile.packages(entry)
    except ValueError as error:
        raise CommandError(f"{profile.key(entry)}: {error}") from error
    for pkg in packages:
        if pkg not in installed:
            return pkg
    return ""


def _readback(device: Device, entry: profile.Entry) -> tuple[bool, str]:
    try:
        got, state = profile.store_for(entry).read(device, entry)
    except (AdbError, profile.ProfileError) as error:
        return False, str(error)
    if not profile.satisfied(entry, got, state):
        return False, (f'device holds "{got}" ({state.value}), want "{entry.value}"')
    return True, ""


def _reset_readback(device: Device, store: profile.Store, entry: profile.Entry) -> tuple[bool, str]:
    try:
        got, state = store.read(device, entry)
    except (AdbError, profile.ProfileError) as error:
        return False, str(error)
    if not profile.reset_ok(entry, got, state):
        return False, f'device holds "{got}" ({state.value}) after the reset'
    return True, ""


def _skip_note(count: int) -> str:
    return f" ({count} skipped)" if count else ""


def _quote(value: str) -> str:
    """Quote a value for an error message, with JSON's escaping rules."""
    return json.dumps(value, ensure_ascii=False)
