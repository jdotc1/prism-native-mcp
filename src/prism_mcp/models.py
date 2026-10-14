"""Semantic input schemas; no dataset-specific logic or UI dependencies."""
from __future__ import annotations

import math
import re
from typing import Literal
import uuid
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def safe_text(value: str) -> str:
    if any(c in value for c in ('"', '\r', '\n', '\x00')):
        raise ValueError('Text must be one line and cannot contain double quotes or NUL.')
    if len(value) > 256:
        raise ValueError('Text is limited to 256 characters.')
    return value


class ColumnInput(InputModel):
    label: str = Field(min_length=1, max_length=128)
    values: list[float | None] = Field(min_length=1, max_length=500000)
    study_ids: list[str | None] | None = None

    _label = field_validator('label')(safe_text)

    @field_validator('values', mode='before')
    @classmethod
    def numeric_observations(cls, values):
        if not isinstance(values, list):
            raise ValueError('Observations must be a list.')
        for x in values:
            if x is not None:
                if isinstance(x, bool) or not isinstance(x, (int, float)):
                    raise ValueError('Only finite numeric measurements or null missing values are accepted.')
                try:
                    converted = float(x)
                    if not math.isfinite(converted) or converted != x:
                        raise ValueError('Observation cannot be represented without loss as a finite number.')
                except OverflowError as exc:
                    raise ValueError('Observation is outside the supported numeric range.') from exc
        return values

    @model_validator(mode='after')
    def aligned_ids(self):
        if self.study_ids is not None and len(self.study_ids) != len(self.values):
            raise ValueError('study_ids must align one-to-one with observations.')
        return self


class GraphFormat(InputModel):
    title: str | None = None
    x_title: str | None = None
    y_title: str | None = None
    y_min: float | None = None
    y_max: float | None = None
    y_interval: float | None = Field(default=None, gt=0)
    color_scheme: str | None = None

    @field_validator('title', 'x_title', 'y_title', 'color_scheme')
    @classmethod
    def titles(cls, value):
        return safe_text(value) if value is not None else value

    @model_validator(mode='after')
    def axis_range(self):
        for x in (self.y_min, self.y_max, self.y_interval):
            if x is not None and not math.isfinite(x):
                raise ValueError('Axis values must be finite.')
        if self.y_min is not None and self.y_max is not None and self.y_min >= self.y_max:
            raise ValueError('y_min must be less than y_max.')
        return self


class TableInput(InputModel):
    name: str = Field(min_length=1, max_length=128)
    units: str = Field(default='min', max_length=32)
    columns: list[ColumnInput] = Field(min_length=2, max_length=2)
    graph: GraphFormat = Field(default_factory=GraphFormat)
    measurement_resolution: float | None = Field(default=None, gt=0, description='Original measured resolution in table units. When supplied, permit only rounding below one hundred-millionth of this resolution, with an explicit precision report.')

    _name = field_validator('name')(safe_text)
    _units = field_validator('units')(safe_text)

    @model_validator(mode='after')
    def distinct_groups(self):
        if self.measurement_resolution is not None and not math.isfinite(self.measurement_resolution):
            raise ValueError('Measurement resolution must be finite.')
        if len({c.label for c in self.columns}) != len(self.columns):
            raise ValueError('Group labels must be distinct.')
        if any(sum(v is not None for v in c.values) < 3 for c in self.columns):
            raise ValueError('A verified violin family requires at least three observations per group.')
        return self


class LayoutInput(InputModel):
    title: str = 'Layout'
    rows: int = Field(default=1, ge=1, le=8)
    columns: int = Field(default=2, ge=1, le=8)
    orientation: Literal['portrait', 'landscape'] = 'landscape'
    _title = field_validator('title')(safe_text)


class AnnotationInput(InputModel):
    text: str = Field(min_length=1, max_length=200)
    x_inches: float = Field(ge=0, le=30)
    y_inches: float = Field(ge=0, le=30)
    font_size: float = Field(default=6, ge=4, le=72)
    color: str = '#444444'
    bold: bool = False
    italic: bool = False
    horizontal_anchor: Literal['left', 'center', 'right'] = 'left'
    _text = field_validator('text')(safe_text)

    @field_validator('color')
    @classmethod
    def rgb_color(cls, value):
        if re.fullmatch(r'#[0-9a-fA-F]{6}', value) is None:
            raise ValueError('Text color must be a six-digit #RRGGBB string.')
        return value

    @field_validator('font_size')
    @classmethod
    def finite_font(cls, value):
        if not math.isfinite(value):
            raise ValueError('Text font size must be finite.')
        if not (value * 2).is_integer():
            raise ValueError('Text font size must use half-point increments.')
        return value


class NativeGraphStyle(InputModel):
    """Bounded classic-graph styling; native appearance acceptance remains separate."""

    kind: Literal['violin', 'median_iqr']
    width_inches: float = Field(default=37 / 25.4, ge=.1, le=20)
    height_inches: float = Field(default=62 / 25.4, ge=.1, le=20)
    colors: list[str] = Field(default_factory=lambda: ['#5B2D90', '#4A4A4A'], min_length=2, max_length=2)
    fills: list[str] = Field(default_factory=lambda: ['#E1D9EB', '#E6E6E6'], min_length=2, max_length=2)
    point_opacity: float = Field(default=.5, ge=0, le=1)
    point_size: float = Field(default=2, ge=.1, le=100)
    median_size: float = Field(default=4, ge=.1, le=100)
    axis_font_size: int = Field(default=6, ge=1, le=200)
    title_font_size: int = Field(default=6, ge=1, le=200)
    y_title_font_size: int = Field(default=7, ge=1, le=200)
    y_tick_origin: float | None = None
    show_title: bool = True
    truncate: bool = True
    smoothing: Literal['Light', 'Medium', 'High'] = 'High'

    @field_validator('colors', 'fills')
    @classmethod
    def rgb_colors(cls, values):
        if any(re.fullmatch(r'#[0-9a-fA-F]{6}', value) is None for value in values):
            raise ValueError('Colors must be six-digit #RRGGBB strings.')
        return values

    @field_validator('width_inches', 'height_inches', 'point_opacity', 'point_size', 'median_size', 'y_tick_origin')
    @classmethod
    def finite_style_numbers(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError('Native style values must be finite.')
        return value


class NativeGraphCreation(InputModel):
    title: str = Field(min_length=1, max_length=128)
    kind: Literal['median_iqr'] = 'median_iqr'
    formatting: GraphFormat = Field(default_factory=GraphFormat)
    style: NativeGraphStyle | None = None

    _title = field_validator('title')(safe_text)

    @model_validator(mode='after')
    def matching_style(self):
        if self.style is not None and self.style.kind != self.kind:
            raise ValueError('The style kind must match the requested new graph kind.')
        return self


class NativeGraphPlacement(InputModel):
    x_inches: float = Field(ge=0, le=30)
    y_inches: float = Field(ge=0, le=30)
    scale_percent: int = Field(default=100, ge=1, le=1000)


class NativeOverlayPanel(NativeGraphPlacement):
    """Native graph identities and a visible header used only for UI selection."""

    violin_id: str
    summary_id: str
    selection_label: str = Field(min_length=4, max_length=200)

    @field_validator('violin_id', 'summary_id')
    @classmethod
    def graph_uuid(cls, value):
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError('Overlay graph IDs must be canonical UUID strings.') from exc
        if str(parsed) != value.lower():
            raise ValueError('Overlay graph IDs must be canonical UUID strings.')
        return value

    @field_validator('selection_label')
    @classmethod
    def visible_label(cls, value):
        safe_text(value)
        if len(value.strip()) < 4:
            raise ValueError('A visible graph header must contain at least four non-padding characters.')
        return value


class NativeOverlayComposition(InputModel):
    panels: list[NativeOverlayPanel] = Field(min_length=2, max_length=2)

    @model_validator(mode='after')
    def distinct_graphs_and_headers(self):
        identities = [identity.lower() for panel in self.panels for identity in (panel.violin_id, panel.summary_id)]
        if len(set(identities)) != 4:
            raise ValueError('Overlay composition requires four distinct native graphs.')
        if len({panel.selection_label.strip().casefold() for panel in self.panels}) != 2:
            raise ValueError('Overlay panels require distinct visible selection headers.')
        return self


class NativeLayoutAlignment(InputModel):
    axes: list[Literal['x', 'y']] = Field(default_factory=lambda: ['x', 'y'], min_length=1, max_length=2)

    @field_validator('axes')
    @classmethod
    def distinct_axes(cls, values):
        if len(set(values)) != len(values):
            raise ValueError('Alignment axes must be distinct.')
        return values
