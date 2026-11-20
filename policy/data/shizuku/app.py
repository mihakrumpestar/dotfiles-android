"""Shizuku-Next: the privileged backend other apps borrow adb from."""

from uiu.dsl import app

APP = app(
    package="moe.shizuku.privileged.api",
    install={
        "assets": {"universal": "shizuku-next.apk"},
        "checksum": "digest",
        "hint": "grant the auto-start permission, then enable Start on boot in Shizuku",
        "release": "https://api.github.com/repos/rushiranpise/Shizuku-Next/releases/latest",
        "start": {"library": "libshizuku.so", "process": "shizuku_server"},
    },
    permissions={
        "android.permission.NEARBY_WIFI_DEVICES": (
            "granted",
            "find the wireless debugging service on the network",
        ),
    },
    appops={
        "GET_USAGE_STATS": ("allow", "hiding lists need the foreground app"),
        "RUN_ANY_IN_BACKGROUND": ("allow", "the server outlives the manager"),
    },
    doze=("allow", "the server must be up when the first ring arrives"),
)
