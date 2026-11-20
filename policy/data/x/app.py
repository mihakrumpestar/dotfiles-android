"""X: the Piko build, which drops the ads and the tracking.

Its settings are one JSON that the app's own importer reads. The file has to stay
on one line, because the importer parses only the first line it reads.
"""

from uiu.dsl import app, if_absent, push, scroll, shell, sleep, tap, wait

APP = app(
    package="com.twitter.android",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "mention and message notices"),
    },
    # Piko opens its own settings from a deep link, which is a shorter way in
    # than the navigation drawer.
    setup=[
        push("x-piko.json", "/sdcard/Download/x-piko.json"),
        # A push writes the file but no MediaStore row, and the picker lists
        # the folder from the store, so the volume is rescanned first.
        shell("content call --uri content://media --method scan_volume --arg external_primary"),
        shell("am start -a android.intent.action.VIEW -d https://x.com/i/piko com.twitter.android"),
        wait(text="Backup and restore", id="android:id/title"),
        tap(text="Backup and restore", id="android:id/title"),
        wait(text="Import Piko settings", id="android:id/title"),
        tap(text="Import Piko settings", id="android:id/title"),
        # The picker opens at the last used folder and keeps a search box open
        # between runs, so the walk goes through the roots list to Download. Its
        # toolbar slides while the picker appears, so the tap waits for that.
        sleep(2),
        # The roots button toggles a drawer that may already be open, so a tap
        # that left Download hidden is followed by one more.
        tap(desc="Show roots"),
        sleep(1),
        if_absent(text="Downloads", steps=[tap(desc="Show roots"), sleep(1)]),
        wait(text="Downloads"),
        tap(text="Downloads"),
        sleep(1),
        # The list opens on whatever the picker last sorted by, so the file can be
        # on screen without scrolling.
        if_absent(
            text_contains="x-piko.json",
            steps=[scroll("down", times=8, until={"text_contains": "x-piko.json"})],
        ),
        tap(id="android:id/title", text_contains="x-piko.json"),
        # The import ends with a restart prompt.
        wait(text="OK", id="android:id/button1"),
        tap(text="OK", id="android:id/button1"),
        sleep(6),
    ],
)
