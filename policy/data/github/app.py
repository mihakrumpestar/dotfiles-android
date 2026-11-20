"""GitHub."""

from uiu.dsl import app

APP = app(
    package="com.github.android",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "review and CI notices"),
    },
)
