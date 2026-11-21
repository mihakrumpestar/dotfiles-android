"""Obsidian: the Nextcloud sync plugin, and nothing else.

The vault is filled by the sync: notes, attachments and plugin settings alike.
Only the plugin that talks to Nextcloud has to exist first, so this walk is the
single thing a fresh phone needs before a sync can restore everything.

The vault must already be open, created with the same name as on the desktop,
because that name is the remote folder. Obsidian keeps its settings view across
a restart, so the walk backs out of whatever page the last run left open first.
Its drawer toggle and settings gear are drawn inside a webview without a label
the tree can match, so the gear is tapped by point on this 1080x2340 screen.
"""

from uiu.dsl import app, if_absent, launch, shell, sleep, tap, type_text, wait

GEAR = "input tap 800 2230"  # bottom right of the drawer: the settings gear
SEARCH = "input tap 470 365"  # the plugin browser's search field
BACK = "input keyevent KEYCODE_BACK"
BUTTON = "android.widget.Button"

APP = app(
    package="md.obsidian",
    setup=[
        launch("md.obsidian/md.obsidian.MainActivity", clear=True),
        wait(desc="Expand"),
        # The settings page survives a restart; the vault tolerates extra backs.
        shell(BACK),
        shell(BACK),
        shell(BACK),
        shell(BACK),
        # Those backs may also have left the app, so it is started again.
        launch("md.obsidian/md.obsidian.MainActivity"),
        wait(desc="Expand"),
        tap(desc="Expand"),
        shell(GEAR),
        wait(text="Community plugins"),
        tap(text="Community plugins"),
        # A vault that has never had plugins is in Restricted mode, and leaving
        # it reloads the app, which drops the visitor back on the vault.
        tap(text="Exit Restricted mode", optional=True),
        sleep(4),
        tap(text="Trust author and enable plugins", class_name=BUTTON, optional=True),
        if_absent(
            text="Browse",
            steps=[
                tap(desc="Expand"),
                shell(GEAR),
                wait(text="Community plugins"),
                tap(text="Community plugins"),
            ],
        ),
        tap(text="Browse"),
        wait(text="Show installed only"),
        shell(SEARCH),
        type_text("nextcloud"),
        sleep(3),
        # A missing plugin says Install; an installed one says INSTALLED, and the
        # list row behind the browser carries its version, so it cannot collide.
        tap(text="Nextcloud Sync", optional=True),
        tap(text_contains="Nextcloud SyncINSTALLED", optional=True),
        sleep(2),
        tap(text="Install", class_name=BUTTON, optional=True),
        sleep(4),
        tap(text="Enable", class_name=BUTTON, optional=True),
        tap(text="Trust author and enable plugins", class_name=BUTTON, optional=True),
    ],
)
