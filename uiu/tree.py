"""The accessibility tree as parsed nodes: selectors, tap targets, probe rows.

A UIAutomator dump is an XML copy of the screen. Each `node` element carries the
text a person sees, a resource id, a content description, a class name and a
pixel rectangle. This module turns that into a flat list, and every step matches
selectors against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


class TreeError(ValueError):
    pass


class NoMatch(TreeError):
    pass


@dataclass(frozen=True)
class Rect:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def centre(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)

    @property
    def empty(self) -> bool:
        return self.x2 <= self.x1 or self.y2 <= self.y1

    def contains(self, x: int, y: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2


@dataclass(frozen=True)
class Node:
    index: int
    text: str
    resource_id: str
    desc: str
    class_name: str
    package: str
    checkable: bool
    checked: bool
    clickable: bool
    bounds: Rect


@dataclass(frozen=True)
class Selector:
    text: str = ""
    resource_id: str = ""
    desc: str = ""
    text_contains: str = ""
    class_name: str = ""
    index: int = 0

    @property
    def empty(self) -> bool:
        return not (
            self.text or self.resource_id or self.desc or self.text_contains or self.class_name
        )

    def matches(self, node: Node) -> bool:
        if self.text and node.text != self.text:
            return False
        if self.resource_id and node.resource_id != self.resource_id:
            return False
        if self.desc and node.desc != self.desc:
            return False
        if self.text_contains and self.text_contains not in node.text:
            return False
        return not self.class_name or node.class_name == self.class_name

    def __str__(self) -> str:
        parts = []
        for name, value in (
            ("text", self.text),
            ("id", self.resource_id),
            ("desc", self.desc),
            ("textContains", self.text_contains),
            ("class", self.class_name),
        ):
            if value:
                parts.append(f"{name}={value}")
        if self.index:
            parts.append(f"index={self.index}")
        return "{" + ", ".join(parts) + "}"


def _bounds(text: str) -> Rect:
    digits = text.replace("[", " ").replace("]", " ").replace(",", " ").split()
    if len(digits) != 4:
        return Rect(0, 0, 0, 0)
    try:
        x1, y1, x2, y2 = (int(part) for part in digits)
    except ValueError:
        return Rect(0, 0, 0, 0)
    return Rect(x1, y1, x2, y2)


def parse_dump(xml: str) -> list[Node]:
    """Decode a UIAutomator hierarchy, in document order, which selectors index into."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise TreeError(f"parsing dump: {error}") from error
    nodes: list[Node] = []
    for element in root.iter("node"):
        nodes.append(
            Node(
                index=len(nodes),
                text=element.get("text", ""),
                resource_id=element.get("resource-id", ""),
                desc=element.get("content-desc", ""),
                class_name=element.get("class", ""),
                package=element.get("package", ""),
                checkable=element.get("checkable") == "true",
                checked=element.get("checked") == "true",
                clickable=element.get("clickable") == "true",
                bounds=_bounds(element.get("bounds", "")),
            )
        )
    if not nodes:
        raise TreeError("dump has no nodes")
    return nodes


def resolve(nodes: list[Node], selector: Selector) -> Node:
    """The node a selector names, or NoMatch saying what it found instead."""
    if selector.empty:
        raise NoMatch("empty selector")
    found = 0
    for node in nodes:
        if not selector.matches(node):
            continue
        if found == selector.index:
            return node
        found += 1
    raise NoMatch(f"no matching node for {selector} ({found} match(es))")


def _smallest(nodes: list[Node], predicate, x: int, y: int) -> Node | None:
    best = None
    for node in nodes:
        if not predicate(node) or node.bounds.empty or not node.bounds.contains(x, y):
            continue
        if best is None or node.bounds.area < best.bounds.area:
            best = node
    return best


def _same_row_switch(nodes: list[Node], label: Node) -> Node | None:
    """A checkable control in the label's row, to its left or right.

    The vertical test is overlap, not the label's centre: a two-line row puts the
    switch beside the title and its summary, so its top can start below the
    title's centre. A radio row puts its control to the left of the label, a
    switch row to the right, and the nearest one wins.
    """
    best, best_distance = None, 0
    for node in nodes:
        if not node.checkable or node.bounds.empty:
            continue
        # Strict overlap: a control whose edge only touches the label's edge is
        # in the row above or below it.
        if node.bounds.y2 <= label.bounds.y1 or node.bounds.y1 >= label.bounds.y2:
            continue
        if node.bounds.x1 >= label.bounds.x2:
            distance = node.bounds.x1 - label.bounds.x2
        elif node.bounds.x2 <= label.bounds.x1:
            distance = label.bounds.x1 - node.bounds.x2
        else:
            # Horizontally overlapping the label means the control is part of
            # it, which the smallest-checkable rule already covers.
            continue
        if best is None or distance < best_distance:
            best, best_distance = node, distance
    return best


def hit_target(nodes: list[Node], node: Node) -> Node:
    """The node a tap on `node` should actually hit.

    A settings row often reports its state on a switch inside the row, so the
    smallest checkable node under the label wins. A row whose switch is a
    sibling to its right is covered by the row rule instead.
    """
    x, y = node.bounds.centre
    return (
        _smallest(nodes, lambda item: item.checkable, x, y)
        or _same_row_switch(nodes, node)
        or _smallest(nodes, lambda item: item.clickable, x, y)
        or node
    )


def label_of(node: Node) -> tuple[str, str]:
    """The field to write into a selector, preferring what a person sees."""
    if node.desc:
        return "desc", node.desc
    if node.text:
        return "text", node.text
    if node.resource_id:
        return "id", node.resource_id
    return "", ""


def field_value(node: Node, field: str) -> str:
    """The value a selector written on this field matches against."""
    if field == "desc":
        return node.desc
    if field == "text":
        return node.text
    if field == "id":
        return node.resource_id
    return ""


def kind_of(node: Node) -> str:
    leaf = node.class_name.rsplit(".", 1)[-1].lower()
    return leaf or "node"


def _selector_json(field: str, value: str, class_name: str | None, index: int | None) -> str:
    parts = []
    if class_name is not None:
        parts.append(f'"class": {_quote(class_name)}')
    if index is not None:
        parts.append(f'"index": {index}')
    parts.append(f'"{field}": {_quote(value)}')
    return "{" + ", ".join(parts) + "}"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class ProbeRow:
    selector: str
    notes: list[str]


def probe_rows(
    nodes: list[Node], focus: str | None = None, show_all: bool = False
) -> list[ProbeRow]:
    """One row per control, with the smallest selector that reaches it.

    Nodes whose taps route to the same control collapse into one line, so a
    settings row rendered as three nodes becomes one instruction. A label that
    two controls share is told apart by class where the classes differ, and by
    index where they do not.
    """
    labeled: list[Node] = []
    for node in nodes:
        field, _ = label_of(node)
        if not field:
            continue
        if (
            not show_all
            and not node.clickable
            and not node.checkable
            and field == "id"
            and "EditText" not in node.class_name
        ):
            continue
        labeled.append(node)

    chosen: dict[int, int] = {}
    order: list[int] = []

    def score(node: Node, target: Node) -> int:
        field, _ = label_of(node)
        points = 2 if field == "id" else 0
        if node.bounds != target.bounds:
            points += 1
        return points

    for position, node in enumerate(labeled):
        target = hit_target(nodes, node)
        if target.index not in chosen:
            chosen[target.index] = position
            order.append(target.index)
            continue
        previous = chosen[target.index]
        if score(node, target) < score(labeled[previous], target):
            chosen[target.index] = position

    groups: dict[tuple[str, str], list[int]] = {}
    rows: list[ProbeRow] = []
    for target_index in order:
        node = labeled[chosen[target_index]]
        target = hit_target(nodes, node)
        field, value = label_of(node)
        groups.setdefault((field, value), []).append(len(rows))
        notes = [kind_of(target)]
        focus_package = focus.split("/", 1)[0] if focus else ""
        if node.package and focus_package and node.package != focus_package:
            notes.append("in " + node.package)
        if target.checkable and target.checked:
            notes.append("checked")
        rows.append(ProbeRow(_selector_json(field, value, None, None), notes))

    for position, row in enumerate(rows):
        node = labeled[chosen[order[position]]]
        field, value = label_of(node)
        group = groups[(field, value)]
        if len(group) > 1:
            classes = {labeled[chosen[order[index]]].class_name for index in group}
            if len(classes) == len(group):
                row.selector = _selector_json(field, value, node.class_name, None)
                row.notes.append("label shared, class tells them apart")
            else:
                # The rank must count what a selector on this field would match,
                # not what each node's preferred label happens to be. A node with
                # both text and a description is counted by resolve(text=...),
                # and would otherwise be skipped here.
                rank = sum(
                    1
                    for other in nodes
                    if other.index < node.index and field_value(other, field) == value
                )
                row.selector = _selector_json(field, value, None, rank)
                row.notes.append(f"{len(group)} share label and class")
    return rows


def unlabelled_clickable(nodes: list[Node]) -> int:
    return sum(1 for node in nodes if not label_of(node)[0] and node.clickable)


def render_probe(focus: str | None, rows: list[ProbeRow], unlabelled: int) -> str:
    lines = []
    if focus:
        lines.append(f"screen  {focus}")
    width = max((len(row.selector) for row in rows), default=0)
    for position, row in enumerate(rows):
        lines.append(f"{position + 1:3d}  {row.selector:<{width}}  {', '.join(row.notes)}")
    if unlabelled:
        lines.append(
            f"{unlabelled} clickable node(s) carry no label, so no selector can reach them"
        )
    return "\n".join(lines) + "\n"
