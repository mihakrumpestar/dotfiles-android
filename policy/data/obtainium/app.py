"""Obtainium: the installer, and the app list it exports."""

from uiu.dsl import app, if_absent, key, launch, pull, push, scroll, shell, sleep, tap, wait

APP = app(
    package="dev.imranr.obtainium",
    install={
        "assets": {
            "arm64-v8a": "app-arm64-v8a-release.apk",
            "armeabi-v7a": "app-armeabi-v7a-release.apk",
            "universal": "app-release.apk",
            "x86_64": "app-x86_64-release.apk",
        },
        "checksum": "asset",
        "hint": "open Obtainium and import your export (Settings > Import/Export)",
        "release": "https://api.github.com/repos/ImranR98/Obtainium/releases/latest",
    },
    artifact="obtainium.json",
    backup=[
        key("KEYCODE_HOME"),
        launch("dev.imranr.obtainium/.MainActivity", clear=True),
        wait(desc="Settings"),
        tap(desc="Settings"),
        wait(desc="Import/export", class_name="android.widget.Button"),
        tap(desc="Import/export", class_name="android.widget.Button"),
        wait(desc="Obtainium export", class_name="android.widget.Button"),
        # The selector list decides which entries stay out of the public
        # artifact; the export must include them so the private half is whole.
        if_absent(
            text="All",
            steps=[
                tap(text="Exclude secrets"),
                sleep(1),
                tap(desc="All"),
            ],
        ),
        tap(desc="Obtainium export", class_name="android.widget.Button"),
        # With the export set to "All", Obtainium warns that the file holds
        # secrets before writing it; with any other setting there is no dialog.
        sleep(2),
        tap(desc="Continue", optional=True),
        pull(
            "/sdcard/phone-sync/configs/obtainium-export-*.json",
            "{{artifact}}",
            newest=True,
        ),
    ],
    restore=[
        key("KEYCODE_HOME"),
        push("{{artifact}}", "/sdcard/Download/{{artifact}}"),
        # A push writes the file but no MediaStore row, and the picker lists
        # folders from the store, so the volume is rescanned first.
        shell("content call --uri content://media --method scan_volume --arg external_primary"),
        launch("dev.imranr.obtainium/.MainActivity", clear=True),
        wait(desc="Settings", timeout=30),
        tap(desc="Settings"),
        wait(desc="Import/export", class_name="android.widget.Button"),
        tap(desc="Import/export", class_name="android.widget.Button"),
        wait(desc="Obtainium import", class_name="android.widget.Button"),
        tap(desc="Obtainium import", class_name="android.widget.Button"),
        # The picker opens at the last used folder and keeps a search box open
        # between runs, so the walk goes through the roots list to Download.
        sleep(2),
        tap(desc="Show roots"),
        sleep(1),
        if_absent(text="Downloads", steps=[tap(desc="Show roots"), sleep(1)]),
        wait(text="Downloads"),
        tap(text="Downloads"),
        sleep(1),
        if_absent(
            text_contains="{{artifact}}",
            steps=[scroll("down", times=8, until={"text_contains": "{{artifact}}"})],
        ),
        tap(id="android:id/title", text_contains="{{artifact}}"),
        wait(desc="Continue", timeout=20),
        tap(desc="Continue"),
        sleep(3),
    ],
    omit=[
        "appVersion",
        "exportedAt",
        "settings.lastCompletedBGCheckTime",
        "settings.updateHealthFilteredReleasesCount",
        "apps.lastUpdateCheck",
        "apps.latestVersionChangedAt",
        "apps.installedVersion",
        "apps.releaseDate",
        "apps.releaseUrl",
        "apps.otherAssetUrls",
        "apps.apkSizeBytes",
        "apps.changeLog",
        "apps.consecutiveFailedChecks",
        "apps.newerReleasesFilteredOut",
        "apps.updateHealthDismissedVersion",
        "apps.isArchived",
        "apps.pendingRepoRenameUrl",
    ],
    private="private.json",
    sort=["apps"],
)
