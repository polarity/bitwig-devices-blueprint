#!/usr/bin/env python3
"""Profile-independent lints for parsed Bitwig 6.1.3 documents.

Every rule here encodes a defect that reached a host at least once and is
visible in the document alone. The lints read the JSON form produced by the
repository document codec; they compile nothing and prove no host behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
import re
from typing import Any, Iterable


# Highest header document version each document type accepts in Bitwig 6.1.3.
# Observed on the factory library of the exact Windows build: all devices carry
# 209, all modulators and Grid modules 202. A higher value fails on insert
# with InvalidDocumentVersionException although metadata indexing succeeds.
MAX_DOCUMENT_VERSION = {
    "application/bitwig-device": 209,
    "application/bitwig-modulator": 202,
    "application/bitwig-module": 202,
}
MODULATOR_TYPE = "application/bitwig-modulator"
MODULATOR_PANEL_SIZE = 17  # grid units; a taller main panel is clipped by the slot
GRID_INSPECTOR_WIDTH = 39  # native Windows 6.1.7 ADSR inspector
MAPPING_MIN_COLUMNS = 5    # the native mapping arrow has a fixed size
MAPPING_MIN_ROWS = 3
TOGGLE_ICON_MAX_PX = 9     # a native toggle face is lower than a three-row cell
NAME_FIELD_MAX_CHARS = 10

PROXY_PORT_CLASS = 154
NITRO_CLASS = 1721
DRAWING_CLASS = 5512
DRAWING_LANES = 4
NESTED_CHAIN_CLASS = 587
DELAY_COMPENSATION_CLASS = 1371
DYNAMIC_LATENCY_CLASS = 1438
FIXED_LATENCY_CLASS = 1437
TOGGLE_ITEM_CLASS = 1702
IMAGE_ITEM_CLASS = 1719
MAPPING_ITEM_CLASS = 1717
TEXT_FIELD_ITEM_CLASS = 1762
INDEXED_ATOM_CLASS = 180
PANEL_EXPRESSION_CLASS = 1728


@dataclass(frozen=True)
class Lint:
    severity: str
    rule: str
    message: str


def _fields(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields")
    return fields if isinstance(fields, dict) else {}


def _field(item: dict[str, Any], suffix: str) -> dict[str, Any] | None:
    matches = [value for name, value in _fields(item).items() if name.endswith(suffix)]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], dict) else None


def _value(item: dict[str, Any], suffix: str, default: Any = None) -> Any:
    field = _field(item, suffix)
    return default if field is None else field.get("value", default)


def owned_objects(root: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield objects in serialization order; object IDs are 1-based positions."""
    yield root
    for entry in _fields(root).values():
        kind, value = entry.get("type"), entry.get("value")
        if kind == "Object" and isinstance(value, dict):
            yield from owned_objects(value)
        elif kind == "ObjectList" and isinstance(value, list):
            for item in value:
                child = item.get("Object") if isinstance(item, dict) else None
                if isinstance(child, dict):
                    yield from owned_objects(child)


def _resolve(entry: Any, objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    child = entry.get("value") if entry.get("type") == "Object" else entry.get("Object")
    if isinstance(child, dict):
        return child
    reference = entry.get("value") if entry.get("type") == "ObjectRef" else entry.get("ObjectRef")
    if type(reference) is int and 1 <= reference <= len(objects):
        return objects[reference - 1]
    return None


def _list(item: dict[str, Any], suffix: str, objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = _value(item, suffix, [])
    resolved = (_resolve(entry, objects) for entry in entries if isinstance(entries, list))
    return [entry for entry in resolved if entry is not None]


def _document_type(document: dict[str, Any]) -> str:
    # Metadata keys are plain names; several of them end in "type".
    entry = _fields(document.get("metadata", {})).get("type", {})
    kind = entry.get("value") if isinstance(entry, dict) else ""
    return kind if isinstance(kind, str) else ""


def _describe(item: dict[str, Any], ids: dict[int, int]) -> str:
    name = item.get("class_name") or "unnamed"
    return f"object {ids.get(id(item), '?')} (class {item.get('class_id')} {name})"


def _connections(component: dict[str, Any], objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = _resolve(_field(component, "settings#6194") or {}, objects)
    return _list(settings, "inport_connections#614", objects) if settings else []


def lint_document_version(document: dict[str, Any]) -> list[Lint]:
    kind = _document_type(document)
    version = document.get("header", {}).get("document_version")
    limit = MAX_DOCUMENT_VERSION.get(kind)
    if limit is None or type(version) is not int or version <= limit:
        return []
    return [Lint("error", "DOC003",
                 f"Header document version {version} exceeds {limit}, the highest value Bitwig 6.1.3 "
                 f"accepts for {kind}; inserting the file fails with InvalidDocumentVersionException.")]


def lint_grid_sort_order(document: dict[str, Any]) -> list[Lint]:
    body = document.get("body")
    if not isinstance(body, dict) or body.get("class_id") != 1904:
        return []
    field = _field(body, "#10243")
    if field is None:
        return []
    order = field.get("value")
    if type(order) is int and -9 <= order <= 9:
        return []
    return [Lint("error", "DOC004", f"Grid module sort_order is {order!r}. Bitwig 6.1.7 accepts -9 through 9.")]


def lint_scheduling(document: dict[str, Any]) -> list[Lint]:
    """Every graph component must be registered in the root child list.

    A component serialized only inside a connection or inside another atom's
    field (for example an associated modulation source) is parsed but never
    scheduled, so its controls and outputs stay dead without any diagnostic.
    """
    body = document.get("body")
    if not isinstance(body, dict) or _field(body, "child_components#173") is None:
        return []
    objects = list(owned_objects(body))
    ids = {id(item): index for index, item in enumerate(objects, 1)}
    children = _list(body, "child_components#173", objects)
    scheduled = {id(item) for item in children}
    for suffix in ("proxy_in_ports#177", "proxy_out_ports#178"):
        scheduled.update(id(item) for item in _list(body, suffix, objects))
    findings = [
        Lint("error", "GRAPH008",
             f"Root child_components points to {_describe(item, ids)}, which has no component settings.")
        for item in children
        if _field(item, "settings#6194") is None
    ]
    for item in objects:
        if item is body or item.get("class_id") == PROXY_PORT_CLASS or id(item) in scheduled:
            continue
        settings = _field(item, "settings#6194")
        if settings is None or settings.get("type") not in ("Object", "ObjectRef"):
            continue
        findings.append(Lint("error", "GRAPH003",
                             f"Component {_describe(item, ids)} is serialized but absent from root "
                             "child_components; Bitwig never schedules it."))
    return findings


def lint_connection_widths(document: dict[str, Any]) -> list[Lint]:
    """Check connections that end in a Nitro output table or a drawing input."""
    body = document.get("body")
    if not isinstance(body, dict):
        return []
    objects = list(owned_objects(body))
    ids = {id(item): index for index, item in enumerate(objects, 1)}
    findings = []
    for consumer in objects:
        for position, connection in enumerate(_connections(consumer, objects)):
            source = _resolve(_field(connection, "source_component#248") or {}, objects)
            outport = _value(connection, "outport_index#249")
            if source is None or type(outport) is not int:
                continue
            if source.get("class_id") != NITRO_CLASS:
                if consumer.get("class_id") == DRAWING_CLASS:
                    findings.append(Lint("error", "ABI035",
                                         f"Drawing {_describe(consumer, ids)} input {position} is fed by "
                                         f"{_describe(source, ids)}, not by a four-lane Nitro output."))
                continue
            outputs = _list(source, "nitro_outputs#15757", objects)
            if outport >= len(outputs):
                findings.append(Lint("error", "GRAPH004",
                                     f"{_describe(consumer, ids)} input {position} reads output {outport} of "
                                     f"Nitro {_describe(source, ids)}, which declares {len(outputs)} output(s)."))
                continue
            if consumer.get("class_id") == DRAWING_CLASS:
                lanes = _value(outputs[outport], "channel_count#8559")
                if lanes != DRAWING_LANES:
                    findings.append(Lint("error", "ABI035",
                                         f"Drawing {_describe(consumer, ids)} input {position} reads a "
                                         f"{lanes}-lane Nitro output; a drawing input reads {DRAWING_LANES} lanes "
                                         "and a scalar source crashed the 6.1.3 audio engine. Pack it into f32<4>."))
    return findings


def lint_chain_delay_sources(document: dict[str, Any]) -> list[Lint]:
    """Flag dry taps tied directly to the same multi-output Nitro band splitter."""
    body = document.get("body")
    if not isinstance(body, dict):
        return []
    objects = list(owned_objects(body))
    ids = {id(item): index for index, item in enumerate(objects, 1)}
    chains: dict[int, set[int]] = {}
    dry_taps: set[tuple[int, int]] = set()
    latency_fanout: dict[tuple[int, int], set[int]] = {}
    split_boundaries: dict[int, set[int]] = {}
    compensated_boundaries: set[int] = set()
    for item in _list(body, "child_components#173", objects):
        kind = item.get("class_id")
        if kind not in (NESTED_CHAIN_CLASS, DELAY_COMPENSATION_CLASS):
            continue
        inputs = _connections(item, objects)
        if not inputs:
            continue
        source = _resolve(_field(inputs[0], "source_component#248"), objects)
        port = _value(inputs[0], "outport_index#249")
        if source and source.get("class_id") == FIXED_LATENCY_CLASS and port == 0:
            if kind == DELAY_COMPENSATION_CLASS:
                compensated_boundaries.add(ids[id(source)])
            else:
                boundary_inputs = _connections(source, objects)
                if boundary_inputs:
                    splitter = _resolve(_field(boundary_inputs[0], "source_component#248"), objects)
                    if splitter and splitter.get("class_id") == NITRO_CLASS:
                        split_boundaries.setdefault(ids[id(splitter)], set()).add(ids[id(source)])
        if kind == NESTED_CHAIN_CLASS and source and source.get("class_id") == DYNAMIC_LATENCY_CLASS:
            marker_inputs = _connections(source, objects)
            if len(marker_inputs) > 1:
                latency_input = marker_inputs[1]
                producer = _resolve(_field(latency_input, "source_component#248"), objects)
                latency_port = _value(latency_input, "outport_index#249")
                if producer and producer.get("class_id") == NITRO_CLASS and type(latency_port) is int:
                    latency_fanout.setdefault((ids[id(producer)], latency_port), set()).add(ids[id(source)])
        if source is None or source.get("class_id") != NITRO_CLASS or type(port) is not int:
            continue
        outputs = _list(source, "nitro_outputs#15757", objects)
        if not 0 <= port < len(outputs) or _value(outputs[port], "kind#8561") != 1:
            continue
        source_id = ids[id(source)]
        if kind == NESTED_CHAIN_CLASS:
            chains.setdefault(source_id, set()).add(port)
        else:
            dry_taps.add((source_id, port))
    findings = [
        Lint("error", "GRAPH005",
             f"Nitro component {source} directly feeds several nested-chain bands and matching "
             "dry delay compensation. Use a separate single-output source per band for the "
             "chain input and its dry tap. This topology was present when FX-4 failed latency reconfiguration.")
        for source, ports in chains.items()
        if len(ports) > 1 and sum((source, port) in dry_taps for port in ports) > 1
    ]
    for (producer, port), markers in latency_fanout.items():
        if len(markers) > 1:
            findings.append(Lint(
                "error", "GRAPH006",
                f"Nitro component {producer} output {port} drives {len(markers)} dynamic latency "
                "markers upstream of nested chains. FX-4 overflowed Bitwig's latency compensation "
                "with this topology. Report the common latency once after recombining the bands.",
            ))
    for producer, boundaries in split_boundaries.items():
        if len(boundaries & compensated_boundaries) > 1:
            findings.append(Lint(
                "error", "GRAPH007",
                f"Nitro splitter {producer} has parallel dry compensation taps at its fixed "
                "nested-chain boundaries. This FX-4 topology aborted delay reconfiguration. "
                "Sum the unprocessed bands before one dry compensation point.",
            ))
    return findings


def _layout(item: dict[str, Any], objects: list[dict[str, Any]]) -> tuple[int, int, int, int] | None:
    layout = _resolve(_field(item, "layout_settings#6226") or {}, objects)
    if layout is None:
        return None
    rect = tuple(_value(layout, suffix) for suffix in ("x#6215", "y#6216", "width#6217", "height#6218"))
    return rect if all(type(number) is int for number in rect) else None


def _main_panel(body: dict[str, Any], objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    for panel in _list(body, "panels#6213", objects):
        if _value(panel, "name#6211") == "Main":
            return panel
    return None


def lint_modulator_panel(document: dict[str, Any]) -> list[Lint]:
    if _document_type(document) != MODULATOR_TYPE or not isinstance(document.get("body"), dict):
        return []
    body = document["body"]
    objects = list(owned_objects(body))
    panel = _main_panel(body, objects)
    if panel is None:
        return [Lint("error", "PANEL016", "Modulator has no panel named Main.")]
    findings = []
    width, height = _value(panel, "width#6232"), _value(panel, "height#6233")
    if width != MODULATOR_PANEL_SIZE or type(height) is not int or height > MODULATOR_PANEL_SIZE:
        findings.append(Lint("error", "PANEL016",
                             f"Modulator main panel is {width}x{height}; the modulator slot shows "
                             f"{MODULATOR_PANEL_SIZE}x{MODULATOR_PANEL_SIZE} grid units and clips the rest."))
    root = _resolve(_field(panel, "root_item#6212") or {}, objects)
    items = _list(root, "items#6221", objects) if root else []
    rects = [(item, _layout(item, objects)) for item in items]
    for item, rect in rects:
        if rect is None or _value(item, "is_visible#6309", True) is False:
            continue
        x, y, columns, rows = rect
        label = f"class {item.get('class_id')} {item.get('class_name') or 'unnamed'} at {x},{y} {columns}x{rows}"
        if x + columns > MODULATOR_PANEL_SIZE or y + rows > MODULATOR_PANEL_SIZE:
            findings.append(Lint("error", "PANEL016",
                                 f"Modulator panel item ({label}) extends beyond the visible "
                                 f"{MODULATOR_PANEL_SIZE}x{MODULATOR_PANEL_SIZE} grid."))
        if item.get("class_id") == MAPPING_ITEM_CLASS and (
                columns < MAPPING_MIN_COLUMNS or rows < MAPPING_MIN_ROWS):
            findings.append(Lint("error", "PANEL017",
                                 f"Mapping button ({label}) is smaller than {MAPPING_MIN_COLUMNS}x"
                                 f"{MAPPING_MIN_ROWS}; its fixed-size arrow is clipped."))
        if item.get("class_id") == IMAGE_ITEM_CLASS:
            svg = _value(item, "svg_data#6259", "")
            match = re.search(r"<svg[^>]*\bheight=\"([0-9.]+)\"", svg if isinstance(svg, str) else "")
            covers_toggle = any(other.get("class_id") == TOGGLE_ITEM_CLASS and other_rect == rect
                                for other, other_rect in rects)
            if match and covers_toggle and float(match.group(1)) > TOGGLE_ICON_MAX_PX:
                findings.append(Lint("warning", "PANEL018",
                                     f"Embedded icon ({label}) is {match.group(1)} px high over a native "
                                     f"toggle; icons above {TOGGLE_ICON_MAX_PX} px spilled over the toggle face."))
        if item.get("class_id") == TEXT_FIELD_ITEM_CLASS:
            model = _resolve(_field(item, "data_model#6220") or {}, objects)
            text = _value(model, "value#6391", "") if model else ""
            if isinstance(text, str) and (len(text) > NAME_FIELD_MAX_CHARS or re.search(r"\s", text.strip())):
                findings.append(Lint("warning", "PANEL019",
                                     f"Name field default {text!r} is longer than {NAME_FIELD_MAX_CHARS} "
                                     "characters or has several words; the modulator slot cut or wrapped it."))
    return findings


def lint_clickable_containers(document: dict[str, Any]) -> list[Lint]:
    """Native clickable stacks derive their minimum hit area from children."""
    objects = list(owned_objects(document.get("body", {})))
    ids = {id(item): index for index, item in enumerate(objects, 1)}
    return [
        Lint("error", "PANEL020", f"{_describe(item, ids)} has an action but no child to size its hit area.")
        for item in objects
        if item.get("class_id") == 3405
        and any(_value(item, field) for field in ("#10185", "#10186"))
        and not _list(item, "#6221", objects)
    ]


def lint_indexed_panel_expressions(document: dict[str, Any]) -> list[Lint]:
    """Panel expressions bind indexed atom identifiers as integers."""
    objects = list(owned_objects(document.get("body", {})))
    indexed = set()
    for item in objects:
        if item.get("class_id") == INDEXED_ATOM_CLASS:
            name = _value(item, "#701")
            if isinstance(name, str):
                indexed.add(name)

    findings = []
    member = re.compile(r'"(?:[^"\\]|\\.)*"|(?<![\w.])([A-Za-z_]\w*)\s*\.\s*value\b')
    for item in objects:
        if item.get("class_id") != PANEL_EXPRESSION_CLASS:
            continue
        source = _value(item, "#6274")
        if not isinstance(source, str):
            continue
        names = {match.group(1) for match in member.finditer(source)}
        for name in sorted(names & indexed):
            findings.append(Lint("error", "PANEL021",
                                 f"Panel expression reads {name}.value. Read {name} directly."))
    return findings


def lint_grid_toggle_labels(document: dict[str, Any]) -> list[Lint]:
    """An icon-only Grid toggle needs an icon to show its purpose."""
    if _document_type(document) != "application/bitwig-module":
        return []
    return [
        Lint("error", "PANEL022", f"Grid toggle {_value(item, '#6241')!r} is icon-only and has no icon.")
        for item in owned_objects(document.get("body", {}))
        if item.get("class_id") == TOGGLE_ITEM_CLASS
        and _value(item, "#6309", True)
        and _value(item, "#6241")
        and _value(item, "#6267") == 1
        and not _value(item, "#6239")
    ]


def lint_grid_inspector_width(document: dict[str, Any]) -> list[Lint]:
    """Keep custom Inspector content within Bitwig's native sidebar width."""
    if _document_type(document) != "application/bitwig-module":
        return []
    body = document.get("body", {})
    objects = list(owned_objects(body))
    inspector = _resolve(_field(body, "#9820"), objects)
    if inspector is None:
        return []

    def content_width(item):
        layout = _resolve(_field(item, "#6226"), objects) or {}
        width = _value(layout, "#6217", 0)
        children = _list(item, "#6221", objects)
        if item.get("class_id") == 1679 and _value(item, "#6209", False):
            gap = max(0, len(children) - 1) if _value(item, "#9821", False) else 0
            return max(width, sum(content_width(child) for child in children) + gap)
        extent = max((content_width(child) for child in children), default=0)
        return _value(layout, "#6215", 0) + max(width, extent)

    width = _value(inspector, "#6232", 0)
    if not 1 <= width <= GRID_INSPECTOR_WIDTH or _value(inspector, "#10404", False):
        return [Lint("error", "PANEL025", f"Grid Inspector must have a fixed width of at most {GRID_INSPECTOR_WIDTH} units.")]
    root = _resolve(_field(inspector, "#6212"), objects)
    if root is not None and content_width(root) > width:
        return [Lint("error", "PANEL025", f"Grid Inspector content exceeds its declared {width}-unit width.")]
    return []


def lint_sonogram_tile_feeds(document: dict[str, Any]) -> list[Lint]:
    """Give each Sonogram tile a fixed scalar feed from its frequency bank."""
    body = document.get("body", {})
    if (_document_type(document) != "application/bitwig-module"
            or _value(body, "#6369") != "39e52c1c-4642-4f1e-9bc5-83b036beb1c1"):
        return []
    objects = list(owned_objects(body))

    def feed(component, count):
        links = _connections(component, objects)
        if len(links) < count:
            return None, None
        link = links[0]
        return _resolve(_field(link, "#248"), objects), _value(link, "#249")

    tiles = [item for item in objects if item.get("class_id") == DRAWING_CLASS]
    revision = _value(body, "#13647", 1)
    paired = revision == 8
    if revision >= 12:
        tile_count, sections = 8, 2
    elif revision >= 10:
        tile_count, sections = 12, 4
    else:
        tile_count, sections = (8, 3) if revision >= 8 else (6, 2 if revision >= 3 else 1)
    expected = {f"SONOGRAM_TILE_{index}": index for index in range(tile_count * sections)}
    names = [_value(tile, "#701") for tile in tiles]
    if len(names) != len(expected) or set(names) != expected.keys():
        return [Lint("error", "PANEL023", "Sonogram must have each frequency and time tile exactly once.")]
    for name, tile in zip(names, tiles):
        section, frequency = divmod(expected[name], tile_count)
        bank, lane = divmod(frequency, tile_count // 2)
        pair, port = divmod(lane, 2) if paired else (0, lane)
        output_count = 2 if paired else 3
        if revision >= 10:
            pair, port = divmod(lane, 2)
            output_count = 2
        elif revision == 9 and lane == 3:
            port, output_count = 0, 1
        history, _ = feed(tile, 3)
        history_inputs = _connections(history, objects) if history else []
        splitter, scalar_port = feed(history, 1) if history else (None, None)
        splitter_inputs = _connections(splitter, objects) if splitter else []
        analyzer, bank_port = feed(splitter, 1) if splitter else (None, None)
        if revision >= 10:
            vector_split = analyzer
            if (bank_port != pair or vector_split is None
                    or len(_list(vector_split, "#15757", objects)) != tile_count // 4):
                return [Lint("error", "PANEL023", f"{name} must read the matching packed pair of its frequency bank.")]
            analyzer, bank_port = feed(vector_split, 1)
        for _ in range(section):
            analyzer, bank_port = feed(analyzer, 1) if analyzer and bank_port == 0 else (None, None)
        if paired:
            if (len(splitter_inputs) != 2 or (_field(splitter_inputs[1], "#248") or {}).get("type") != "NullObject"
                    or _value(splitter_inputs[1], "#6443") != pair):
                return [Lint("error", "PANEL023", f"{name} must read the matching pair of its frequency bank.")]
        outputs = _list(splitter, "#15757", objects) if splitter else []
        if (len(history_inputs) != 1 or scalar_port != port or bank_port != 1 + bank
                or analyzer is None or len(outputs) != output_count or len(splitter_inputs) != (2 if paired else 1)
                or _value(outputs[port], "#8559") != 1):
            return [Lint("error", "PANEL023", f"{name} must read its dedicated scalar splitter output.")]
    return []


def lint_vectorscope_tile_feeds(document: dict[str, Any]) -> list[Lint]:
    """Keep phosphor tile identities on distinct connected outputs."""
    body = document.get("body", {})
    if (_document_type(document) != "application/bitwig-module"
            or _value(body, "#6369") != "6e988a57-2a23-4283-b1a8-39768b391545"
            or _value(body, "#13647", 1) < 4):
        return []
    objects = list(owned_objects(body))
    revision = _value(body, "#13647", 1)
    if revision >= 10:
        arities = ()
    elif revision >= 8:
        arities = (2,) * 6
    else:
        arities = (3, 3, 2, 2) if revision >= 6 else (2,) * (4 if revision == 5 else 2)
    expected = {f"VECTORSCOPE_PHOSPHOR_{tile}": tile for tile in range(prod(arities))}
    tiles = [(name, item) for item in objects if item.get("class_id") == DRAWING_CLASS
             if (name := _value(item, "#701") or "").startswith("VECTORSCOPE_PHOSPHOR_")]
    if len(tiles) != len(expected) or {name for name, _ in tiles} != expected.keys():
        return [Lint("error", "PANEL024", f"Vectorscope must contain all {len(expected)} phosphor drawings.")]

    def feed(component, *, drawing=False):
        links = _connections(component, objects) if component else []
        if len(links) != (9 if drawing else 1):
            return None, None
        return _resolve(_field(links[0], "#248"), objects), _value(links[0], "#249")

    roots = set()
    for name, drawing in tiles:
        root_split, _ = feed(drawing, drawing=True)
        address = expected[name]
        for arity in reversed(arities):
            address, expected_port = divmod(address, arity)
            root_split, port = feed(root_split)
            if root_split is None or port != expected_port:
                return [Lint("error", "PANEL024", f"{name} must use its own routed tile signal.")]
        feeder, port = feed(root_split)
        if feeder is None or port != 2:
            return [Lint("error", "PANEL024", f"{name} must use the motion blur input feed.")]
        roots.add(id(root_split))
    return [] if len(roots) == 1 else [Lint("error", "PANEL024", "Vectorscope tiles must share one routing root.")]


def lint_document(document: dict[str, Any]) -> list[Lint]:
    """Run every document lint; order is stable for reports."""
    return [
        *lint_document_version(document),
        *lint_grid_sort_order(document),
        *lint_scheduling(document),
        *lint_connection_widths(document),
        *lint_chain_delay_sources(document),
        *lint_modulator_panel(document),
        *lint_clickable_containers(document),
        *lint_indexed_panel_expressions(document),
        *lint_grid_toggle_labels(document),
        *lint_grid_inspector_width(document),
        *lint_sonogram_tile_feeds(document),
        *lint_vectorscope_tile_feeds(document),
    ]
