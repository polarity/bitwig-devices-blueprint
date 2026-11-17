"""Native control atoms and Nitro port descriptors for hybrid devices.

Shared by the hybrid, note and instrument builders. These helpers were first
written in the Growl builder; they live here so no device depends on another
device's workspace.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys

from bitwig_nitro import decompile_nitrobin

from devices.common import panel_helpers as lab

# Short names the builders use.
h = lab.helpers
v, obj, field = lab.value, lab.obj, lab.field


def load(path: Path, name: str):
    """Import a Python file under an explicit module name."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class DecimalSpec:
    name: str
    identifier: str
    minimum: float
    maximum: float
    default: float
    digits: int = 1


@dataclass(frozen=True)
class ChoiceSpec:
    name: str
    identifier: str
    items: tuple[str, ...]
    default: int = 0
    automatable: bool = True


def set_existing(o: dict, key: str, data, kind: str | None = None) -> None:
    if key in o["fields"] and kind is None:
        o["fields"][key]["value"] = data
    else:
        o["fields"][key] = v(kind or "String", data)


def configure_decimal(template: dict, spec: DecimalSpec) -> dict:
    atom = copy.deepcopy(template)
    for key, data in (("name#374", spec.name), ("short_name#821", spec.name),
                      ("preset_identifier#701", spec.identifier), ("value#712", spec.default)):
        set_existing(atom, key, data, "Double" if key == "value#712" else "String")
    set_existing(atom, "allow_automation#7489", True, "Bool")
    set_existing(atom, "allow_modulation#7490", True, "Bool")
    settings = atom["fields"]["settings#6194"]["value"]
    set_existing(settings, "inport_connections#614", [], "ObjectList")
    value_type = atom["fields"]["value_type#702"]["value"]
    for key, data in (("min#292", spec.minimum), ("max#293", spec.maximum),
                      ("default_value#891", spec.default), ("pixel_step_size#1988", -1.0)):
        set_existing(value_type, key, float(data), "Double")
    set_existing(value_type, "domain#294", 0, "Byte")
    set_existing(value_type, "engine_domain#295", 0, "Byte")
    set_existing(value_type, "value_origin#3014", 2 if spec.minimum < 0 < spec.maximum else 0, "Byte")
    set_existing(value_type, "unit#296", 0, "Byte")
    set_existing(value_type, "#297", spec.digits, "Byte")
    set_existing(value_type, "parameter_smoothing#4433", False, "Bool")
    set_existing(value_type, "allow_automation_curves#4434", True, "Bool")
    return atom


def configure_choice(template: dict, spec: ChoiceSpec) -> dict:
    atom = copy.deepcopy(template)
    for key, data in (("name#374", spec.name), ("short_name#821", spec.name),
                      ("preset_identifier#701", spec.identifier)):
        set_existing(atom, key, data, "String")
    set_existing(atom, "value#457", spec.default, "Byte")
    set_existing(atom, "allow_automation#7489", spec.automatable, "Bool")
    set_existing(atom, "allow_modulation#7490", spec.automatable, "Bool")
    settings = atom["fields"]["settings#6194"]["value"]
    set_existing(settings, "inport_connections#614", [], "ObjectList")
    value_type = atom["fields"]["value_type#702"]["value"]
    set_existing(value_type, "default_value#6958", spec.default, "Byte")
    item_template = copy.deepcopy(value_type["fields"]["items#393"]["value"][0]["Object"])
    items = []
    for index, label in enumerate(spec.items):
        item = copy.deepcopy(item_template)
        set_existing(item, "id#650", index, "Byte")
        set_existing(item, "name#651", label, "String")
        set_existing(item, "icon#9823", "", "String")
        set_existing(item, "description#9824", "", "String")
        items.append({"Object": item})
    set_existing(value_type, "items#393", items, "ObjectList")
    return atom


NATIVE_NOTE_EVENT_TYPE = "{i32, i32, i32, float, i32, i32, i32, [ 16 x i8 ]}"


def compiled_ports(binary: bytes, *, native_note_abi: bool = False,
                   grid_abi: bool = False) -> tuple[str, dict[bool, list[dict]]]:
    ast = decompile_nitrobin(binary)
    component = next(item for item in ast.children[0] if item.kind == "ComponentDeclaration")
    ports: dict[bool, list[dict]] = {True: [], False: []}
    for declaration in component.children[1]:
        if declaration.kind != "PortDeclaration":
            continue
        port_type, name = declaration.children[:2]
        is_input = port_type.kind.startswith("Input")
        kind = {"Audio": 1, "Event": 2, "Value": 3}[
            port_type.kind.removeprefix("Input").removeprefix("Output").removesuffix("PortType")
        ]
        dtype = port_type.children[0]
        channels = 2 if dtype is None else 1
        if grid_abi and dtype is not None and dtype.kind == "ArrayType":
            frame_type, frame_count = dtype.children
            if frame_type.kind != "VectorType" or frame_count.children[1] != 4:
                raise ValueError(f"Grid audio port {name} must carry four stereo frames")
            scalar_type, channel_count = frame_type.children
            if scalar_type.kind != "Float32Type" or channel_count is None or channel_count.children[1] != 2:
                raise ValueError(f"Grid audio port {name} must use f32<2>[4]")
            channels, dtype = 8, scalar_type
        if dtype is not None and dtype.kind == "VectorType":
            scalar, length = dtype.children
            if grid_abi and (kind != 1 or scalar.kind != "Float32Type"
                             or length is None or length.children[1] != 4):
                raise ValueError(f"Grid audio port {name} must use f32<2>[4] for a Grid jack or f32<4> for a drawing output")
            channels, dtype = length.children[1], scalar
        scalar = "Float32Type" if dtype is None else dtype.kind
        event_type = {"Float32Type": "f32", "VoidType": "void"}.get(scalar, "") if kind == 2 else ""
        event_size = 4 if event_type == "f32" else 0
        if native_note_abi and kind == 2 and scalar == "NamedType" and dtype.children[0] == "NoteEvent":
            channels, event_type, event_size = 0, NATIVE_NOTE_EVENT_TYPE, 44
        if grid_abi and kind == 3 and scalar == "BooleanType":
            channels, event_type, event_size = 0, "i1", 1
        ports[is_input].append({
            "name#8557": name, "channel_count#8559": channels,
            "index#8560": len(ports[is_input]), "kind#8561": kind,
            "is_input#8562": is_input,
            "is_double_precision#10071": native_note_abi and kind == 1 and scalar == "Float64Type",
            "event_size#8565": event_size, "event_type#8566": event_type,
        })
    return component.children[0], ports


def make_port(template: dict, values: dict) -> dict:
    port = copy.deepcopy(template)
    for key, data in values.items():
        if key in port["fields"]:
            port["fields"][key]["value"] = data
        else:
            kind = "String" if isinstance(data, str) else "Bool" if isinstance(data, bool) else "Int32"
            port["fields"][key] = v(kind, data)
    return {"Object": port}
