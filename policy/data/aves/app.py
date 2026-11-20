"""Aves: the gallery, which reads the media collection."""

from uiu.dsl import app

APP = app(
    package="deckers.thibault.aves.libre",
    permissions={
        "android.permission.READ_MEDIA_IMAGES": ("granted", "show photos in the gallery"),
        "android.permission.READ_MEDIA_VIDEO": ("granted", "show videos in the gallery"),
    },
)
