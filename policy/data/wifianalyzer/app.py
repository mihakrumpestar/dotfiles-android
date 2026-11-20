"""WiFiAnalyzer."""

from uiu.dsl import app

APP = app(
    package="com.vrem.wifianalyzer",
    permissions={
        "android.permission.NEARBY_WIFI_DEVICES": ("granted", "scan nearby networks"),
        "android.permission.ACCESS_FINE_LOCATION": ("granted", "network details"),
        "android.permission.ACCESS_COARSE_LOCATION": ("granted", "approximate location"),
    },
)
