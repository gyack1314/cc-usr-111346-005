"""EDF固定头与逐信号头的ASCII定宽解析与校验。

字段宽度与顺序遵循EDF/EDF+规范：固定头256字节，其后每个信号256字节。
本模块只做头部解析与静态合法性检查，不解释数据记录内容。所有数值字段
按ASCII整数/小数严格解析，不接受前导空格以外的非法字符。
"""
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction

from .errors import EDFHeaderError

FIXED_HEADER_BYTES = 256
SIGNAL_HEADER_BYTES = 256

# (字段名, 起始偏移, 宽度)，按固定头中的字节顺序
_FIXED_FIELDS = [
    ('version', 0, 8),
    ('patient_id', 8, 80),
    ('recording_id', 88, 80),
    ('start_date', 168, 8),
    ('start_time', 176, 8),
    ('header_bytes', 184, 8),
    ('reserved', 192, 44),
    ('num_records', 236, 8),
    ('record_duration', 244, 8),
    ('num_signals', 252, 4),
]

# 逐信号头字段，每个信号内顺序排列
_SIGNAL_FIELDS = [
    ('label', 0, 16),
    ('transducer_type', 16, 80),
    ('physical_dimension', 96, 8),
    ('physical_min', 104, 8),
    ('physical_max', 112, 8),
    ('digital_min', 120, 8),
    ('digital_max', 128, 8),
    ('prefiltering', 136, 80),
    ('samples_per_record', 216, 8),
    ('reserved', 224, 32),
]

ANNOTATION_LABEL = 'EDF Annotations'


def _slice(raw, offset, width):
    return raw[offset:offset + width]


def _ascii_text(raw, offset, width, field_name):
    """头部字段按ASCII解码；非ASCII字节直接判失败。"""
    chunk = _slice(raw, offset, width)
    try:
        return chunk.decode('ascii').strip()
    except UnicodeDecodeError as exc:
        raise EDFHeaderError(f'{field_name}不是合法ASCII定宽字段') from exc


def _ascii_int(raw, offset, width, field_name):
    text = _ascii_text(raw, offset, width, field_name)
    if not text:
        raise EDFHeaderError(f'{field_name}为空')
    try:
        return int(text)
    except ValueError as exc:
        raise EDFHeaderError(f'{field_name}不是整数: {text!r}') from exc


def _ascii_decimal(raw, offset, width, field_name):
    """头部小数字段解析为精确有理数，不经过二进制浮点。"""
    text = _ascii_text(raw, offset, width, field_name)
    if not text:
        raise EDFHeaderError(f'{field_name}为空')
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise EDFHeaderError(f'{field_name}不是合法小数: {text!r}') from exc


@dataclass(frozen=True)
class FixedHeader:
    version: str
    patient_id: str
    recording_id: str
    start_date: str
    start_time: str
    header_bytes: int
    reserved: str
    num_records: int
    record_duration: Fraction
    num_signals: int
    raw_start_datetime: str = ''
    start_datetime: datetime | None = None
    is_plus: bool = False
    variant: str = ''  # 'EDF+C' 或 'EDF+D'

    @property
    def record_count_unknown(self):
        return self.num_records == -1


@dataclass(frozen=True)
class SignalHeader:
    index: int
    label: str
    transducer_type: str
    physical_dimension: str
    physical_min: Fraction
    physical_max: Fraction
    digital_min: int
    digital_max: int
    prefiltering: str
    samples_per_record: int
    reserved: str
    physical_gain: Fraction = field(compare=False)
    physical_offset: Fraction = field(compare=False)
    is_annotation: bool = False

    def to_physical(self, digital):
        """digital值到物理值的精确有理线性映射。

        p = pmin + (d - dmin) * (pmax - pmin) / (dmax - dmin)
        返回Fraction；注释信号不允许调用该映射。
        """
        return self.physical_offset + Fraction(digital) * self.physical_gain


def _parse_start_datetime(date_text, time_text):
    """按EDF固定头原文解析本地 naive datetime。

    原始dd.mm.yy与hh.mm.ss字符串原样保留在raw_start_datetime中；解析结果
    不带时区，绝不使用本机时区推导UTC。年份按EDF惯例处理世纪（85-99为
    19xx，其余20xx）。解析失败返回None并保留原文，不静默伪造时刻。
    """
    raw = f'{date_text} {time_text}'
    try:
        day, month, year2 = (int(part) for part in date_text.split('.'))
        hour, minute, second = (int(part) for part in time_text.split('.'))
        year = 1900 + year2 if year2 >= 85 else 2000 + year2
        return raw, datetime(year, month, day, hour, minute, second)
    except (ValueError, AttributeError):
        return raw, None


def parse_headers(raw):
    """解析固定头与全部逐信号头。

    返回(FixedHeader, tuple[SignalHeader, ...])。头长与信号数冲突、
    极值关系不合法、非EDF+变体等情况直接抛出EDFHeaderError。
    """
    if len(raw) < FIXED_HEADER_BYTES:
        raise EDFHeaderError(
            f'文件短于固定头: {len(raw)} < {FIXED_HEADER_BYTES} 字节')

    values = {
        name: _ascii_text(raw, offset, width, name)
        for name, offset, width in _FIXED_FIELDS
    }
    if values['version'] != '0':
        raise EDFHeaderError(f"EDF版本字段必须为'0'，实际为{values['version']!r}")

    reserved = values['reserved']
    variant = reserved[:5]
    if variant not in ('EDF+C', 'EDF+D'):
        raise EDFHeaderError(
            f"仅支持EDF+C与EDF+D，reserved字段为{reserved!r}")
    is_plus = True

    try:
        header_bytes = int(values['header_bytes'])
        num_records = int(values['num_records'])
        num_signals = int(values['num_signals'])
    except ValueError as exc:
        raise EDFHeaderError('固定头整数字段解析失败') from exc
    record_duration = _ascii_decimal(raw, 244, 8, 'record_duration')

    if num_signals <= 0:
        raise EDFHeaderError(f'信号数必须为正整数，实际为{num_signals}')
    expected_header_bytes = FIXED_HEADER_BYTES + SIGNAL_HEADER_BYTES * num_signals
    if header_bytes != expected_header_bytes:
        raise EDFHeaderError(
            f'头长{header_bytes}与信号数{num_signals}冲突，'
            f'应为{expected_header_bytes}字节')
    if len(raw) < header_bytes:
        raise EDFHeaderError(
            f'文件短于声明头长: {len(raw)} < {header_bytes} 字节')
    if num_records < -1 or num_records == 0:
        raise EDFHeaderError(f'记录数字段不合法: {num_records}')
    if record_duration <= 0:
        raise EDFHeaderError(f'记录时长必须为正，实际为{record_duration}')

    raw_start, start_dt = _parse_start_datetime(
        values['start_date'], values['start_time'])
    fixed = FixedHeader(
        version=values['version'],
        patient_id=values['patient_id'],
        recording_id=values['recording_id'],
        start_date=values['start_date'],
        start_time=values['start_time'],
        header_bytes=header_bytes,
        reserved=reserved,
        num_records=num_records,
        record_duration=record_duration,
        num_signals=num_signals,
        raw_start_datetime=raw_start,
        start_datetime=start_dt,
        is_plus=is_plus,
        variant=variant,
    )

    signals = []
    annotation_count = 0
    # 逐信号头按字段分块排列：先全部label，再全部transducer_type，……
    blocks = {}
    block_offset = FIXED_HEADER_BYTES
    for name, _, width in _SIGNAL_FIELDS:
        blocks[name] = (block_offset, width)
        block_offset += width * num_signals

    def signal_field(name, index):
        offset, width = blocks[name]
        return _ascii_text(raw, offset + index * width, width,
                           f'signal{index}.{name}')

    for index in range(num_signals):
        sv = {name: signal_field(name, index)
              for name, _, _ in _SIGNAL_FIELDS}
        is_annotation = sv['label'] == ANNOTATION_LABEL
        try:
            digital_min = int(sv['digital_min'])
            digital_max = int(sv['digital_max'])
            samples_per_record = int(sv['samples_per_record'])
        except ValueError as exc:
            raise EDFHeaderError(f'信号{index}的整数字段解析失败') from exc

        if is_annotation:
            annotation_count += 1
            # 注释通道的physical min/max按EDF+规范允许留空，
            # 其数据按字节读取，不做物理映射
            if sv['physical_min'] == '' and sv['physical_max'] == '':
                physical_min = Fraction(0)
                physical_max = Fraction(1)
            else:
                try:
                    physical_min = Fraction(sv['physical_min'])
                    physical_max = Fraction(sv['physical_max'])
                except (ValueError, ZeroDivisionError) as exc:
                    raise EDFHeaderError(
                        f'注释信号{index}物理极值解析失败') from exc
        else:
            try:
                physical_min = Fraction(sv['physical_min'])
                physical_max = Fraction(sv['physical_max'])
            except (ValueError, ZeroDivisionError) as exc:
                raise EDFHeaderError(
                    f'信号{index}物理极值解析失败') from exc

        if not digital_min < digital_max:
            raise EDFHeaderError(
                f'信号{index}极值关系不合法: digital_min({digital_min}) '
                f'必须严格小于digital_max({digital_max})')
        if not physical_min < physical_max:
            raise EDFHeaderError(
                f'信号{index}极值关系不合法: physical_min({physical_min}) '
                f'必须严格小于physical_max({physical_max})')
        if samples_per_record <= 0:
            raise EDFHeaderError(
                f'信号{index}每记录样本数必须为正，实际为{samples_per_record}')

        span = Fraction(digital_max - digital_min)
        gain = (physical_max - physical_min) / span
        offset = physical_min - digital_min * gain
        signals.append(SignalHeader(
            index=index,
            label=sv['label'],
            transducer_type=sv['transducer_type'],
            physical_dimension=sv['physical_dimension'],
            physical_min=physical_min,
            physical_max=physical_max,
            digital_min=digital_min,
            digital_max=digital_max,
            prefiltering=sv['prefiltering'],
            samples_per_record=samples_per_record,
            reserved=sv['reserved'],
            physical_gain=gain,
            physical_offset=offset,
            is_annotation=is_annotation,
        ))

    if annotation_count == 0:
        raise EDFHeaderError('EDF+文件必须至少包含一个EDF Annotations信号')

    return fixed, tuple(signals)
