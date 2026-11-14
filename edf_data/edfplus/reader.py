"""EDF+采集读取器：结构索引、通道切片计划与有界样本流。

设计上明确区分两类信息：

1. 结构索引（StructuralIndex）：只读取头部与各记录注释通道的TAL，不解码
   任何生理波形样本。它给出每条记录的文件字节偏移、真实记录起点、记录
   间空档与尾记录截断情况，并保存带字节范围的TAL。
2. 样本已验证范围（ValidatedRange）：只有实际解码并通过digital极值检查
   的通道-记录区间才会被登记，按半开记录区间合并，独立于结构索引声明。

时间窗采集先由结构索引生成纯数据的通道切片计划（WindowPlan），样本流
按计划seek到目标字节，不解码时间窗之前的生理样本。所有时间量使用
Fraction精确有理数，半开端点[start, stop)规则固定。
"""
from dataclasses import dataclass, field
from fractions import Fraction
import os
import struct

from .errors import (
    EDFConfigError,
    EDFHeaderError,
    EDFStructureError,
    EDFTALError,
)
from .header import parse_headers
from .tal import parse_tals, timekeeping_onset

SUPPORTED_TIMEKEEPING_POLICIES = ('consistent', 'first-channel')


def _ceil_div(numerator, denominator):
    """有理数整数上界：ceil(numerator / denominator)，denominator为正。"""
    return -((-numerator) // denominator)


def _as_time(value, name):
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise EDFConfigError(f'{name}不是合法十进制秒: {value!r}') from exc
    raise EDFConfigError(f'{name}必须为秒数字符串、int或Fraction')


@dataclass(frozen=True)
class AnnotationConfig:
    """多注释通道timekeeping一致性的支持配置。

    - consistent（默认）：每条记录内所有注释通道的timekeeping onset必须
      完全一致，否则采集失败；
    - first-channel：以最低序号注释通道为准，其余通道的不一致记入结构
      索引的timekeeping_mismatches，不静默丢弃也不重排文件顺序。
    """

    timekeeping: str = 'consistent'

    def __post_init__(self):
        if self.timekeeping not in SUPPORTED_TIMEKEEPING_POLICIES:
            raise EDFConfigError(
                f'不支持的timekeeping策略: {self.timekeeping!r}，'
                f'支持值: {SUPPORTED_TIMEKEEPING_POLICIES}')


@dataclass(frozen=True)
class AnnotationBlock:
    channel_index: int
    byte_start: int
    byte_end: int
    tals: tuple  # tuple[TAL]


@dataclass(frozen=True)
class RecordIndex:
    index: int
    file_offset: int
    onset: Fraction
    annotation_blocks: tuple[AnnotationBlock, ...]


@dataclass(frozen=True)
class Gap:
    """无记录区间。kind='gap'为记录间断档，'uncovered'为窗外无数据段。"""

    kind: str
    start: Fraction
    end: Fraction
    preceding_record: int | None
    next_record: int | None


@dataclass(frozen=True)
class IncompleteTail:
    byte_start: int
    byte_end: int
    expected_bytes: int


@dataclass(frozen=True)
class TimekeepingMismatch:
    record_index: int
    channel_index: int
    expected_onset: Fraction
    actual_onset: Fraction


@dataclass(frozen=True)
class StructuralIndex:
    record_size: int
    records: tuple[RecordIndex, ...]
    gaps: tuple[Gap, ...]
    incomplete_tail: IncompleteTail | None
    timekeeping_mismatches: tuple[TimekeepingMismatch, ...]
    annotation_channels: tuple[int, ...]

    @property
    def complete_record_count(self):
        return len(self.records)


class ValidatedRange:
    """已实际解码并通过校验的通道-记录区间（半开，按记录号合并）。"""

    def __init__(self):
        self._intervals = {}

    def mark(self, channel_index, record_index):
        intervals = self._intervals.setdefault(channel_index, [])
        start, end = record_index, record_index + 1
        merged = []
        placed = False
        for lo, hi in intervals:
            if hi < start:
                merged.append((lo, hi))
            elif lo > end:
                if not placed:
                    merged.append((start, end))
                    placed = True
                merged.append((lo, hi))
            else:
                start = min(start, lo)
                end = max(end, hi)
        if not placed:
            merged.append((start, end))
        self._intervals[channel_index] = merged

    def snapshot(self):
        return {
            channel: tuple(intervals)
            for channel, intervals in sorted(self._intervals.items())
        }


@dataclass(frozen=True)
class RecordSlice:
    record_index: int
    sample_start: int  # 含
    sample_end: int    # 不含

    @property
    def is_empty(self):
        return self.sample_start >= self.sample_end


@dataclass(frozen=True)
class ChannelPlan:
    channel_index: int
    slices: tuple[RecordSlice, ...]


@dataclass(frozen=True)
class WindowPlan:
    start: Fraction
    stop: Fraction
    record_indices: tuple[int, ...]
    channels: tuple[ChannelPlan, ...]


@dataclass(frozen=True)
class Sample:
    channel_index: int
    record_index: int
    sample_index: int
    time: Fraction
    digital: int
    physical: Fraction
    source_bytes: bytes


@dataclass(frozen=True)
class AnnotationEvent:
    record_index: int
    channel_index: int
    onset: Fraction
    duration: Fraction | None
    texts: tuple[str, ...]
    tal_byte_start: int
    tal_byte_end: int

    @property
    def interval_end(self):
        return self.onset + (self.duration if self.duration is not None
                             else Fraction(0))


@dataclass(frozen=True)
class WindowResult:
    plan: WindowPlan
    gaps: tuple[Gap, ...]
    annotations: tuple[AnnotationEvent, ...]
    incomplete_tail: IncompleteTail | None


class EDFPlusReader:
    """EDF+C/EDF+D文件读取器。

    构造时完成头部解析与结构索引（读取注释通道，但不解码生理样本）。
    样本通过iter_samples按WindowPlan有界流式产出，并同步登记已验证范围。
    """

    def __init__(self, source, annotation_config=None):
        self._config = annotation_config or AnnotationConfig()
        if not isinstance(self._config, AnnotationConfig):
            raise EDFConfigError('annotation_config必须为AnnotationConfig')
        self._owns_stream = False
        if hasattr(source, 'read'):
            self._stream = source
        else:
            self._stream = open(source, 'rb')
            self._owns_stream = True
        try:
            self._build()
        except BaseException:
            if self._owns_stream:
                self._stream.close()
            raise
        self._validated = ValidatedRange()

    # ---- 构造与结构索引 ------------------------------------------------

    def _build(self):
        self._stream.seek(0, os.SEEK_END)
        file_size = self._stream.tell()
        self._stream.seek(0)
        header_raw = self._stream.read(256)
        if len(header_raw) < 256:
            raise EDFHeaderError('文件短于256字节固定头')
        # 先粗读信号数以确定完整头长，再一次性读取整个头部
        try:
            num_signals = int(header_raw[252:256].decode('ascii'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EDFHeaderError('信号数字段不可解析') from exc
        if num_signals <= 0:
            raise EDFHeaderError(f'信号数必须为正整数，实际为{num_signals}')
        header_bytes = 256 + 256 * num_signals
        header_raw += self._stream.read(header_bytes - len(header_raw))
        self.fixed, self.signals = parse_headers(header_raw)
        if len(header_raw) != header_bytes:
            raise EDFHeaderError(
                f'文件短于声明头长: {len(header_raw)} < {header_bytes}')

        self._annotation_channels = tuple(
            sig.index for sig in self.signals if sig.is_annotation)
        self._record_size = 2 * sum(sig.samples_per_record
                                    for sig in self.signals)
        data_size = file_size - header_bytes
        if data_size < 0:
            raise EDFStructureError('文件短于声明头长')

        complete = data_size // self._record_size
        remainder = data_size % self._record_size
        declared = self.fixed.num_records
        self._incomplete_tail = None
        if declared != -1:
            if remainder or complete != declared:
                raise EDFStructureError(
                    f'数据记录数与头部声明不符: 声明{declared}，'
                    f'按字节可得{complete}条完整记录，余{remainder}字节')
            record_count = declared
        else:
            # number-of-records为-1：只能由完整数据记录与真实EOF确定数目
            record_count = complete
            if record_count == 0:
                raise EDFStructureError(
                    'number-of-records为-1但文件不含任何完整数据记录')
            if remainder:
                self._incomplete_tail = IncompleteTail(
                    byte_start=header_bytes + record_count * self._record_size,
                    byte_end=file_size,
                    expected_bytes=self._record_size,
                )

        self._channel_block_offsets = []
        running = 0
        for sig in self.signals:
            self._channel_block_offsets.append(running)
            running += 2 * sig.samples_per_record

        records = []
        mismatches = []
        previous_onset = None
        previous_end = None
        for record_index in range(record_count):
            record_offset = header_bytes + record_index * self._record_size
            blocks = []
            onsets = []
            for channel_index in self._annotation_channels:
                block_start = (record_offset
                               + self._channel_block_offsets[channel_index])
                block_len = 2 * self.signals[channel_index].samples_per_record
                self._stream.seek(block_start)
                block = self._stream.read(block_len)
                if len(block) != block_len:
                    raise EDFStructureError(
                        f'记录{record_index}注释通道{channel_index}读取不完整')
                tals = parse_tals(block, block_start)
                onset = timekeeping_onset(tals, record_index)
                onsets.append(onset)
                blocks.append(AnnotationBlock(
                    channel_index=channel_index,
                    byte_start=block_start,
                    byte_end=block_start + block_len,
                    tals=tals,
                ))
            authoritative = onsets[0]
            if self._config.timekeeping == 'consistent':
                if any(onset != authoritative for onset in onsets[1:]):
                    raise EDFTALError(
                        f'记录{record_index}多个注释通道的timekeeping '
                        f'onset不一致: {[str(o) for o in onsets]}')
            else:
                for channel_index, onset in zip(
                        self._annotation_channels[1:], onsets[1:]):
                    if onset != authoritative:
                        mismatches.append(TimekeepingMismatch(
                            record_index=record_index,
                            channel_index=channel_index,
                            expected_onset=authoritative,
                            actual_onset=onset,
                        ))

            if previous_onset is not None and onset <= previous_onset:
                # 重复或重叠的记录起点不静默按文件顺序修正
                raise EDFStructureError(
                    f'记录{record_index}起点{onset}未严格递增，'
                    f'前一记录起点为{previous_onset}')
            if self.fixed.variant == 'EDF+C':
                expected_first = Fraction(0)
                if record_index == 0 and onset != expected_first:
                    raise EDFStructureError(
                        f'EDF+C首记录起点必须为0，实际为{onset}')
                if previous_end is not None and onset != previous_end:
                    raise EDFStructureError(
                        'EDF+C必须连续: '
                        f'记录{record_index}起点{onset}不等于前一记录'
                        f'终点{previous_end}')
            else:
                if previous_end is not None and onset < previous_end:
                    raise EDFStructureError(
                        f'EDF+D记录{record_index}起点{onset}早于前一记录'
                        f'终点{previous_end}，记录区间重叠')

            records.append(RecordIndex(
                index=record_index,
                file_offset=record_offset,
                onset=onset,
                annotation_blocks=tuple(blocks),
            ))
            previous_onset = onset
            previous_end = onset + self.fixed.record_duration

        gaps = []
        for earlier, later in zip(records, records[1:]):
            gap_start = earlier.onset + self.fixed.record_duration
            gap_end = later.onset
            if gap_start < gap_end:
                gaps.append(Gap(
                    kind='gap',
                    start=gap_start,
                    end=gap_end,
                    preceding_record=earlier.index,
                    next_record=later.index,
                ))

        self._index = StructuralIndex(
            record_size=self._record_size,
            records=tuple(records),
            gaps=tuple(gaps),
            incomplete_tail=self._incomplete_tail,
            timekeeping_mismatches=tuple(mismatches),
            annotation_channels=self._annotation_channels,
        )

    # ---- 公共属性 ------------------------------------------------------

    @property
    def index(self):
        return self._index

    @property
    def validated_range(self):
        return self._validated.snapshot()

    def close(self):
        if self._owns_stream:
            self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ---- 时间窗切片计划 ------------------------------------------------

    def plan_window(self, start, stop):
        """按记录起点与各通道自身样本间隔生成切片计划（不读取样本字节）。"""
        start = _as_time(start, 'start')
        stop = _as_time(stop, 'stop')
        if start > stop:
            raise EDFConfigError(f'时间窗起点{start}晚于终点{stop}')
        rd = self.fixed.record_duration
        selected = []
        channel_plans = []
        waveform = [sig for sig in self.signals if not sig.is_annotation]
        for record in self._index.records:
            rec_start, rec_end = record.onset, record.onset + rd
            if not (rec_start < stop and rec_end > start):
                continue
            selected.append(record.index)
        for sig in waveform:
            slices = []
            spr = sig.samples_per_record
            for record_index in selected:
                record = self._index.records[record_index]
                low = _ceil_div((start - record.onset) * spr, rd)
                high = _ceil_div((stop - record.onset) * spr, rd)
                sample_start = max(0, low)
                sample_end = min(spr, high)
                if sample_start < sample_end:
                    slices.append(RecordSlice(record_index, sample_start,
                                              sample_end))
            channel_plans.append(ChannelPlan(sig.index, tuple(slices)))
        return WindowPlan(
            start=start,
            stop=stop,
            record_indices=tuple(selected),
            channels=tuple(channel_plans),
        )

    def _gaps_for_window(self, plan):
        result = []
        start, stop = plan.start, plan.stop
        first = self._index.records[0]
        last = self._index.records[-1]
        rd = self.fixed.record_duration
        if start < first.onset:
            result.append(Gap(
                kind='uncovered',
                start=start,
                end=min(stop, first.onset),
                preceding_record=None,
                next_record=first.index,
            ))
        for gap in self._index.gaps:
            lo = max(gap.start, start)
            hi = min(gap.end, stop)
            if lo < hi:
                result.append(Gap(kind='gap', start=lo, end=hi,
                                  preceding_record=gap.preceding_record,
                                  next_record=gap.next_record))
        last_end = last.onset + rd
        if stop > last_end:
            result.append(Gap(
                kind='uncovered',
                start=max(start, last_end),
                end=stop,
                preceding_record=last.index,
                next_record=None,
            ))
        return tuple(result)

    # ---- 有界样本流 ----------------------------------------------------

    def _sample_byte_offset(self, record, channel_index, sample_index):
        sig = self.signals[channel_index]
        block_offset = self._channel_block_offsets[channel_index]
        return (record.file_offset + block_offset
                + 2 * sample_index), sig

    def iter_samples(self, plan):
        """按计划逐样本产出，仅seek到计划内字节，不触碰窗前生理样本。"""
        for record_index in plan.record_indices:
            record = self._index.records[record_index]
            for channel_plan in plan.channels:
                sig = self.signals[channel_plan.channel_index]
                target = next((s for s in channel_plan.slices
                               if s.record_index == record_index), None)
                if target is None or target.is_empty:
                    continue
                offset, _ = self._sample_byte_offset(
                    record, sig.index, target.sample_start)
                count = target.sample_end - target.sample_start
                self._stream.seek(offset)
                raw = self._stream.read(2 * count)
                if len(raw) != 2 * count:
                    raise EDFStructureError(
                        f'记录{record_index}通道{sig.index}样本读取不完整')
                interval = Fraction(self.fixed.record_duration,
                                    sig.samples_per_record)
                for order in range(count):
                    sample_index = target.sample_start + order
                    word = raw[2 * order:2 * order + 2]
                    digital = int.from_bytes(word, 'little', signed=True)
                    if not sig.digital_min <= digital <= sig.digital_max:
                        raise EDFStructureError(
                            f'记录{record_index}通道{sig.index}样本'
                            f'{sample_index}的digital值{digital}越过'
                            f'[{sig.digital_min},{sig.digital_max}]')
                    physical = sig.to_physical(digital)
                    time = record.onset + interval * sample_index
                    yield Sample(
                        channel_index=sig.index,
                        record_index=record_index,
                        sample_index=sample_index,
                        time=time,
                        digital=digital,
                        physical=physical,
                        source_bytes=word,
                    )
                # 整块通道数据已成功解码并通过极值检查，登记已验证范围
                self._validated.mark(sig.index, record_index)

    def collect_samples(self, plan):
        return list(self.iter_samples(plan))

    # ---- 注释 ----------------------------------------------------------

    def read_annotations(self, plan):
        """读取与时间窗半开相交的注释事件（timekeeping空文本不导出）。

        注释可跨记录时长：只按[onset, onset+duration)与窗口的时间相交
        判断，持续区间不裁成采样点，原始TAL字节范围保留。
        """
        events = []
        start, stop = plan.start, plan.stop
        for record in self._index.records:
            for block in record.annotation_blocks:
                for tal in block.tals:
                    if tal.is_timekeeping:
                        continue
                    if tal.onset < stop and tal.interval_end > start:
                        events.append(AnnotationEvent(
                            record_index=record.index,
                            channel_index=block.channel_index,
                            onset=tal.onset,
                            duration=tal.duration,
                            texts=tal.texts,
                            tal_byte_start=tal.byte_start,
                            tal_byte_end=tal.byte_end,
                        ))
        return tuple(events)

    def acquire(self, start, stop):
        """一次给出切片计划、空档与注释；样本仍由iter_samples有界流式取。"""
        plan = self.plan_window(start, stop)
        return WindowResult(
            plan=plan,
            gaps=self._gaps_for_window(plan),
            annotations=self.read_annotations(plan),
            incomplete_tail=self._incomplete_tail,
        )
