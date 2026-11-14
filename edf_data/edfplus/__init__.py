"""EDF+（EDF+C/EDF+D）采集读取。

对外暴露头部、TAL与读取器的主要类型。读取失败闭合：结构、极值、TAL、
连续性等不合法情况抛出EDFPlusError子类，不产出部分采集结论。
"""
from .errors import (
    EDFConfigError,
    EDFHeaderError,
    EDFPlusError,
    EDFStructureError,
    EDFTALError,
)
from .header import FixedHeader, SignalHeader, parse_headers
from .tal import TAL, parse_tals
from .reader import (
    AnnotationConfig,
    AnnotationEvent,
    ChannelPlan,
    EDFPlusReader,
    Gap,
    IncompleteTail,
    RecordIndex,
    RecordSlice,
    Sample,
    StructuralIndex,
    WindowPlan,
    WindowResult,
)

__all__ = [
    'AnnotationConfig',
    'AnnotationEvent',
    'ChannelPlan',
    'EDFConfigError',
    'EDFHeaderError',
    'EDFPlusError',
    'EDFStructureError',
    'EDFTALError',
    'EDFPlusReader',
    'FixedHeader',
    'Gap',
    'IncompleteTail',
    'RecordIndex',
    'RecordSlice',
    'Sample',
    'SignalHeader',
    'StructuralIndex',
    'TAL',
    'WindowPlan',
    'WindowResult',
    'parse_headers',
    'parse_tals',
]
