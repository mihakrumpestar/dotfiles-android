"""Discord."""

from uiu.dsl import app

APP = app(
    package="com.discord",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "message notices"),
    },
)
