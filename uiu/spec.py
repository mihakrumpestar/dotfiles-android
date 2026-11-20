"""One app, one file: `<app>/app.py`, public policy or the private tree.

The file holds everything the repository knows about one app: its package, how
to install it, the device state it needs, and the walk through its own screens.
Nothing about an app lives anywhere else, and the package id is written once.

    from uiu.dsl import app, tap, wait

    APP = app(
        package="com.example.demo",
        install={
            "release": "https://api.github.com/repos/example/demo/releases/latest",
            "checksum": "asset",
            "assets": {"universal": "demo.apk"},
        },
        permissions={
            "android.permission.POST_NOTIFICATIONS": ("granted", "show a notice"),
        },
        artifact="demo.json",
        backup=[tap(desc="Settings"), tap(text="Export")],
        restore=[tap(text="Import")],
    )

An app with no `backup`, `restore`, `setup` or `artifact` is policy-only: install
facts and device state, no screen walk. `APP` must be an `App`, set at module
level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .recipe import APP_FILE, Recipe, RecipeError, parse_private, valid_package, validate

# A runtime permission or app op name, without the package. The package is the
# app's own, so repeating it in every row would be a second place to get it wrong.
_POLICY_NAME_RE = re.compile(r"[A-Za-z0-9_.*]+")
_MODE_RE = re.compile(r"[a-z]+")
_ROLE_RE = re.compile(r"android\.app\.role\.[A-Z_]+")
_FOLDER_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_INSTALL_KEYS = ("assets", "checksum", "hint", "release", "start")
_PERMISSION_VALUES = ("granted", "revoked")
_DOZE_VALUES = ("allow", "ignore")


@dataclass(frozen=True)
class App:
    """Everything one app declares, keyed by the package it provides."""

    name: str
    package: str
    # The install facts: release URL, checksum source, one asset per ABI and an
    # optional hint and starter. None means the app arrives some other way, such
    # as the Play Store.
    install: dict[str, Any] | None = None
    # Permission or app op name -> (device value, why it is wanted).
    permissions: dict[str, tuple[str, str]] = field(default_factory=dict)
    appops: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Role name -> why this app should hold it. A role is a system job such as
    # the SMS app, and this app is the one asking for it.
    roles: dict[str, str] = field(default_factory=dict)
    # Whether this app may run while the phone is in Doze, and why.
    doze: tuple[str, str] | None = None
    # The screen walk, or None for a policy-only app.
    recipe: Recipe | None = None
    dir: Path | None = None

    @property
    def path(self) -> Path:
        return (self.dir / APP_FILE) if self.dir is not None else Path(APP_FILE)

    def rows(self) -> list[tuple[str, str, str, str]]:
        """The app's device-state rows as `(namespace, key, value, reason)`.

        This is the one place the app file meets the profile format, so a row
        cannot be spelled differently by two readers.
        """
        rows: list[tuple[str, str, str, str]] = [
            ("permissions", f"{self.package}:{name}", value, reason)
            for name, (value, reason) in self.permissions.items()
        ]
        rows += [
            ("appops", f"{self.package}:{name}", value, reason)
            for name, (value, reason) in self.appops.items()
        ]
        rows += [("role", role, self.package, reason) for role, reason in self.roles.items()]
        if self.doze is not None:
            rows.append(("deviceidle", self.package, self.doze[0], self.doze[1]))
        return rows


def _map(
    raw: Any, where: str, allowed: tuple[str, ...] | None = None
) -> dict[str, tuple[str, str]]:
    """Read `{name: (value, reason)}`, refusing anything a store could not use.

    The reason is required for the same reason a `profile.conf` row needs one: a
    change nobody can justify later is indistinguishable from a mistake.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RecipeError(f"{where} is an object of name: (value, reason)")
    found: dict[str, tuple[str, str]] = {}
    for name in raw:
        if not isinstance(name, str) or not _POLICY_NAME_RE.fullmatch(name):
            raise RecipeError(f"{where}: invalid name {name!r}; letters, digits, dot, asterisk")
        pair = raw[name]
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise RecipeError(f'{where}: {name} is (value, reason), e.g. ("granted", "why")')
        value, reason = pair
        if not isinstance(value, str) or not _MODE_RE.fullmatch(value):
            raise RecipeError(f"{where}: {name} has invalid value {value!r}")
        if allowed is not None and value not in allowed:
            raise RecipeError(f"{where}: {name} is one of {', '.join(allowed)}, got {value!r}")
        if not isinstance(reason, str) or reason.strip() == "":
            raise RecipeError(f"{where}: {name} needs a reason")
        found[name] = (value, reason.strip())
    return found


def _roles(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RecipeError("roles is an object of role: reason")
    found: dict[str, str] = {}
    for role in raw:
        if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
            raise RecipeError(f"roles: {role!r} is not an android.app.role name")
        reason = raw[role]
        if not isinstance(reason, str) or reason.strip() == "":
            raise RecipeError(f"roles: {role} needs a reason")
        found[role] = reason.strip()
    return found


def _install(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RecipeError("install is an object: release, checksum, assets, optional hint, start")
    for key in raw:
        if key not in _INSTALL_KEYS:
            raise RecipeError(f'install: unknown field "{key}"')
    return dict(raw)


def _doze(raw: Any) -> tuple[str, str] | None:
    if raw is None:
        return None
    return _map({"doze": raw}, "doze", _DOZE_VALUES)["doze"]


def _recipe(
    *,
    package: str,
    artifact: Any,
    backup: Any,
    restore: Any,
    setup: Any,
    volatile: Any,
    redact: Any,
    private: Any,
    sort: Any,
) -> Recipe | None:
    """The screen walk, or None when the app has none.

    A recipe is built with the same `Recipe` the backup engine runs, so an app
    file cannot describe a walk the runner would refuse.
    """
    steps = list(backup)
    back = list(restore)
    turns = list(setup)
    if not steps and not back and not turns and not artifact:
        return None
    fields, private_file = parse_private(private or None)
    built = Recipe(
        package=package,
        artifact=artifact,
        backup=steps,
        restore=back,
        setup=turns,
        volatile=list(volatile),
        redact=list(redact),
        private=fields,
        private_file=private_file,
        sort=list(sort),
    )
    validate(built)
    return built


def build(
    *,
    package: Any,
    install: Any = None,
    permissions: Any = None,
    appops: Any = None,
    roles: Any = None,
    doze: Any = None,
    artifact: Any = "",
    backup: Any = (),
    restore: Any = (),
    setup: Any = (),
    volatile: Any = (),
    redact: Any = (),
    private: Any = (),
    sort: Any = (),
) -> App:
    """Build and validate one app.

    The package is checked once, here, and then handed to the recipe, so the two
    cannot disagree about which app the file describes.
    """
    if not isinstance(package, str) or not valid_package(package):
        raise RecipeError(f"package: {package!r} is not an Android package id")
    return App(
        name="",
        package=package,
        install=_install(install),
        permissions=_map(permissions, "permissions", _PERMISSION_VALUES),
        appops=_map(appops, "appops"),
        roles=_roles(roles),
        doze=_doze(doze),
        recipe=_recipe(
            package=package,
            artifact=artifact,
            backup=backup,
            restore=restore,
            setup=setup,
            volatile=volatile,
            redact=redact,
            private=private,
            sort=sort,
        ),
    )


def parse(path: Path) -> App:
    """Load one app's file from `app.py`, the way Python runs a script.

    An app file is committed policy from the policy or private tree, not a
    plugin fetched from the network, so it runs in this process.
    """
    if path.name != APP_FILE:
        raise RecipeError(f"{path}: an app file must be named {APP_FILE}")
    try:
        body = path.read_bytes().decode()
    except UnicodeDecodeError as error:
        raise RecipeError(f"{path}: not UTF-8: {error}") from error
    except OSError as error:
        raise RecipeError(f"reading {path}: {error.strerror or error}") from error
    namespace: dict[str, Any] = {
        "__file__": str(path),
        "__name__": f"uiu_app_{path.parent.name}",
    }
    try:
        exec(compile(body, str(path), "exec"), namespace)  # noqa: S102
    except RecipeError as error:
        raise RecipeError(f"{path}: {error}") from error
    except Exception as error:
        raise RecipeError(f"{path}: {type(error).__name__}: {error}") from error
    found = namespace.get("APP")
    if not isinstance(found, App):
        raise RecipeError(f"{path}: set APP = app(...) at module level")
    name = path.parent.name
    if not _FOLDER_RE.fullmatch(name):
        raise RecipeError(
            f'{path}: folder "{name}" is not a normalized app name; use lowercase letters,'
            " digits, dot, dash or underscore"
        )
    return App(
        name=name,
        package=found.package,
        install=found.install,
        permissions=found.permissions,
        appops=found.appops,
        roles=found.roles,
        doze=found.doze,
        recipe=found.recipe,
        dir=path.parent,
    )
