"""Super Productivity: the task tracker.

Its reminders are scheduled with exact alarms, so the app op has to be allowed
or Android batches them and a reminder can arrive late.
"""

from uiu.dsl import app

APP = app(
    package="com.superproductivity.superproductivity",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "reminder notices"),
    },
    appops={
        "SCHEDULE_EXACT_ALARM": ("allow", "reminders fire at the set time"),
    },
)
