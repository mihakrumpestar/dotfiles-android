"""OSS Document Scanner: scans paper into PDFs."""

from uiu.dsl import app

APP = app(
    package="com.akylas.documentscanner",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "scan finished notice"),
        "android.permission.CAMERA": ("granted", "scan a page"),
    },
)
