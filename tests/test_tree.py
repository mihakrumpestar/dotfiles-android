"""The accessibility tree: selectors, tap targets, and the probe table."""

from __future__ import annotations

import pytest
from support import xml_node, xml_screen

from uiu.tree import (
    NoMatch,
    Rect,
    Selector,
    TreeError,
    hit_target,
    parse_dump,
    probe_rows,
    render_probe,
    resolve,
    unlabelled_clickable,
)


def screen(*nodes: str) -> list:
    return parse_dump(xml_screen(*nodes))


def test_nodes_keep_document_order():
    nodes = screen(
        xml_node(text="Settings", bounds="[0,0,100,50]"),
        xml_node(desc="Import/export", cls="android.widget.Button", bounds="[0,50,100,100]"),
        xml_node(id="android:id/title", text="demo.json", cls="android.widget.EditText"),
    )
    assert [node.index for node in nodes] == [0, 1, 2]
    assert nodes[1].class_name == "android.widget.Button"


def test_selector_matches_every_field_it_sets():
    nodes = screen(
        xml_node(
            text="Import/export", desc="Export", cls="android.widget.Button", id="app:id/button"
        )
    )
    assert resolve(nodes, Selector(desc="Export")).index == 0
    assert resolve(nodes, Selector(text="Import/export")).index == 0
    assert (
        resolve(nodes, Selector(text_contains="Import", class_name="android.widget.Button")).index
        == 0
    )
    assert resolve(nodes, Selector(resource_id="app:id/button")).index == 0
    with pytest.raises(NoMatch):
        resolve(nodes, Selector(text="Import/export", desc="Import"))


def test_selector_index_picks_the_nth_match():
    nodes = screen(xml_node(text="Delete"), xml_node(text="Delete"))
    assert resolve(nodes, Selector(text="Delete", index=0)).index == 0
    assert resolve(nodes, Selector(text="Delete", index=1)).index == 1
    with pytest.raises(NoMatch, match=r"2 match"):
        resolve(nodes, Selector(text="Delete", index=2))


def test_empty_selector_and_empty_dump_are_errors():
    nodes = screen(xml_node(text="x"))
    with pytest.raises(NoMatch, match="empty selector"):
        resolve(nodes, Selector())
    with pytest.raises(TreeError, match="no nodes"):
        parse_dump("<hierarchy rotation='0' />")


def test_malformed_bounds_are_empty_not_fatal():
    assert Rect(0, 0, 0, 0).empty
    nodes = screen(xml_node(bounds="nonsense"))
    assert nodes[0].bounds.empty


def test_label_inside_a_switch_taps_the_switch():
    nodes = screen(
        xml_node(text="Wi-Fi", bounds="[380,10,500,90]"),
        xml_node(
            cls="android.widget.Switch", checkable=True, checked=True, bounds="[380,10,500,90]"
        ),
    )
    label = resolve(nodes, Selector(text="Wi-Fi"))
    assert hit_target(nodes, label).checkable


def test_sibling_switch_to_the_right_taps_the_switch():
    nodes = screen(
        xml_node(text="Lock Settings", bounds="[20,20,400,80]"),
        xml_node(cls="android.widget.Switch", checkable=True, bounds="[900,10,980,90]"),
    )
    label = resolve(nodes, Selector(text="Lock Settings"))
    assert hit_target(nodes, label).bounds == Rect(900, 10, 980, 90)


def test_plain_row_taps_the_clickable_container():
    nodes = screen(
        xml_node(text="Backup", bounds="[0,0,1000,100]", clickable=True),
        xml_node(text="Backup", bounds="[20,20,300,80]"),
    )
    label = resolve(nodes, Selector(text="Backup", index=1))
    assert hit_target(nodes, label).clickable


def test_unlabelled_node_is_its_own_target():
    nodes = screen(xml_node(cls="android.widget.ImageView", clickable=True, bounds="[0,0,50,50]"))
    node = nodes[0]
    assert hit_target(nodes, node) is node


def test_probe_prints_paste_ready_selectors():
    nodes = screen(
        xml_node(desc="Settings", bounds="[0,0,500,100]", clickable=True),
        xml_node(text="Import / export", cls="android.widget.Button", bounds="[0,100,500,200]"),
    )
    rows = probe_rows(nodes, focus="dev.example/.Main")
    text = render_probe("dev.example/.Main", rows, unlabelled_clickable(nodes))
    assert text.splitlines()[0] == "screen  dev.example/.Main"
    assert '{"desc": "Settings"}' in text
    assert '{"text": "Import / export"}' in text


def test_probe_disambiguates_a_shared_label_by_class():
    nodes = screen(
        xml_node(text="Delete", cls="android.widget.Button", bounds="[0,0,100,50]"),
        xml_node(text="Delete", cls="android.widget.TextView", bounds="[0,50,100,100]"),
    )
    selectors = [row.selector for row in probe_rows(nodes)]
    assert selectors == [
        '{"class": "android.widget.Button", "text": "Delete"}',
        '{"class": "android.widget.TextView", "text": "Delete"}',
    ]


def test_probe_disambiguates_by_index_when_class_cannot():
    nodes = screen(
        xml_node(text="Notifications", bounds="[0,0,500,100]"),
        xml_node(cls="android.widget.Switch", checkable=True, bounds="[900,10,980,90]"),
        xml_node(text="Notifications", bounds="[0,100,500,200]"),
        xml_node(cls="android.widget.Switch", checkable=True, bounds="[900,110,980,190]"),
    )
    rows = probe_rows(nodes)
    assert [row.selector for row in rows] == [
        '{"index": 0, "text": "Notifications"}',
        '{"index": 1, "text": "Notifications"}',
    ]
    assert all("2 share label and class" in ", ".join(row.notes) for row in rows)


def test_probe_prefers_the_label_a_person_reads():
    nodes = screen(
        xml_node(id="app:id/row", clickable=True, bounds="[0,0,500,100]"),
        xml_node(text="Backup", bounds="[20,20,300,80]"),
    )
    rows = probe_rows(nodes)
    assert len(rows) == 1
    assert rows[0].selector == '{"text": "Backup"}'


def test_probe_hides_structural_ids_unless_asked():
    nodes = screen(
        xml_node(id="app:id/container", bounds="[0,0,1000,100]"),
        xml_node(id="android:id/title", cls="android.widget.EditText", bounds="[0,100,1000,200]"),
    )
    assert [row.selector for row in probe_rows(nodes)] == ['{"id": "android:id/title"}']
    assert len(probe_rows(nodes, show_all=True)) == 2


def test_unlabelled_clickables_are_counted():
    nodes = screen(
        xml_node(cls="android.widget.ImageView", clickable=True),
        xml_node(text="Settings", clickable=True),
    )
    assert unlabelled_clickable(nodes) == 1


def test_probe_rank_counts_the_field_the_selector_names():
    nodes = screen(
        xml_node(text="Delete", clickable=True, bounds="[0,0,100,50]"),
        xml_node(text="Delete", desc="Remove", clickable=True, bounds="[0,60,100,110]"),
        xml_node(text="Delete", clickable=True, bounds="[0,120,100,170]"),
    )

    rows = probe_rows(nodes)

    # The middle row is described as Remove, but resolve(text="Delete") still
    # matches it, so the third Delete is index 2, not index 1.
    selectors = [row.selector for row in rows]
    assert '{"index": 2, "text": "Delete"}' in selectors
    assert '{"index": 1, "text": "Delete"}' not in selectors
