"""Samsung Keyboard: the stock IME, which still reads the clipboard.

Samsung protects the package from `pm disable-user` and re-adds its IME to
`secure/enabled_input_methods`, so the keyboard cannot be disabled. It still
reads the clipboard from the background, so both clipboard app ops are denied.
"""

from uiu.dsl import app

APP = app(
    package="com.samsung.android.honeyboard",
    appops={
        "READ_CLIPBOARD": ("deny", "the stock keyboard reads the clipboard"),
        "WRITE_CLIPBOARD": ("deny", "the stock keyboard writes the clipboard"),
    },
)
