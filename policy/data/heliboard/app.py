"""HeliBoard: the keyboard.

Its backup is one zip carrying the settings, the extra layouts, both learned-word
dictionaries and the clipboard history, so the file lives in the private tree and
this walk only restores it. A fresh install shows its setup wizard until HeliBoard
is the enabled and current input method, so the walk sets that first.
"""

from uiu.dsl import app, launch, push, scroll, shell, sleep, tap, wait

APP = app(
    package="helium314.keyboard",
    permissions={
        "android.permission.READ_CONTACTS": ("granted", "suggest contact names"),
    },
    artifact="HeliBoard_backup.zip",
    restore=[
        # The IME has to be enabled and current before the app stops showing its
        # setup wizard; `settings put` alone is ignored on an enabled-list miss.
        shell("ime enable helium314.keyboard/.latin.LatinIME"),
        shell("ime set helium314.keyboard/.latin.LatinIME"),
        push("{{artifact}}", "/sdcard/Download/{{artifact}}"),
        # A push writes the file but no MediaStore row, and the picker lists
        # folders from the store, so the volume is rescanned first.
        shell("content call --uri content://media --method scan_volume --arg external_primary"),
        launch("helium314.keyboard/.settings.SettingsActivity"),
        # The setup wizard shows until the IME is current, and it ends on its own
        # once that is true, so its last page is dismissed when it appears.
        tap(text="Finished", optional=True),
        wait(text="Advanced"),
        tap(text="Advanced"),
        scroll("down", times=2, until={"text": "Backup and restore"}),
        tap(text="Backup and restore"),
        wait(text="Restore"),
        tap(text="Restore"),
        # The picker opens at the last used folder, so it goes to Download first.
        wait(desc="Show roots"),
        tap(desc="Show roots"),
        wait(text="Downloads"),
        tap(text="Downloads"),
        scroll("down", times=4, until={"text_contains": "HeliBoard_backup.zip"}),
        tap(id="android:id/title", text_contains="HeliBoard_backup.zip"),
        # Picking the file restores at once and returns to the Advanced screen.
        wait(text="Backup and restore"),
        sleep(2),
    ],
)
