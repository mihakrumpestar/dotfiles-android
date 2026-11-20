"""YouTube: the Morphe build, which drops the ads.

Its settings are one JSON, so the walk imports the file committed beside this one
and accepts the restart the app offers, which is what applies it.
"""

from uiu.dsl import app, if_absent, launch, push, scroll, shell, sleep, tap, wait

APP = app(
    package="app.morphe.android.youtube",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "update and download notices"),
        "android.permission.GET_ACCOUNTS": ("granted", "add the Google account to MicroG"),
    },
    setup=[
        push("youtube-revanced.json", "/sdcard/Download/youtube-revanced.json"),
        # A push writes the file but no MediaStore row, and the picker lists
        # the folder from the store, so the volume is rescanned first.
        shell("content call --uri content://media --method scan_volume --arg external_primary"),
        launch("app.morphe.android.youtube/.morphe_original_5", clear=True),
        wait(desc="You"),
        # The bottom bar exists before the feed has settled, and a tap in that
        # window is swallowed.
        sleep(2),
        tap(desc="You"),
        wait(desc="Settings", timeout=20),
        tap(desc="Settings"),
        wait(text="Settings", id="android:id/title"),
        tap(text="Settings", id="android:id/title"),
        wait(id="android:id/title", text_contains="Miscellaneous"),
        tap(id="android:id/title", text_contains="Miscellaneous"),
        # Import / Export sits two screens down the alphabetical list.
        scroll("down", times=2),
        wait(text="Import / Export", id="android:id/title"),
        tap(text="Import / Export", id="android:id/title"),
        wait(text="Import from file"),
        tap(text="Import from file"),
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
            text_contains="youtube-revanced.json",
            steps=[scroll("down", times=8, until={"text_contains": "youtube-revanced.json"})],
        ),
        tap(id="android:id/title", text_contains="youtube-revanced.json"),
        # The box only fills once the file has been read, and Save is what
        # writes the preferences.
        wait(text="Save"),
        tap(text="Save"),
        # Saving raises "Restart required" only when something actually changed,
        # so the restart is dismissed when it is there and skipped otherwise.
        sleep(3),
        tap(text="Restart", optional=True),
        sleep(6),
    ],
)
