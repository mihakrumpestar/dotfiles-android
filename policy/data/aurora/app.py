"""Aurora Store: an app store that can download without a Google account."""

from uiu.dsl import app, ensure, launch, tap, wait

APP = app(
    package="com.aurora.store",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "update notices"),
        "android.permission.READ_EXTERNAL_STORAGE": ("granted", "import an APK from storage"),
        "android.permission.WRITE_EXTERNAL_STORAGE": ("granted", "save an APK to storage"),
    },
    appops={
        "REQUEST_INSTALL_PACKAGES": ("allow", "install the apps it downloads"),
        "MANAGE_EXTERNAL_STORAGE": ("allow", "keep its download folder"),
    },
    # "Do not auto-update apps" is the only option that stops the store from
    # checking and installing on its own; the two below it check or install.
    setup=[
        launch("com.aurora.store/.ComposeActivity", clear=True),
        wait(desc="More"),
        tap(desc="More"),
        wait(text="Settings"),
        tap(text="Settings"),
        wait(text="Updates"),
        tap(text="Updates"),
        wait(text="Automatic updates"),
        tap(text="Automatic updates"),
        wait(text="Do not auto-update apps"),
        ensure(True, text="Do not auto-update apps"),
    ],
)
