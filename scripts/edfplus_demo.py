"""端到端核对演示（独立脚本，非测试套件）。

在独立空目录中生成并核对：
1. EDF+D：两种采样率 + 一个断档，核对时间窗与断档；
2. 同一文件的多文本TAL、跨记录duration与负起点；
3. number-of-records=-1（真实EOF定数目）与尾记录截断标记；
4. 损坏TAL如何阻止该文件的完整采集结论。

运行（从仓库根目录）：
    python -I scripts/edfplus_demo.py
脚本目录不放任何可导入模块，所有代码路径显式加入sys.path。
"""
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from edf_data.edfplus import (  # noqa: E402
    AnnotationConfig,
    EDFPlusReader,
    EDFTALError,
)
from tests.edf_builder import (  # noqa: E402
    EDFPlusBuilder,
    SignalSpec,
    TALBuilder,
)

SPECS = [
    SignalSpec('channelA', samples_per_record=2),
    SignalSpec('channelB', samples_per_record=1),
    SignalSpec('EDF Annotations', samples_per_record=96,
               physical_min='-1', physical_max='1',
               physical_dimension=''),
]


def section(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


def main():
    workdir = Path(tempfile.mkdtemp(prefix='edfplus-demo-'))
    print(f'合成文件目录: {workdir}')

    # ------------------------------------------------------------------
    section('1. EDF+D：两种采样率 + 断档；时间窗半开、不跨断点拼接')
    # ------------------------------------------------------------------
    builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
    note0 = TALBuilder.build(Fraction('0.25'),
                             texts=('noteA', 'noteB'),
                             duration=Fraction('1.5'))
    builder.add_record(Fraction(0), {0: [10, 20], 1: [30]},
                       tals={2: [note0]})
    builder.add_record(Fraction(3), {0: [40, 50], 1: [60]})
    path = workdir / 'two-rates-gap.edf'
    path.write_bytes(builder.build())

    with EDFPlusReader(path) as reader:
        index = reader.index
        print(f'变体: {reader.fixed.variant}')
        print('记录起点:', [str(r.onset) for r in index.records])
        print('断档:', [(str(g.start), str(g.end)) for g in index.gaps])
        print('构建索引后已验证范围（应不含生理通道）:', reader.validated_range)

        result = reader.acquire(Fraction('0.5'), Fraction('3.5'))
        plan = result.plan
        print('窗口[0.5, 3.5)选中记录:', plan.record_indices)
        samples = reader.collect_samples(plan)
        print('样本（通道,记录,样本号,时间,digital,源字节）:')
        for s in samples:
            print(f'  ch{s.channel_index} rec{s.record_index} '
                  f'#{s.sample_index} t={s.time} d={s.digital} '
                  f'bytes={s.source_bytes.hex()}')
        print('窗内空档（不插零、不拼接）:',
              [(g.kind, str(g.start), str(g.end)) for g in result.gaps])
        sample_times = [s.time for s in samples]
        assert all(not (Fraction(1) <= t < Fraction(3))
                   for t in sample_times), '断档内出现样本'
        # 通道A间隔0.5秒、通道B间隔1秒，各自独立；
        # 3.5恰为窗口半开上界，A在3.5的样本被排除
        a_times = [str(s.time) for s in samples if s.channel_index == 0]
        b_times = [str(s.time) for s in samples if s.channel_index == 1]
        assert a_times == ['1/2', '3'], a_times
        assert b_times == ['3'], b_times
        print('采样率独立核对通过: A间隔1/2秒, B间隔1秒; '
              '半开上界3.5排除边界样本')
        print('样本已验证范围:', reader.validated_range)

    # ------------------------------------------------------------------
    section('2. 多文本TAL、跨记录duration、负起点')
    # ------------------------------------------------------------------
    neg_builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS)
    neg_builder.add_record(Fraction(-2), {0: [1, 2], 1: [3]})
    long_note = TALBuilder.build(Fraction('-1.75'),
                                 texts=('event-x', 'event-y'),
                                 duration=Fraction('4'))
    neg_builder.add_record(Fraction(1), {0: [4, 5], 1: [6]},
                           tals={2: [long_note]})
    neg_path = workdir / 'negative-onset.edf'
    neg_path.write_bytes(neg_builder.build())

    with EDFPlusReader(neg_path) as reader:
        print('记录起点:', [str(r.onset) for r in reader.index.records])
        result = reader.acquire(Fraction(-3), Fraction(3))
        for event in result.annotations:
            print(f'注释: onset={event.onset} duration={event.duration} '
                  f'区间=[{event.onset},{event.interval_end}) '
                  f'文本={event.texts} TAL字节={event.tal_byte_start}'
                  f'-{event.tal_byte_end}')
        assert result.annotations[0].texts == ('event-x', 'event-y')
        # 持续区间[-1.75, 2.25)跨记录且不裁成采样点
        assert result.annotations[0].interval_end == Fraction('9/4')
        # timekeeping空文本不导出
        assert all(event.texts for event in result.annotations)
        print('多文本与跨记录duration核对通过；空文本timekeeping未导出')

    # ------------------------------------------------------------------
    section('3. number-of-records=-1：EOF定数目；尾记录截断标记')
    # ------------------------------------------------------------------
    unk_builder = EDFPlusBuilder(variant='EDF+D', signals=SPECS,
                                 num_records_override=-1)
    unk_builder.add_record(Fraction(0), {0: [1, 2], 1: [3]})
    unk_builder.add_record(Fraction(2), {0: [4, 5], 1: [6]})
    full = unk_builder.build()
    unk_path = workdir / 'unknown-records.edf'
    unk_path.write_bytes(full)
    trunc_path = workdir / 'unknown-records-truncated.edf'
    trunc_path.write_bytes(full[:-9])

    with EDFPlusReader(unk_path) as reader:
        assert reader.fixed.num_records == -1
        print(f'-1文件按完整记录+真实EOF确定数目: '
              f'{reader.index.complete_record_count}，截断尾: '
              f'{reader.index.incomplete_tail}')
        assert reader.index.complete_record_count == 2
        assert reader.index.incomplete_tail is None
    with EDFPlusReader(trunc_path) as reader:
        tail = reader.index.incomplete_tail
        print(f'截断文件: 完整记录{reader.index.complete_record_count}条, '
              f'不完整尾 {tail.byte_start}-{tail.byte_end} '
              f'(期望{tail.expected_bytes}字节)')
        assert reader.index.complete_record_count == 1
        assert tail is not None and tail.byte_end == len(full) - 9
    print('未知记录数与截断标记核对通过')

    # ------------------------------------------------------------------
    section('4. 损坏TAL阻止完整采集结论（失败闭合）')
    # ------------------------------------------------------------------
    corrupt = bytearray(builder.build())
    header_bytes = 256 + 256 * len(SPECS)
    ann_block = header_bytes + 2 * (2 + 1)
    corrupt[ann_block] = ord('X')  # 破坏timekeeping TAL的'+'
    corrupt_path = workdir / 'corrupt-tal.edf'
    corrupt_path.write_bytes(bytes(corrupt))
    print(f'已破坏字节偏移{ann_block}（timekeeping TAL计时字段）')
    try:
        with EDFPlusReader(corrupt_path) as reader:
            reader.acquire(0, 4)
        raise AssertionError('损坏TAL必须阻止采集，但读取器正常返回')
    except EDFTALError as exc:
        print(f'读取器拒绝完整采集结论: {type(exc).__name__}: {exc}')
        print('=> 结构索引未建立、无样本流、无已验证范围，调用方必须拒收')

    # 多注释通道一致性策略示例
    section('附. 多注释通道timekeeping策略')
    dual = [
        SignalSpec('channelA', samples_per_record=2),
        SignalSpec('EDF Annotations', samples_per_record=64,
                   physical_min='-1', physical_max='1',
                   physical_dimension=''),
        SignalSpec('EDF Annotations', samples_per_record=64,
                   physical_min='-1', physical_max='1',
                   physical_dimension=''),
    ]
    db = EDFPlusBuilder(variant='EDF+D', signals=dual)
    db.add_record(Fraction(0), {0: [1, 2]})
    good = workdir / 'dual-annotations.edf'
    good.write_bytes(db.build())
    with EDFPlusReader(good, AnnotationConfig('consistent')) as reader:
        print('consistent策略下两注释通道起点一致，采集允许: '
              f'{reader.index.annotation_channels}')
    print('\n全部核对完成。')


if __name__ == '__main__':
    main()
