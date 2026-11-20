"""Fossify Messages: the SMS app, and the role that makes it one."""

from uiu.dsl import app

APP = app(
    package="org.fossify.messages",
    roles={
        "android.app.role.SMS": "Fossify Messages is the SMS app",
    },
)
