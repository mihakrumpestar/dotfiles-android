"""OsmAnd: offline maps, restored from one settings export.

The export carries the profiles, the favorites, the search and navigation
history and the voice resources, so a fresh phone gets the same OsmAnd back in
one import. It is stored in the private tree because the history and the
favorites are the user's own data.

The import dialog selects a whole section when its title is tapped, so the walk
needs one tap per section. The file picker's roots drawer has no label the dump
can reach, so the drawer and Downloads are tapped by point on a 1080x2340
screen.
"""

from uiu.dsl import app, launch, push, scroll, shell, sleep, tap

ROOTS = "input tap 79 168"  # the picker's roots icon, top left
DOWNLOADS = "input tap 300 975"  # the Downloads row of the roots drawer

APP = app(
    package="net.osmand.plus",
    artifact="osmand.osf",
    permissions={
        "android.permission.ACCESS_FINE_LOCATION": ("granted", "navigation"),
        "android.permission.ACCESS_COARSE_LOCATION": ("granted", "approximate position"),
        "android.permission.POST_NOTIFICATIONS": ("granted", "navigation notices"),
    },
    appops={
        "MANAGE_EXTERNAL_STORAGE": ("allow", "read maps from any folder"),
    },
    restore=[
        push("{{artifact}}", "/sdcard/Download/{{artifact}}"),
        # A push writes the file but no MediaStore row, and the picker lists
        # folders from the store, so the volume is rescanned first.
        shell("content call --uri content://media --method scan_volume --arg external_primary"),
        launch("net.osmand.plus/.activities.MapActivity", clear=True),
        sleep(4),
        # The first run on a new phone offers the local map; the import below
        # does not need it.
        tap(text="Close", optional=True),
        tap(desc="Back to menu"),
        sleep(1),
        # Settings sits below the menu's fold and below the settings list's fold.
        scroll("down", times=3),
        tap(text="Settings"),
        sleep(1),
        scroll("down", times=3),
        tap(text="Import/export"),
        tap(text="Import from file"),
        sleep(2),
        shell(ROOTS),
        sleep(1),
        shell(DOWNLOADS),
        sleep(2),
        tap(text="{{artifact}}"),
        sleep(3),
        # One tap on a section title selects everything under it.
        tap(text="Settings"),
        tap(text="My Places"),
        tap(text="Resources"),
        tap(text="Continue"),
        sleep(3),
        # The conflict screen appears when the phone already holds profiles of
        # the same name; the export is the version that should win.
        tap(text="Replace all", optional=True),
        sleep(5),
        tap(text="Restart", optional=True),
        sleep(6),
    ],
)
