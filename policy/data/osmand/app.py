"""OsmAnd: offline maps."""

from uiu.dsl import app

APP = app(
    package="net.osmand.plus",
    permissions={
        "android.permission.ACCESS_FINE_LOCATION": ("granted", "navigation"),
        "android.permission.ACCESS_COARSE_LOCATION": ("granted", "approximate position"),
        "android.permission.POST_NOTIFICATIONS": ("granted", "navigation notices"),
    },
    appops={
        "MANAGE_EXTERNAL_STORAGE": ("allow", "read maps from any folder"),
    },
)
