"""NetBird: the WireGuard mesh VPN."""

from uiu.dsl import app

APP = app(
    package="io.netbird.client",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "connection state notice"),
    },
    appops={
        "ACTIVATE_VPN": ("allow", "route through the NetBird network"),
    },
)
