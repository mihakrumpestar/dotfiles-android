"""Do Not Disturb: the default Sleeping rule, 22:00 to 06:00 every day.

Android owns the rule, so Settings can enable it but not edit its times. The
walk ensures the rule is on; it allows alarms and silences calls and messages.
"""

from uiu.dsl import app, ensure, shell, wait

APP = app(
    package="com.android.settings",
    setup=[
        shell("am start -a android.settings.ZEN_MODE_SETTINGS"),
        wait(text="Do not disturb"),
        ensure(checked=True, desc="Sleeping", class_name="android.widget.Switch"),
    ],
)
