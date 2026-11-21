"""Clock: the 05:40 alarm on Monday to Friday.

The platform SET_ALARM intent creates it when absent; the switch is then
checked, so a rerun is idempotent.
"""

from uiu.dsl import app, ensure, if_absent, launch, shell, sleep, tap, wait

ALARM = "05:40 , Every Monday, Every Tuesday, Every Wednesday, Every Thursday, Every Friday"

APP = app(
    package="com.sec.android.app.clockpackage",
    setup=[
        launch("com.sec.android.app.clockpackage/.ClockPackage", clear=True),
        tap(desc="Alarm", optional=True),
        wait(desc="Add alarm"),
        if_absent(
            desc=ALARM,
            class_name="android.widget.Switch",
            steps=[
                shell(
                    "am start -a android.intent.action.SET_ALARM "
                    "--ei android.intent.extra.alarm.HOUR 5 "
                    "--ei android.intent.extra.alarm.MINUTES 40 "
                    "--eial android.intent.extra.alarm.DAYS 2,3,4,5,6 "
                    "--ez android.intent.extra.alarm.SKIP_UI true"
                ),
                sleep(1),
            ],
        ),
        ensure(checked=True, desc=ALARM, class_name="android.widget.Switch"),
    ],
)
