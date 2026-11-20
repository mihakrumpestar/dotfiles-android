"""IronFox: the Firefox fork, and the default browser.

Two settings have no export: the default search engine, which ships without
Brave, and the switch that allows installing an add-on at all. Both are set here.
The add-on pages themselves are GeckoView and expose no controls to the engine,
so installing an add-on stays manual:

- I don't care about cookies
"""

from uiu.dsl import app, ensure, if_absent, key, launch, scroll, shell, tap, type_text, wait

APP = app(
    package="org.ironfoxoss.ironfox",
    permissions={
        "android.permission.POST_NOTIFICATIONS": ("granted", "download and update notices"),
    },
    roles={
        "android.app.role.BROWSER": "IronFox is the browser",
    },
    setup=[
        launch("org.ironfoxoss.ironfox/.App", clear=True),
        # A newer build offers itself on start, and the dialog covers the page.
        tap(text="Later", optional=True),
        wait(desc="More options"),
        tap(desc="More options"),
        wait(desc="Settings"),
        tap(desc="Settings"),
        wait(text="Search", id="android:id/title"),
        tap(text="Search", id="android:id/title"),
        wait(text="Default search engine", id="android:id/title"),
        tap(text="Default search engine", id="android:id/title"),
        # The engine list opens at the top; "Add search engine" is its last row.
        scroll("down", times=4, until={"text": "Add search engine"}),
        # Adding an engine that is already there leaves a second identical row.
        if_absent(
            text="Brave",
            steps=[
                tap(text="Add search engine"),
                wait(id="org.ironfoxoss.ironfox:id/edit_engine_name"),
                type_text("Brave", id="org.ironfoxoss.ironfox:id/edit_engine_name"),
                # `input text` reads %s as a space, so the placeholder goes in
                # as two pieces after the URL.
                type_text(
                    "https://search.brave.com/search?q=",
                    id="org.ironfoxoss.ironfox:id/edit_search_string",
                ),
                shell("input text '%'"),
                shell("input text s"),
                # The keyboard covers the Save button, which only enters the
                # tree once the keyboard is gone.
                key("KEYCODE_BACK"),
                tap(text="Save"),
                wait(text="Default search engine"),
            ],
        ),
        scroll("down", times=4, until={"text": "Brave"}),
        ensure(True, text="Brave"),
        tap(desc="Navigate up"),
        tap(desc="Navigate up"),
        # IronFox's own page is the last section of Settings.
        scroll("down", times=8, until={"text": "IronFox settings"}),
        tap(text="IronFox settings", id="android:id/title"),
        # Security sits near the bottom of that page.
        scroll("down", times=10, until={"text": "Allow installation of add-ons"}),
        ensure(True, text="Allow installation of add-ons"),
    ],
)
