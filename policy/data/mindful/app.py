"""Mindful: screen time limits and a bedtime."""

from uiu.dsl import app

APP = app(
    package="com.mindful.android",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "bedtime and limit notices"),
    },
    appops={
        "GET_USAGE_STATS": ("allow", "measure screen time"),
        "SYSTEM_ALERT_WINDOW": ("allow", "draw the pause screen"),
        "ACTIVATE_VPN": ("allow", "block apps through its local VPN"),
        "SCHEDULE_EXACT_ALARM": ("allow", "start a bedtime at the set time"),
    },
)
