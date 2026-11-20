"""Round Sync: the rclone client that copies backups to the server.

The walk replays the app's own config export, which holds the remotes, the sync
tasks and their triggers as one zip. That zip carries the WebDAV password in
`rclone.conf`, so the export lives in the private tree, not in this repository:
`task ui-backup` writes `secrets/roundsync/roundsync.zip`, and `task ui-restore`
puts it back.
"""

from uiu.dsl import app, clear, key, launch, pull, push, shell, sleep, tap, type_text, wait

APP = app(
    package="de.felixnuesse.extract",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "sync notices"),
        "android.permission.READ_EXTERNAL_STORAGE": ("granted", "read folders to copy"),
        "android.permission.WRITE_EXTERNAL_STORAGE": ("granted", "write restored files"),
    },
    appops={
        "MANAGE_EXTERNAL_STORAGE": ("allow", "copy any folder"),
        "SCHEDULE_EXACT_ALARM": ("allow", "run the scheduled sync"),
    },
    doze=("allow", "Round Sync copies backups on a schedule"),
    artifact="roundsync.zip",
    # The export writes the sync tasks and their triggers, and it stamps every
    # archive member with the moment of the export.
    backup=[
        launch("de.felixnuesse.extract/ca.pkay.rcloneexplorer.Activities.MainActivity", clear=True),
        wait(desc="Navigate up"),
        tap(desc="Navigate up"),
        # The drawer slides in, so its rows move after the dump that matched them.
        sleep(1),
        wait(text="Settings"),
        tap(text="Settings"),
        wait(text="Export config"),
        tap(text="Export config"),
        # The save dialog opens where the last export went, which is not stable.
        # DocumentsUI's roots list names Download, so go there before naming the
        # file, or the pull reads a path nothing wrote.
        wait(desc="Show roots"),
        tap(desc="Show roots"),
        wait(text="Downloads"),
        tap(text="Downloads"),
        wait(id="android:id/title", class_name="android.widget.EditText"),
        clear(id="android:id/title", class_name="android.widget.EditText"),
        type_text("{{artifact}}", id="android:id/title", class_name="android.widget.EditText"),
        wait(text="SAVE"),
        tap(text="SAVE"),
        pull("/sdcard/Download/{{artifact}}", "{{artifact}}"),
    ],
    restore=[
        push("{{artifact}}", "/sdcard/Download/{{artifact}}"),
        # A push writes the file but no MediaStore row, and the picker lists
        # folders from the store, so the volume is rescanned first.
        shell("content call --uri content://media --method scan_volume --arg external_primary"),
        launch("de.felixnuesse.extract/ca.pkay.rcloneexplorer.Activities.MainActivity", clear=True),
        wait(desc="Navigate up"),
        tap(desc="Navigate up"),
        sleep(1),
        wait(text="Settings"),
        tap(text="Settings"),
        wait(text="Import config"),
        tap(text="Import config"),
        wait(text="Continue"),
        tap(text="Continue"),
        wait(id="com.google.android.documentsui:id/option_menu_search"),
        tap(id="com.google.android.documentsui:id/option_menu_search"),
        wait(id="com.google.android.documentsui:id/search_src_text"),
        type_text("{{artifact}}", id="com.google.android.documentsui:id/search_src_text"),
        key("KEYCODE_ESCAPE"),
        wait(id="android:id/title", text_contains="{{artifact}}"),
        tap(id="android:id/title", text_contains="{{artifact}}"),
        sleep(6),
    ],
)
