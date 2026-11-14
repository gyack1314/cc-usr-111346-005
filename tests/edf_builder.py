"""测试用EDF+文件构造器（仅测试代码使用，不属于采集工具）。

独立于读取器手工拼字节，保证测试不依赖被测代码的编码路径。构造器支持
EDF+C/EDF+D、逐通道不同samples-per-record、每记录真实起点（含负值与
空档）、一个TAL多文本、带duration以及number-of-records=-1。
"""
from dataclasses import dataclass


def _field(value, width):
    text = str(value)
    if len(text) > width:
        raise ValueError(f'字段{text!r}超出{width}字节定宽')
    return text.ljust(width).encode('ascii')


def _decimal_text(value):
    """Fraction/int转EDF+十进制小数字符串（不产生分数形式如3/2）。"""
    from fractions import Fraction
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    # 逐位长除，保证精确十进制文本；除不尽时抛出（EDF+时间本身有限小数）
    numerator = value.numerator
    negative = numerator < 0
    numerator = abs(numerator)
    whole, remainder = divmod(numerator, value.denominator)
    digits = []
    seen = {}
    while remainder:
        if remainder in seen:
            raise ValueError(f'{value}无法表示为有限十进制小数')
        seen[remainder] = len(digits)
        remainder *= 10
        digit, remainder = divmod(remainder, value.denominator)
        digits.append(str(digit))
    text = f'{whole}.{"".join(digits)}' if digits else str(whole)
    return ('-' + text) if negative else text


def _signed_text(value):
    """Fraction/int转EDF+带符号十进制秒字符串。"""
    from fractions import Fraction
    value = Fraction(value)
    sign = '+' if value >= 0 else '-'
    return sign + _decimal_text(abs(value))


@dataclass
class SignalSpec:
    label: str
    samples_per_record: int
    digital_min: int = -32768
    digital_max: int = 32767
    physical_min: str = '-100'
    physical_max: str = '100'
    physical_dimension: str = 'uV'
    transducer_type: str = ''
    prefiltering: str = ''


class TALBuilder:
    """按字节拼TAL: onset[0x15 duration] 0x14 {text 0x14} 0x00。"""

    @staticmethod
    def build(onset, texts=(), duration=None):
        chunk = _signed_text(onset).encode('ascii')
        if duration is not None:
            if duration < 0:
                raise ValueError('TAL duration不允许为负')
            chunk += b'\x15' + _decimal_text(duration).encode('ascii')
        chunk += b'\x14'
        for text in texts:
            chunk += text.encode('utf-8') + b'\x14'
        chunk += b'\x00'
        return chunk


class EDFPlusBuilder:
    def __init__(self, variant='EDF+D', signals=None, record_duration=1,
                 start_date='09.10.26', start_time='10.00.00',
                 num_records_override=None, patient='synthetic',
                 recording='local'):
        if variant not in ('EDF+C', 'EDF+D'):
            raise ValueError('variant必须为EDF+C或EDF+D')
        self.variant = variant
        self.signals = list(signals or [])
        self.record_duration = record_duration
        self.start_date = start_date
        self.start_time = start_time
        self.num_records_override = num_records_override
        self.patient = patient
        self.recording = recording
        self.records = []  # 每项: {'onset': Fraction, 'samples': {idx: [int]}, 'tals': {idx: [bytes]}}

    def add_record(self, onset, samples, tals=None):
        """添加一条记录。tals: {注释通道序号: [TAL字节...]}。

        timekeeping TAL由本方法自动补在各注释通道最前；调用方只需给出
        业务注释TAL（也可显式给全，传auto_keep=False）。
        """
        tals = tals or {}
        record = {'onset': onset, 'samples': dict(samples), 'tals': dict(tals)}
        self.records.append(record)
        return record

    def _annotation_specs(self):
        return [i for i, spec in enumerate(self.signals)
                if spec.label == 'EDF Annotations']

    def _header(self, num_records):
        n = len(self.signals)
        header_bytes = 256 + 256 * n
        parts = [
            _field('0', 8),
            _field(self.patient, 80),
            _field(self.recording, 80),
            _field(self.start_date, 8),
            _field(self.start_time, 8),
            _field(header_bytes, 8),
            _field(self.variant, 44),
            _field(num_records, 8),
            _field(self.record_duration, 8),
            _field(n, 4),
        ]
        out = b''.join(parts)
        assert len(out) == 256

        # 逐信号头按字段分块：每一字段跨所有信号连续排列
        def field_block(attr, width):
            return b''.join(
                _field(getattr(spec, attr, ''), width)
                for spec in self.signals)

        out += field_block('label', 16)
        out += field_block('transducer_type', 80)
        out += field_block('physical_dimension', 8)
        out += field_block('physical_min', 8)
        out += field_block('physical_max', 8)
        out += field_block('digital_min', 8)
        out += field_block('digital_max', 8)
        out += field_block('prefiltering', 80)
        out += field_block('samples_per_record', 8)
        out += field_block('reserved', 32)
        assert len(out) == header_bytes
        return out

    def build(self, append_bytes=b''):
        annotation_indices = self._annotation_specs()
        num_records = (self.num_records_override
                       if self.num_records_override is not None
                       else len(self.records))
        out = bytearray(self._header(num_records))
        for record_index, record in enumerate(self.records):
            for idx, spec in enumerate(self.signals):
                if spec.label == 'EDF Annotations':
                    tals = list(record['tals'].get(idx, []))
                    keeper = TALBuilder.build(record['onset'])
                    payload = keeper + b''.join(tals)
                    capacity = 2 * spec.samples_per_record
                    if len(payload) > capacity:
                        raise ValueError(
                            f'记录{record_index}注释通道{idx}的TAL超出容量')
                    out += payload + b'\x00' * (capacity - len(payload))
                else:
                    values = record['samples'].get(idx, [])
                    if len(values) != spec.samples_per_record:
                        raise ValueError(
                            f'记录{record_index}通道{idx}样本数'
                            f'{len(values)} != {spec.samples_per_record}')
                    for value in values:
                        out += int(value).to_bytes(2, 'little', signed=True)
        out += append_bytes
        return bytes(out)

    def record_size(self):
        return 2 * sum(spec.samples_per_record for spec in self.signals)
