"""ShizuCallRecorder: records calls through Shizuku.

Its settings are toggles with no export, so the walk flips them and reads each
one back. `ensure` taps only a row that disagrees, so running it again changes
nothing.
"""

from uiu.dsl import app, ensure, launch, scroll, wait

APP = app(
    package="com.kitsumed.shizucallrecorder",
    permissions={
        "android.permission.READ_CALL_LOG": ("granted", "identify incoming and outgoing calls"),
        "android.permission.READ_PHONE_STATE": ("granted", "follow call state changes"),
        "android.permission.READ_CONTACTS": ("granted", "name recordings after the contact"),
        "android.permission.POST_NOTIFICATIONS": ("granted", "show that a recording is running"),
    },
    appops={
        "MANAGE_ONGOING_CALLS": ("allow", "record the call in progress"),
    },
    doze=("allow", "the recorder has to run while the phone sleeps"),
    # The toggles sit below the About block, which is taller than one screen.
    setup=[
        launch("com.kitsumed.shizucallrecorder/.MainActivity", clear=True),
        scroll("down"),
        wait(text="Automatically record incoming calls"),
        ensure(True, text="Automatically record incoming calls"),
        scroll("down"),
        wait(text="Automatically record outgoing calls"),
        ensure(True, text="Automatically record outgoing calls"),
    ],
)
