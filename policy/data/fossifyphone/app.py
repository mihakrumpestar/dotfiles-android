"""Fossify Phone: the dialer, and the role that makes it one."""

from uiu.dsl import app

APP = app(
    package="org.fossify.phone",
    roles={
        "android.app.role.DIALER": "Fossify Phone is the dialer",
    },
)
