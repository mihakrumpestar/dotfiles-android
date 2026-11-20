"""KDE Connect: the bridge to the desktop."""

from uiu.dsl import app

APP = app(
    package="org.kde.kdeconnect_tp",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "mirror phone notifications"),
        "android.permission.READ_CONTACTS": ("granted", "send a contact"),
        "android.permission.READ_CALL_LOG": ("granted", "show a missed call"),
        "android.permission.READ_PHONE_STATE": ("granted", "pause on a call"),
        "android.permission.READ_SMS": ("granted", "read and send messages from the desktop"),
        "android.permission.SEND_SMS": ("granted", "reply to a message from the desktop"),
        "android.permission.RECEIVE_SMS": ("granted", "receive messages while connected"),
        "android.permission.READ_EXTERNAL_STORAGE": ("granted", "browse files"),
        "android.permission.WRITE_EXTERNAL_STORAGE": ("granted", "receive files"),
    },
)
