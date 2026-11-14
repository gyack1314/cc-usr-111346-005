"""EDF+读取器测试：结构索引、时间窗、注释、断档与损坏拒绝。

测试文件由tests/edf_builder.py独立拼字节生成，与被测读取器不共享编码
逻辑。另用仓库自带的sources/recording.edf做交叉核对。
"""
from fractions import Fraction
import json
import tempfile
import unittest
from pathlib import Path

from edf_data.edfplus import (
    AnnotationConfig,
    EDFConfigError,
    EDFHeaderError,
    EDFPlusError,
    EDFPlusReader,
    EDFStructureError,
    EDFTALError,
)
from edf_data.edfplus.tal import parse_tals

from tests.edf_builder import EDFPlusBuilder, SignalSpec, TALBuilder

ROOT = Path(__file__).resolve().parent.parent

A, B, ANN = 0, 1, 2
# 通道A: 2样本/记录；通道B: 1样本/记录（两种采样率）
SPECS = [
    SignalSpec('channelA', samples_per_record=2),
    SignalSpec('channelB', samples_per_record=1),
    SignalSpec('EDF Annotations', samples_per_record=64,
               physical_min='-1', physical_max='1',
               physical_dimension=''),
]


def write_temp(content, suffix='.edf'):
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(content)
    handle.close()
    return handle.name


def build_plus_d():
    """两条记录、两种采样率、3-4=1秒断档，含多文本与带duration TAL。"""
    builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
    note = TALBuilder.build(Fraction('0.25'),
                            texts=('note A', 'note B'),
                            duration=Fraction('1.5'))
    builder.add_record(Fraction(0),
                       {A: [10, 20], B: [30]},
                       tals={ANN: [note]})
    note2 = TALBuilder.build(Fraction('3.25'),
                             texts=('note A', 'note B'),
                             duration=Fraction('1.5'))
    builder.add_record(Fraction(3),
                       {A: [40, 50], B: [60]},
                       tals={ANN: [note2]})
    return builder.build()


class EDFPlusDTests(unittest.TestCase):
    def setUp(self):
        self.path = write_temp(build_plus_d())

    def test_record_onsets_gap_and_index_without_waveform_decode(self):
        with EDFPlusReader(self.path) as reader:
            index = reader.index
            self.assertEqual(index.complete_record_count, 2)
            self.assertEqual([rec.onset for rec in index.records],
                             [Fraction(0), Fraction(3)])
            self.assertEqual(len(index.gaps), 1)
            self.assertEqual((index.gaps[0].start, index.gaps[0].end),
                             (Fraction(1), Fraction(3)))
            # 构建索引不登记任何已验证生理通道
            self.assertEqual(reader.validated_range, {})

    def test_two_sample_rates_use_channel_own_spacing(self):
        with EDFPlusReader(self.path) as reader:
            result = reader.acquire(Fraction(0), Fraction(4))
            samples = reader.collect_samples(result.plan)
            channel_a = [s for s in samples if s.channel_index == A]
            channel_b = [s for s in samples if s.channel_index == B]
            # A: 记录时长1秒/2样本=0.5秒间隔；B: 1秒间隔
            self.assertEqual([str(s.time) for s in channel_a],
                             ['0', '1/2', '3', '7/2'])
            self.assertEqual([str(s.time) for s in channel_b],
                             ['0', '3'])
            self.assertEqual([s.digital for s in channel_a], [10, 20, 40, 50])
            self.assertEqual([s.digital for s in channel_b], [30, 60])
            # 每个样本的源字节可独立还原为小端有符号16位
            for sample in samples:
                self.assertEqual(
                    int.from_bytes(sample.source_bytes, 'little',
                                   signed=True),
                    sample.digital)

    def test_exact_physical_mapping_keeps_digital(self):
        specs = [
            SignalSpec('c', samples_per_record=1, digital_min=-10,
                       digital_max=10, physical_min='-1.5',
                       physical_max='1.5'),
            SignalSpec('EDF Annotations', samples_per_record=32,
                       physical_min='-1', physical_max='1',
                       physical_dimension=''),
        ]
        builder = EDFPlusBuilder(variant='EDF+D', signals=specs)
        builder.add_record(Fraction(0), {0: [5]})
        path = write_temp(builder.build())
        with EDFPlusReader(path) as reader:
            result = reader.acquire(0, 1)
            sample = reader.collect_samples(result.plan)[0]
            self.assertEqual(sample.digital, 5)
            # pmin + (d-dmin)*(pmax-pmin)/(dmax-dmin)
            # = -1.5 + 15*3/20 = -1.5 + 2.25 = 0.75 = 3/4
            self.assertEqual(sample.physical, Fraction(3, 4))
            sig = reader.signals[0]
            self.assertEqual(sig.physical_dimension, 'uV')
            self.assertEqual(sig.transducer_type, '')
            self.assertEqual(sig.prefiltering, '')

    def test_half_open_window_and_slice_plan_skip_earlier_samples(self):
        with EDFPlusReader(self.path) as reader:
            plan = reader.plan_window(Fraction(2), Fraction(4))
            # 记录0完全在窗前，计划只选记录1
            self.assertEqual(plan.record_indices, (1,))
            channel_a = next(cp for cp in plan.channels
                             if cp.channel_index == A)
            self.assertEqual(channel_a.slices[0].sample_start, 0)
            self.assertEqual(channel_a.slices[0].sample_end, 2)
            result = reader.acquire(Fraction(2), Fraction(4))
            samples = reader.collect_samples(result.plan)
            self.assertTrue(all(s.record_index == 1 for s in samples))
            # 窗口结束边界3.5为半开：A在3.5的样本被排除
            result2 = reader.acquire(Fraction(3), Fraction('3.5'))
            a_samples = [s for s in reader.collect_samples(result2.plan)
                         if s.channel_index == A]
            self.assertEqual([s.time for s in a_samples], [Fraction(3)])

    def test_gap_window_reports_no_record_region_without_zero_fill(self):
        with EDFPlusReader(self.path) as reader:
            result = reader.acquire(Fraction('0.5'), Fraction('3.5'))
            gap_kinds = [(g.kind, g.start, g.end) for g in result.gaps]
            self.assertIn(('gap', Fraction(1), Fraction(3)), gap_kinds)
            samples = reader.collect_samples(result.plan)
            # 断档处不插零、不拼接：没有任何样本时间落在[1,3)
            self.assertTrue(all(not (Fraction(1) <= s.time < Fraction(3))
                                for s in samples))
            # 窗外无数据段标uncovered：窗口[-1,5)跨首前与尾后两段
            result2 = reader.acquire(Fraction(-1), Fraction(5))
            uncovered = [g for g in result2.gaps if g.kind == 'uncovered']
            self.assertEqual(
                [(g.start, g.end) for g in uncovered],
                [(Fraction(-1), Fraction(0)), (Fraction(4), Fraction(5))])
            self.assertTrue(any(g.kind == 'gap' for g in result2.gaps))

    def test_multi_text_annotation_and_cross_record_duration(self):
        with EDFPlusReader(self.path) as reader:
            result = reader.acquire(Fraction(2), Fraction(3))
            texts_events = [a for a in result.annotations]
            # 0.25起、持续到1.75的事件与[2,3)不相交，应被排除；
            # 3.25起的事件也不与[2,3)相交
            self.assertEqual(texts_events, [])
            result = reader.acquire(Fraction(1), Fraction(3))
            events = result.annotations
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event.texts, ('note A', 'note B'))
            self.assertEqual(event.onset, Fraction('0.25'))
            self.assertEqual(event.duration, Fraction('1.5'))
            # 持续区间不裁成采样点，终点为精确有理数
            self.assertEqual(event.interval_end, Fraction('1.75'))
            self.assertLess(event.tal_byte_start, event.tal_byte_end)
            # timekeeping空文本TAL不导出为研究事件
            self.assertTrue(all(event.texts for event
                                in reader.read_annotations(
                                    reader.plan_window(0, 4))))

    def test_negative_onset(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
        builder.add_record(Fraction(-2), {A: [1, 2], B: [3]})
        builder.add_record(Fraction(1), {A: [4, 5], B: [6]})
        path = write_temp(builder.build())
        with EDFPlusReader(path) as reader:
            self.assertEqual([rec.onset for rec in reader.index.records],
                             [Fraction(-2), Fraction(1)])
            # 窗口[-3,0)只覆盖起点-2的记录：A在-2、-1.5，B在-2
            result = reader.acquire(Fraction(-3), Fraction(0))
            samples = reader.collect_samples(result.plan)
            self.assertEqual(
                sorted({s.time for s in samples}),
                [Fraction(-2), Fraction(-3, 2)])
            # 断档为[-1, 1)，窗外uncovered段[-3,-2)；窗口在0截止，
            # 故窗内空档裁剪为[-1, 0)
            self.assertEqual(
                [(g.kind, g.start, g.end) for g in result.gaps],
                [('uncovered', Fraction(-3), Fraction(-2)),
                 ('gap', Fraction(-1), Fraction(0))])
            # 负样本时间的精确字符串与源字节仍可核对
            self.assertEqual(
                [str(s.time) for s in samples
                 if s.channel_index == A], ['-2', '-3/2'])


class EDFPlusCTests(unittest.TestCase):
    def _build_continuous(self):
        builder = EDFPlusBuilder(variant='EDF+C', signals=SPECS)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        builder.add_record(Fraction(1), {A: [4, 5], B: [6]})
        return builder.build()

    def test_continuous_rule_accepted(self):
        path = write_temp(self._build_continuous())
        with EDFPlusReader(path) as reader:
            self.assertEqual(reader.fixed.variant, 'EDF+C')
            self.assertEqual(reader.index.gaps, ())

    def test_continuity_violation_rejected(self):
        builder = EDFPlusBuilder(variant='EDF+C', signals=SPECS)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        builder.add_record(Fraction(2), {A: [4, 5], B: [6]})
        path = write_temp(builder.build())
        with self.assertRaisesRegex(EDFStructureError, '连续'):
            EDFPlusReader(path)

    def test_first_onset_must_be_zero(self):
        builder = EDFPlusBuilder(variant='EDF+C', signals=SPECS)
        builder.add_record(Fraction(1), {A: [1, 2], B: [3]})
        path = write_temp(builder.build())
        with self.assertRaisesRegex(EDFStructureError, '首记录'):
            EDFPlusReader(path)


class OnsetOrderTests(unittest.TestCase):
    def test_duplicate_and_overlapping_onsets_rejected(self):
        # 记录时长1秒：重复(0)触发非递增，部分重叠(0.5)触发EDF+D重叠规则
        for bad_onset, message in ((Fraction(0), '递增'),
                                   (Fraction('0.5'), '重叠')):
            builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
            builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
            builder.add_record(bad_onset, {A: [4, 5], B: [6]})
            path = write_temp(builder.build())
            with self.assertRaisesRegex(EDFStructureError, message):
                EDFPlusReader(path)


class UnknownRecordCountTests(unittest.TestCase):
    def test_minus_one_resolved_from_full_records_and_eof(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS,
                                 num_records_override=-1)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        builder.add_record(Fraction(3), {A: [4, 5], B: [6]})
        path = write_temp(builder.build())
        with EDFPlusReader(path) as reader:
            self.assertEqual(reader.index.complete_record_count, 2)
            self.assertIsNone(reader.index.incomplete_tail)

    def test_truncated_tail_marked_incomplete(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS,
                                 num_records_override=-1)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        builder.add_record(Fraction(3), {A: [4, 5], B: [6]})
        full = builder.build()
        # 截掉最后一条记录的部分字节，留下不完整尾记录
        truncated = full[:-7]
        path = write_temp(truncated)
        with EDFPlusReader(path) as reader:
            tail = reader.index.incomplete_tail
            self.assertIsNotNone(tail)
            self.assertEqual(tail.expected_bytes, reader.index.record_size)
            self.assertEqual(tail.byte_end, len(truncated))
            self.assertEqual(reader.index.complete_record_count, 1)

    def test_declared_count_conflict_rejected(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS,
                                 num_records_override=3)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        path = write_temp(builder.build())
        with self.assertRaisesRegex(EDFStructureError, '记录数'):
            EDFPlusReader(path)


class CorruptTALTests(unittest.TestCase):
    def test_bad_tal_blocks_full_acquisition(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        content = bytearray(builder.build())
        # 定位第一条记录注释块（A、B之后），破坏timekeeping TAL计时字段
        header = 256 + 256 * len(SPECS)
        ann_offset = header + 2 * (2 + 1)
        content[ann_offset] = ord('X')  # 替换'+'，计时文法非法
        path = write_temp(bytes(content))
        with self.assertRaises(EDFTALError):
            EDFPlusReader(path)

    def test_tal_missing_nul_rejected(self):
        raw = TALBuilder.build(Fraction(0))
        with self.assertRaises(EDFTALError):
            parse_tals(raw[:-1], 0)

    def test_invalid_utf8_rejected(self):
        bad = b'+0\x14\xff\xfe\x14\x00'
        with self.assertRaisesRegex(EDFTALError, 'UTF-8'):
            parse_tals(bad, 0)

    def test_two_timekeeping_tals_rejected(self):
        raw = (TALBuilder.build(Fraction(0))
               + TALBuilder.build(Fraction(1)))
        with self.assertRaisesRegex(EDFTALError, 'timekeeping'):
            from edf_data.edfplus.tal import timekeeping_onset
            timekeeping_onset(parse_tals(raw, 0), 0)


class MultiAnnotationChannelTests(unittest.TestCase):
    def _specs(self):
        return [
            SignalSpec('channelA', samples_per_record=2),
            SignalSpec('EDF Annotations', samples_per_record=48),
            SignalSpec('EDF Annotations', samples_per_record=48),
        ]

    def test_consistent_timekeeping_required_by_default(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=self._specs())
        builder.add_record(Fraction(0), {0: [1, 2]},
                           tals={1: [], 2: []})
        # 手工改写第二条注释通道的timekeeping onset
        content = bytearray(builder.build())
        header = 256 + 256 * 3
        second_ann = header + 2 * 2 + 2 * 48
        content[second_ann:second_ann + 2] = b'+1'
        path = write_temp(bytes(content))
        with self.assertRaisesRegex(EDFTALError, '不一致'):
            EDFPlusReader(path)

    def test_first_channel_policy_records_mismatch(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=self._specs())
        builder.add_record(Fraction(0), {0: [1, 2]})
        content = bytearray(builder.build())
        header = 256 + 256 * 3
        second_ann = header + 2 * 2 + 2 * 48
        content[second_ann:second_ann + 2] = b'+1'
        # +1 后面原本是'.'之类；这里构造的是"+0\x14..."，替换后为"+1\x14..."
        path = write_temp(bytes(content))
        config = AnnotationConfig(timekeeping='first-channel')
        with EDFPlusReader(path, config) as reader:
            mismatches = reader.index.timekeeping_mismatches
            self.assertEqual(len(mismatches), 1)
            self.assertEqual(mismatches[0].actual_onset, Fraction(1))

    def test_unsupported_policy_rejected(self):
        with self.assertRaises(EDFConfigError):
            AnnotationConfig(timekeeping='average')


class HeaderValidationTests(unittest.TestCase):
    def test_invalid_extrema_rejected(self):
        specs = [
            SignalSpec('c', samples_per_record=1, digital_min=10,
                       digital_max=-10),
            SignalSpec('EDF Annotations', samples_per_record=32,
                       physical_min='-1', physical_max='1',
                       physical_dimension=''),
        ]
        builder = EDFPlusBuilder(variant='EDF+D', signals=specs)
        builder.add_record(Fraction(0), {0: [0]})
        path = write_temp(builder.build())
        with self.assertRaisesRegex(EDFHeaderError, '极值'):
            EDFPlusReader(path)

    def test_header_length_signal_count_conflict_rejected(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        content = bytearray(builder.build())
        # 把头部header bytes字段改小：256+256*3=1024 -> 768
        content[184:192] = b'768     '
        path = write_temp(bytes(content))
        with self.assertRaisesRegex(EDFHeaderError, '冲突'):
            EDFPlusReader(path)

    def test_non_edf_plus_rejected(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        content = bytearray(builder.build())
        content[192:197] = b'     '
        path = write_temp(bytes(content))
        with self.assertRaisesRegex(EDFHeaderError, 'EDF\\+'):
            EDFPlusReader(path)

    def test_raw_datetime_preserved_without_timezone_inference(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS,
                                 start_date='09.10.26',
                                 start_time='10.00.00')
        builder.add_record(Fraction(0), {A: [1, 2], B: [3]})
        path = write_temp(builder.build())
        with EDFPlusReader(path) as reader:
            self.assertEqual(reader.fixed.raw_start_datetime,
                             '09.10.26 10.00.00')
            dt = reader.fixed.start_datetime
            self.assertIsNone(dt.tzinfo)
            self.assertEqual((dt.year, dt.month, dt.day), (2026, 10, 9))


class ValidatedRangeTests(unittest.TestCase):
    def test_validated_range_tracks_only_decoded_records(self):
        path = write_temp(build_plus_d())
        with EDFPlusReader(path) as reader:
            plan = reader.plan_window(3, 4)  # 只选记录1
            list(reader.iter_samples(plan))
            snapshot = reader.validated_range
            # 通道0、1只验证了记录1；注释通道不出现
            self.assertEqual(snapshot, {A: ((1, 2),), B: ((1, 2),)})

    def test_digital_out_of_range_rejected_during_decode(self):
        builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
        # 手工构造越界digital值（声明极值[-100,100]，写入101）
        narrow = [
            SignalSpec('channelA', samples_per_record=2,
                       digital_min=-100, digital_max=100),
            SignalSpec('channelB', samples_per_record=1,
                       digital_min=-100, digital_max=100),
            SPECS[2],
        ]
        builder = EDFPlusBuilder(variant='EDF+D', signals=narrow)
        builder.add_record(Fraction(0), {A: [10, 101], B: [30]})
        path = write_temp(builder.build())
        with EDFPlusReader(path) as reader:
            result = reader.acquire(0, 1)
            with self.assertRaisesRegex(EDFStructureError, '越过'):
                    list(reader.iter_samples(result.plan))

    def test_first_channel_rate_not_applied_to_others(self):
        # 首通道1样本/记录、第二通道3样本/记录：切片必须各按各的间隔
        specs = [
            SignalSpec('slow', samples_per_record=1),
            SignalSpec('fast', samples_per_record=3),
            SPECS[2],
        ]
        builder = EDFPlusBuilder(variant='EDF+D', signals=specs)
        builder.add_record(Fraction(0), {0: [7], 1: [1, 2, 3]})
        path = write_temp(builder.build())
        with EDFPlusReader(path) as reader:
            plan = reader.plan_window(0, 1)
            slow = next(cp for cp in plan.channels
                        if cp.channel_index == 0)
            fast = next(cp for cp in plan.channels
                        if cp.channel_index == 1)
            self.assertEqual(slow.slices[0].sample_end, 1)
            self.assertEqual(fast.slices[0].sample_end, 3)
            samples = reader.collect_samples(plan)
            fast_times = [str(s.time) for s in samples
                          if s.channel_index == 1]
            self.assertEqual(fast_times, ['0', '1/3', '2/3'])


class FixtureCrossCheckTests(unittest.TestCase):
    def test_repo_recording_edf_matches_expected_samples(self):
        expected = json.loads(
            (ROOT / 'sources' / 'expected-samples.json').read_text())
        with EDFPlusReader(ROOT / 'sources' / 'recording.edf') as reader:
            self.assertEqual(reader.fixed.variant, 'EDF+D')
            index = reader.index
            self.assertEqual([str(rec.onset) for rec in index.records],
                             ['0', '3'])
            result = reader.acquire(0, 5)
            samples = reader.collect_samples(result.plan)
            for entry in expected:
                for channel_name, values in entry.items():
                    if channel_name in ('record', 'onset'):
                        continue
                    channel_index = next(
                        sig.index for sig in reader.signals
                        if sig.label == channel_name)
                    got = [s.digital for s in samples
                           if s.channel_index == channel_index
                           and s.record_index == entry['record']]
                    self.assertEqual(got, values)
            # 注释通道未被当作波形：切片计划只含两个生理通道
            self.assertEqual(
                {cp.channel_index for cp in result.plan.channels}, {A, B})
            events = result.annotations
            self.assertEqual(len(events), 2)
            self.assertTrue(all(e.texts == ('noteA', 'noteB')
                                for e in events))


if __name__ == '__main__':
    unittest.main()
