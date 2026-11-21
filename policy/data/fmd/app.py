"""FMD: Find My Device, the remote locate and control app.

It takes commands from SMS and from FMD Server while the phone sleeps, so it
needs the location, SMS and notification permissions, and the battery
optimization exemption that stops Android from suspending it.
"""

from uiu.dsl import app

APP = app(
    package="de.nulide.findmydevice",
    permissions={
        "android.permission.ACCESS_FINE_LOCATION": ("granted", "locate the phone"),
        "android.permission.ACCESS_COARSE_LOCATION": ("granted", "approximate position"),
        "android.permission.ACCESS_BACKGROUND_LOCATION": (
            "granted",
            "locate it while the screen is off",
        ),
        "android.permission.READ_SMS": ("granted", "read the SMS command thread"),
        "android.permission.RECEIVE_SMS": ("granted", "take commands over SMS"),
        "android.permission.SEND_SMS": ("granted", "answer an SMS command"),
        "android.permission.READ_PHONE_STATE": ("granted", "phone state in its status report"),
        "android.permission.CAMERA": ("granted", "the take-picture command"),
        "android.permission.POST_NOTIFICATIONS": ("granted", "notices while it runs"),
        "android.permission.BLUETOOTH_CONNECT": ("granted", "toggle Bluetooth"),
        "android.permission.BLUETOOTH_SCAN": ("granted", "list nearby Bluetooth devices"),
    },
    doze=("allow", "it must answer while the phone sleeps"),
)
