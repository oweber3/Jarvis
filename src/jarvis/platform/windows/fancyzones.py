"""PowerToys FancyZones layouts as zone rectangles (read-only, offline).

Pure parsing and geometry over the files PowerToys writes: the applied, custom and
default layouts and the FancyZones settings. Nothing here writes to those files, calls
the operating system, or knows about tools or models; the caller supplies the display
identities, the current virtual desktop and the label vocabulary.

The geometry reproduces PowerToys' own layout code, including its integer rounding, so
a zone here is the rectangle FancyZones snaps a window into. Canvas layouts are scaled
in double precision rather than PowerToys' single precision, which can differ by a pixel
only where a scaled coordinate lands within rounding error of a whole pixel.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from ...debug import debug_log

Rectangle = tuple[int, int, int, int]

_MULTIPLIER = 10000
# Predefined priority-grid layouts for one to ten zones: row percentages, column
# percentages and the cell-to-zone map. Larger counts are plain grids.
_PRIORITY_GRIDS = (
    ([10000], [10000], [[0]]),
    ([10000], [6667, 3333], [[0, 1]]),
    ([10000], [2500, 5000, 2500], [[0, 1, 2]]),
    ([5000, 5000], [2500, 5000, 2500], [[0, 1, 2], [0, 1, 3]]),
    ([5000, 5000], [2500, 5000, 2500], [[0, 1, 2], [3, 1, 4]]),
    ([3333, 3334, 3333], [2500, 5000, 2500], [[0, 1, 2], [0, 1, 3], [4, 1, 5]]),
    ([3333, 3334, 3333], [2500, 5000, 2500], [[0, 1, 2], [3, 1, 4], [5, 1, 6]]),
    ([3333, 3334, 3333], [2500] * 4, [[0, 1, 2, 3], [4, 1, 2, 5], [6, 1, 2, 7]]),
    ([3333, 3334, 3333], [2500] * 4, [[0, 1, 2, 3], [4, 1, 2, 5], [6, 1, 7, 8]]),
    ([3333, 3334, 3333], [2500] * 4, [[0, 1, 2, 3], [4, 1, 5, 6], [7, 1, 8, 9]]),
)
_TEMPLATES = ('columns', 'rows', 'grid', 'priority-grid', 'focus', 'blank')
_LAYOUT_TYPES = (*_TEMPLATES, 'custom')
# PowerToys' built-in defaults when default-layouts.json does not say otherwise.
_BUILTIN_DEFAULT_SPACING = 16
_BUILTIN_DEFAULT_ZONES = 3
_LABEL_DIR = Path(__file__).parent / 'zone_labels'
# How far from an edge or the centre still counts as touching it, as a fraction of the work area.
_EDGE_TOLERANCE = 0.05


class LayoutError(ValueError):
    """A layout that PowerToys itself could not turn into zones."""


# --- geometry ------------------------------------------------------------------------

def _valid(zones: list[Rectangle]) -> list[Rectangle]:
    if any(right <= left or bottom <= top for left, top, right, bottom in zones):
        raise LayoutError('A zone has no area.')
    return zones


def _spans(percents: Sequence[int], extent: int) -> list[tuple[int, int]]:
    """Cumulative edges: the sum of all spans is exactly the extent."""
    spans, total = [], 0
    for percent in percents:
        start = total * extent // _MULTIPLIER
        total += percent
        spans.append((start, total * extent // _MULTIPLIER))
    return spans


def _grid_zones(width: int, height: int, rows: Sequence[int], columns: Sequence[int],
                cells: Sequence[Sequence[int]], spacing: int) -> list[Rectangle]:
    row_spans, column_spans = _spans(rows, height), _spans(columns, width)
    zones: dict[int, Rectangle] = {}
    for row in range(len(rows)):
        for column in range(len(columns)):
            index = cells[row][column]
            if (row and cells[row - 1][column] == index) or (column and cells[row][column - 1] == index):
                continue  # not this zone's top-left cell
            last_row = row
            while last_row + 1 < len(rows) and cells[last_row + 1][column] == index:
                last_row += 1
            last_column = column
            while last_column + 1 < len(columns) and cells[row][last_column + 1] == index:
                last_column += 1
            if index in zones:
                raise LayoutError('A zone index is used by separate regions.')
            half = spacing // 2
            zones[index] = (column_spans[column][0] + (spacing if column == 0 else half),
                            row_spans[row][0] + (spacing if row == 0 else half),
                            column_spans[last_column][1] - (spacing if last_column == len(columns) - 1 else half),
                            row_spans[last_row][1] - (spacing if last_row == len(rows) - 1 else half))
    if set(zones) != set(range(len(zones))):
        raise LayoutError('Zone indices are not consecutive.')
    return _valid([zones[index] for index in sorted(zones)])


def _columns(width: int, height: int, count: int, spacing: int) -> list[Rectangle]:
    total_width, total_height = width - spacing * (count + 1), height - spacing * 2
    zones, left = [], spacing
    for index in range(count):
        right = left + (index + 1) * total_width // count - index * total_width // count
        zones.append((left, spacing, right, total_height + spacing))
        left = right + spacing
    return _valid(zones)


def _rows(width: int, height: int, count: int, spacing: int) -> list[Rectangle]:
    total_width, total_height = width - spacing * 2, height - spacing * (count + 1)
    zones, top = [], spacing
    for index in range(count):
        bottom = top + (index + 1) * total_height // count - index * total_height // count
        zones.append((spacing, top, total_width + spacing, bottom))
        top = bottom + spacing
    return _valid(zones)


def _grid(width: int, height: int, count: int, spacing: int) -> list[Rectangle]:
    rows = 1
    while count // rows >= rows:
        rows += 1
    rows -= 1
    columns = count // rows + (0 if count % rows == 0 else 1)
    row_percents = [_MULTIPLIER * (row + 1) // rows - _MULTIPLIER * row // rows for row in range(rows)]
    column_percents = [_MULTIPLIER * (col + 1) // columns - _MULTIPLIER * col // columns for col in range(columns)]
    cells, index = [], 0
    for _ in range(rows):
        line = []
        for _ in range(columns):
            line.append(index)
            index += 1
            if index == count:
                index -= 1  # the last zone absorbs the cells that remain
        cells.append(line)
    return _grid_zones(width, height, row_percents, column_percents, cells, spacing)


def _focus(width: int, height: int, count: int) -> list[Rectangle]:
    left, top = 100, 100
    right, bottom = left + int(width * 0.4), top + int(height * 0.4)
    step = 0 if count <= 1 else 50
    zones = []
    for _ in range(count):
        zones.append((left, top, right, bottom))
        left, right, top, bottom = left + step, right + step, top + step, bottom + step
    return _valid(zones)


def template_zones(kind: str, width: int, height: int, count: int, spacing: int) -> list[Rectangle]:
    """Zones of a built-in layout, relative to the work area's top-left corner.

    ``spacing`` is the effective gap in pixels (zero when the layout hides spacing).
    """
    if kind not in _TEMPLATES:
        raise LayoutError('Unknown layout type.')
    if kind == 'blank' or (kind == 'focus' and count == 0):
        return []
    if count <= 0 or width <= 0 or height <= 0:
        raise LayoutError('A layout needs a positive zone count and work area.')
    spacing = max(spacing, 0)
    if kind == 'columns':
        return _columns(width, height, count, spacing)
    if kind == 'rows':
        return _rows(width, height, count, spacing)
    if kind == 'focus':
        return _focus(width, height, count)
    if kind == 'priority-grid' and count <= len(_PRIORITY_GRIDS):
        rows, columns, cells = _PRIORITY_GRIDS[count - 1]
        return _grid_zones(width, height, rows, columns, cells, spacing)
    return _grid(width, height, count, spacing)


@dataclass(frozen=True)
class AppliedLayout:
    kind: str
    uuid: uuid.UUID | None
    show_spacing: bool
    spacing: int
    zone_count: int

    @property
    def effective_spacing(self) -> int:
        return self.spacing if self.show_spacing else 0


def _canvas_zones(custom: dict, width: int, height: int) -> list[Rectangle]:
    zones = []
    for x, y, zone_width, zone_height in custom['zones']:
        left, top = x * width / custom['ref_width'], y * height / custom['ref_height']
        zones.append((int(left), int(top), int(left + zone_width * width / custom['ref_width']),
                      int(top + zone_height * height / custom['ref_height'])))
    return _valid(zones)


def _zones_for(layout: AppliedLayout, custom: dict, width: int, height: int) -> list[Rectangle]:
    if layout.kind != 'custom':
        return template_zones(layout.kind, width, height, layout.zone_count, layout.effective_spacing)
    definition = custom.get(layout.uuid)
    if definition is None:
        raise LayoutError('Custom layout not found.')
    if definition['type'] == 'canvas':
        return _canvas_zones(definition, width, height)
    spacing = definition['spacing'] if definition['show_spacing'] else 0
    return _grid_zones(width, height, definition['rows'], definition['columns'], definition['cells'], spacing)


def layout_zones(applied: dict, custom: dict, width: int, height: int) -> list[Rectangle]:
    """Zones of an ``applied-layout`` object, relative to the work area's top-left corner."""
    layout = _parse_layout(applied)
    if layout is None:
        raise LayoutError('Unreadable layout.')
    return _zones_for(layout, custom, width, height)


# --- parsing -------------------------------------------------------------------------

def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _guid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _parse_layout(raw) -> AppliedLayout | None:
    if not isinstance(raw, dict) or raw.get('type') not in _LAYOUT_TYPES:
        return None
    count, spacing, show = _int(raw.get('zone-count')), _int(raw.get('spacing', 0)), raw.get('show-spacing', False)
    if count is None or count < 0 or spacing is None or not isinstance(show, bool):
        return None
    identifier = _guid(raw.get('uuid')) if raw.get('uuid') else None
    if raw['type'] == 'custom' and identifier is None:
        return None
    return AppliedLayout(raw['type'], identifier, show, spacing, count)


def _int_list(value) -> list[int] | None:
    if isinstance(value, list) and all(_int(item) is not None for item in value):
        return list(value)
    return None


def _parse_custom(raw) -> tuple[uuid.UUID, dict] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get('info'), dict):
        return None
    identifier, info = _guid(raw.get('uuid')), raw['info']
    if identifier is None:
        return None
    if raw.get('type') == 'canvas':
        ref_width, ref_height, zones = _int(info.get('ref-width')), _int(info.get('ref-height')), info.get('zones')
        if not ref_width or not ref_height or ref_width < 0 or ref_height < 0 or not isinstance(zones, list):
            return None
        parsed = []
        for zone in zones:
            numbers = [_int(zone.get(key)) for key in ('X', 'Y', 'width', 'height')] if isinstance(zone, dict) else []
            if not numbers or None in numbers:
                return None
            parsed.append(tuple(numbers))
        return identifier, {'type': 'canvas', 'ref_width': ref_width, 'ref_height': ref_height, 'zones': parsed}
    if raw.get('type') == 'grid':
        rows, columns = _int(info.get('rows')), _int(info.get('columns'))
        row_percents, column_percents = _int_list(info.get('rows-percentage')), _int_list(info.get('columns-percentage'))
        cells = info.get('cell-child-map')
        if None in (rows, columns, row_percents, column_percents) or not rows or not columns:
            return None
        if len(row_percents) != rows or len(column_percents) != columns:
            return None
        if not isinstance(cells, list) or len(cells) != rows:
            return None
        parsed_cells = [_int_list(line) for line in cells]
        if any(line is None or len(line) != columns for line in parsed_cells):
            return None
        spacing = _int(info.get('spacing', 16))
        show = info.get('show-spacing', True)
        if spacing is None or not isinstance(show, bool):
            return None
        return identifier, {'type': 'grid', 'rows': row_percents, 'columns': column_percents, 'cells': parsed_cells,
                            'spacing': spacing, 'show_spacing': show}
    return None


@dataclass(frozen=True)
class AppliedEntry:
    monitor: str
    instance: str
    serial: str
    number: int
    desktop: uuid.UUID
    layout: AppliedLayout


@dataclass(frozen=True)
class FancyZonesData:
    applied: tuple[AppliedEntry, ...]
    custom: dict
    defaults: dict  # 'horizontal' / 'vertical' -> AppliedLayout
    spans_monitors: bool = False


def _list_in(raw, key: str) -> list:
    items = raw.get(key) if isinstance(raw, dict) else None
    return items if isinstance(items, list) else []


def _parse_entry(raw) -> AppliedEntry | None:
    if not isinstance(raw, dict) or not isinstance(raw.get('device'), dict):
        return None
    device, layout = raw['device'], _parse_layout(raw.get('applied-layout'))
    desktop, number = _guid(device.get('virtual-desktop')), _int(device.get('monitor-number', 0))
    texts = [device.get(key, '') for key in ('monitor', 'monitor-instance', 'serial-number')]
    if layout is None or desktop is None or number is None or not all(isinstance(text, str) for text in texts):
        return None
    if not texts[0]:
        return None
    return AppliedEntry(texts[0], texts[1], texts[2], number, desktop, layout)


def parse_data(applied, custom, defaults, settings) -> FancyZonesData:
    """Build usable data from the loaded JSON of the four files; anything malformed is skipped."""
    entries = tuple(entry for entry in map(_parse_entry, _list_in(applied, 'applied-layouts')) if entry)
    custom_layouts = dict(pair for pair in map(_parse_custom, _list_in(custom, 'custom-layouts')) if pair)
    default_layouts = {}
    for item in _list_in(defaults, 'default-layouts'):
        layout = _parse_layout(item.get('layout')) if isinstance(item, dict) else None
        if layout and item.get('monitor-configuration') in ('horizontal', 'vertical'):
            default_layouts[item['monitor-configuration']] = layout
    properties = settings.get('properties') if isinstance(settings, dict) else None
    span = (properties or {}).get('fancyzones_span_zones_across_monitors') if isinstance(properties, dict) else None
    spans = isinstance(span, dict) and span.get('value') is True
    return FancyZonesData(entries, custom_layouts, default_layouts, spans)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def load_data(directory: Path) -> FancyZonesData | None:
    """Read PowerToys' FancyZones folder. ``None`` when the applied layouts are unavailable."""
    applied = _read_json(directory / 'applied-layouts.json')
    if not isinstance(applied, dict):
        debug_log('FancyZones data is not available.', 'windows')
        return None
    data = parse_data(applied, _read_json(directory / 'custom-layouts.json'),
                      _read_json(directory / 'default-layouts.json'), _read_json(directory / 'settings.json'))
    debug_log(f'FancyZones data loaded: {len(data.applied)} applied and {len(data.custom)} custom layout(s).',
              'windows')
    return data


# --- matching connected displays -------------------------------------------------------

@dataclass(frozen=True)
class DisplayIdentity:
    """What FancyZones knows a display by. ``hardware_id`` is the EDID-derived monitor id
    (empty when Windows reports none, when the device name stands in for it)."""
    device: str
    number: int
    hardware_id: str = ''
    instance_id: str = ''
    serial: str = ''


@dataclass(frozen=True)
class DisplayInfo:
    identity: DisplayIdentity
    bounds: Rectangle
    work_area: Rectangle


@dataclass(frozen=True)
class ZoneSet:
    layout: str
    rectangles: tuple[Rectangle, ...]
    names: tuple[tuple[str, ...], ...]


_AMBIGUOUS = object()


def _fold(text: str) -> str:
    return text.casefold()


def _match_rank(entry: AppliedEntry, identity: DisplayIdentity) -> int:
    """0: not this display; 2: same instance; 1: same monitor and display number.

    Mirrors PowerToys: the monitor ids must agree; the instances agree or the display
    numbers do; serial numbers veto only when both are known."""
    if _fold(entry.monitor) != _fold(identity.hardware_id or identity.device):
        return 0
    if entry.serial and identity.serial and entry.serial != identity.serial:
        return 0
    if _fold(entry.instance) == _fold(identity.instance_id):
        return 2
    return 1 if entry.number == identity.number else 0


def _find_layout(display: DisplayInfo, data: FancyZonesData, desktop: uuid.UUID):
    ranked = [(rank, entry.layout) for entry in data.applied
              if entry.desktop == desktop and (rank := _match_rank(entry, display.identity))]
    if not ranked:
        return _default_layout(display, data)
    best = max(rank for rank, _ in ranked)
    layouts = {layout for rank, layout in ranked if rank == best}
    return next(iter(layouts)) if len(layouts) == 1 else _AMBIGUOUS


def _default_layout(display: DisplayInfo, data: FancyZonesData) -> AppliedLayout:
    left, top, right, bottom = display.bounds
    orientation = 'vertical' if bottom - top > right - left else 'horizontal'
    if orientation in data.defaults:
        return data.defaults[orientation]
    kind = 'rows' if orientation == 'vertical' else 'priority-grid'
    return AppliedLayout(kind, None, True, _BUILTIN_DEFAULT_SPACING, _BUILTIN_DEFAULT_ZONES)


def zone_sets(displays: Sequence[DisplayInfo], data: FancyZonesData, desktop: uuid.UUID,
              labels: Sequence[dict]) -> dict[str, ZoneSet]:
    """The FancyZones zones of each connected display on one virtual desktop, keyed by device.

    Only connected displays are considered, so entries for disconnected monitors and other
    desktops never contribute. A display whose layout cannot be identified or computed is left
    out rather than guessed; the others are unaffected."""
    if data.spans_monitors:
        debug_log('FancyZones spans zones across monitors, which is not supported.', 'windows')
        return {}
    result: dict[str, ZoneSet] = {}
    unresolved = 0
    for display in displays:
        layout = _find_layout(display, data, desktop)
        if layout is _AMBIGUOUS:
            unresolved += 1
            continue
        left, top, right, bottom = display.work_area
        try:
            local = _zones_for(layout, data.custom, right - left, bottom - top)
        except LayoutError:
            unresolved += 1
            continue
        if not local:
            continue
        names = zone_names(local, right - left, bottom - top, labels)
        result[display.identity.device] = ZoneSet(
            layout.kind, tuple((l + left, t + top, r + left, b + top) for l, t, r, b in local), tuple(names))
    debug_log(f'FancyZones zones resolved for {len(result)} of {len(displays)} display(s); '
              f'{unresolved} unresolved.', 'windows')
    return result


# --- names ---------------------------------------------------------------------------

def _derived_keys(rectangles: Sequence[Rectangle], width: int, height: int) -> list[tuple[str, ...]]:
    """Positional names that name exactly one zone, as label keys."""
    if len(rectangles) < 2:
        return [() for _ in rectangles]
    tolerance, candidates = _EDGE_TOLERANCE, {}
    for index, (left, top, right, bottom) in enumerate(rectangles):
        x, y = (left + right) / 2 / width, (top + bottom) / 2 / height
        full_height = top <= tolerance * height and bottom >= (1 - tolerance) * height
        full_width = left <= tolerance * width and right >= (1 - tolerance) * width
        west, east = x < 0.5 - tolerance, x > 0.5 + tolerance
        north, south = y < 0.5 - tolerance, y > 0.5 + tolerance
        keys = []
        if full_height and west:
            keys.append('left')
        if full_height and east:
            keys.append('right')
        if full_width and north:
            keys.append('top')
        if full_width and south:
            keys.append('bottom')
        if abs(x - 0.5) <= tolerance and abs(y - 0.5) <= tolerance:
            keys.append('middle')
        for vertical, v_hit in (('top', north), ('bottom', south)):
            for horizontal, h_hit in (('left', west), ('right', east)):
                if v_hit and h_hit:
                    keys.append(f'{vertical}_{horizontal}')
        for key in keys:
            candidates.setdefault(key, []).append(index)
    derived: list[list[str]] = [[] for _ in rectangles]
    for key, owners in candidates.items():
        if len(owners) == 1:
            derived[owners[0]].append(key)
    return [tuple(keys) for keys in derived]


def zone_names(rectangles: Sequence[Rectangle], width: int, height: int,
               labels: Sequence[dict]) -> list[tuple[str, ...]]:
    """Every name a zone answers to: its 1-based number, then derived position labels.

    ``rectangles`` are relative to the work area. ``labels`` maps derived-name keys to words,
    one mapping per locale; keys a locale does not define are simply not offered."""
    names = []
    for index, keys in enumerate(_derived_keys(rectangles, width, height)):
        zone = [str(index + 1)]
        for key in keys:
            for vocabulary in labels:
                word = vocabulary.get(key)
                if isinstance(word, str) and word and word not in zone:
                    zone.append(word)
        names.append(tuple(zone))
    return names


@lru_cache(maxsize=16)
def _locale_labels(language: str) -> dict | None:
    path = _LABEL_DIR / f'{language}.json'
    data = _read_json(path) if path.is_file() else None
    if not isinstance(data, dict):
        return None
    return {key: word for key, word in data.items() if isinstance(key, str) and isinstance(word, str) and word}


def load_zone_labels(languages: Sequence[str]) -> list[dict]:
    """Label vocabularies for the supported languages among ``languages`` (plain lower-case codes)."""
    loaded = []
    for language in languages:
        if isinstance(language, str) and re.fullmatch(r'[a-z]{2,3}', language):
            vocabulary = _locale_labels(language)
            if vocabulary and vocabulary not in loaded:
                loaded.append(vocabulary)
    return loaded
