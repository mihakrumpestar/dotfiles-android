"""MicroG RE: the Play Services replacement the YouTube build needs."""

from uiu.dsl import app

APP = app(
    package="app.revanced.android.gms",
    permissions={
        "android.permission.ACCESS_FINE_LOCATION": ("granted", "apps that ask MicroG for location"),
        "android.permission.ACCESS_COARSE_LOCATION": ("granted", "coarse location"),
        "android.permission.ACCESS_BACKGROUND_LOCATION": (
            "granted",
            "location while the app is in the background",
        ),
        "android.permission.READ_CONTACTS": ("granted", "apps that read contacts through MicroG"),
        "android.permission.GET_ACCOUNTS": ("granted", "see the account MicroG registers"),
        "android.permission.READ_PHONE_STATE": ("granted", "device state for Google apps"),
        "android.permission.POST_NOTIFICATIONS": ("granted", "MicroG notices"),
    },
    doze=("allow", "MicroG keeps the YouTube replacement signed in"),
)
