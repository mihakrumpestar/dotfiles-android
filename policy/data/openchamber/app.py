"""OpenChamber."""

from uiu.dsl import app

APP = app(
    package="com.openchamber.app",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "model and run notices"),
        "android.permission.CAMERA": ("granted", "attach a photo"),
        "android.permission.RECORD_AUDIO": ("granted", "dictate a prompt"),
    },
)
