"""mpvEx: the video player."""

from uiu.dsl import app

APP = app(
    package="app.marlboroadvance.mpvex",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "playback notices"),
        "android.permission.READ_MEDIA_VIDEO": ("granted", "play local video"),
    },
    appops={
        "MANAGE_EXTERNAL_STORAGE": ("allow", "play from any folder"),
    },
)
