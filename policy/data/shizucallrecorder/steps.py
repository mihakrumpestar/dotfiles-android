"""ShizuCallRecorder: capture its settings screens, since it has no export."""

from uiu.dsl import capture, launch, recipe, scroll, sleep

RECIPE = recipe(
    package="com.kitsumed.shizucallrecorder",
    backup=[
        launch("com.kitsumed.shizucallrecorder/.MainActivity"),
        sleep(2),
        capture("main"),
        scroll("down"),
        capture("main-scrolled"),
    ],
)
