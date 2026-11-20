"""Orbot: the Tor proxy and its VPN mode."""

from uiu.dsl import app

APP = app(
    package="org.torproject.android",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "proxy state notice"),
    },
    appops={
        "ACTIVATE_VPN": ("allow", "route through Tor"),
        "SCHEDULE_EXACT_ALARM": ("allow", "start and stop at the set time"),
    },
)
