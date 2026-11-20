"""Keep Screen On: a quick settings tile that holds the screen awake."""

from uiu.dsl import app, shell

APP = app(
    package="com.elasticrock.keepscreenon",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "show the tile state"),
    },
    appops={
        "WRITE_SETTINGS": ("allow", "hold the screen timeout while running"),
    },
    # Adding a tile that is already there does nothing, so the walk is safe to
    # repeat.
    setup=[
        shell("cmd statusbar add-tile com.elasticrock.keepscreenon/.service.QSTileService"),
    ],
)
