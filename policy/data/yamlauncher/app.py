"""Yam Launcher: the home screen, and the role that makes it one."""

from uiu.dsl import app, key, pull, push, scroll, sleep, tap, type_text, wait

APP = app(
    package="eu.ottop.yamlauncher",
    roles={
        "android.app.role.HOME": "Yam Launcher is the home screen",
    },
    artifact="yamlauncher.json",
    backup=[
        key("KEYCODE_HOME"),
        wait(id="eu.ottop.yamlauncher:id/textClock"),
        tap(id="eu.ottop.yamlauncher:id/textClock", duration=800),
        wait(text="Set Default Home"),
        scroll("down", times=2),
        wait(text="Backup"),
        tap(text="Backup"),
        wait(desc="Show roots"),
        key("KEYCODE_ESCAPE"),
        wait(id="android:id/title", class_name="android.widget.EditText"),
        type_text("{{artifact}}", id="android:id/title", class_name="android.widget.EditText"),
        key("KEYCODE_ESCAPE"),
        wait(id="android:id/title", text_contains="{{artifact}}"),
        tap(id="android:id/button1"),
        pull("/sdcard/Download/{{artifact}}", "{{artifact}}"),
    ],
    restore=[
        key("KEYCODE_HOME"),
        wait(id="eu.ottop.yamlauncher:id/textClock"),
        tap(id="eu.ottop.yamlauncher:id/textClock", duration=800),
        wait(text="Set Default Home"),
        scroll("down", times=2),
        wait(text="Restore"),
        push("{{artifact}}", "/sdcard/Download/{{artifact}}"),
        tap(text="Restore"),
        wait(id="com.google.android.documentsui:id/option_menu_search"),
        tap(id="com.google.android.documentsui:id/option_menu_search"),
        wait(id="com.google.android.documentsui:id/search_src_text"),
        type_text("{{artifact}}", id="com.google.android.documentsui:id/search_src_text"),
        key("KEYCODE_ESCAPE"),
        wait(id="android:id/title", text_contains="{{artifact}}"),
        tap(id="android:id/title", text_contains="{{artifact}}"),
        sleep(3),
    ],
    volatile=["created_at"],
    private=["data.location.value", "data.locationRegion.value"],
)
