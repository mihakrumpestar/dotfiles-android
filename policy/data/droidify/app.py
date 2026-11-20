"""Droid-ify: the F-Droid client."""

from uiu.dsl import app

APP = app(
    package="com.looker.droidify",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "update notices"),
    },
    appops={
        "REQUEST_INSTALL_PACKAGES": ("allow", "install the apps it downloads"),
    },
)
