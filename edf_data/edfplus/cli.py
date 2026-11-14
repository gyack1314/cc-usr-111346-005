"""EDF+文件的结构检查与时间窗采集命令行入口。

inspect 只输出结构索引（记录起点、空档、截断尾、注释通道），不解码生理
样本；acquire 按半开时间窗[start, stop)产出有界样本流、空档与注释。
所有精确有理数以十进制分数字符串输出，源字节以hex输出，原始日期时间
原样保留，不做本机时区换算。
"""
import argparse
import json
from fractions import Fraction

from .reader import AnnotationConfig, EDFPlusReader
from .errors import EDFPlusError


def _rational(value):
    if value is None:
        return None
    if isinstance(value, Fraction):
        return str(value)
    return value


def _signal_json(sig):
    return {
        'index': sig.index,
        'label': sig.label,
        'transducer_type': sig.transducer_type,
        'physical_dimension': sig.physical_dimension,
        'physical_min': _rational(sig.physical_min),
        'physical_max': _rational(sig.physical_max),
        'digital_min': sig.digital_min,
        'digital_max': sig.digital_max,
        'prefiltering': sig.prefiltering,
        'samples_per_record': sig.samples_per_record,
        'is_annotation': sig.is_annotation,
    }


def _gap_json(gap):
    return {
        'kind': gap.kind,
        'start': _rational(gap.start),
        'end': _rational(gap.end),
        'preceding_record': gap.preceding_record,
        'next_record': gap.next_record,
    }


def inspect_json(reader):
    index = reader.index
    return {
        'variant': reader.fixed.variant,
        'raw_start_datetime': reader.fixed.raw_start_datetime,
        'start_datetime_naive': (
            reader.fixed.start_datetime.isoformat()
            if reader.fixed.start_datetime else None),
        'num_records_header': reader.fixed.num_records,
        'record_duration': _rational(reader.fixed.record_duration),
        'record_size_bytes': index.record_size,
        'complete_record_count': index.complete_record_count,
        'annotation_channels': list(index.annotation_channels),
        'signals': [_signal_json(sig) for sig in reader.signals],
        'record_onsets': [
            {'record': rec.index,
             'onset': _rational(rec.onset),
             'file_offset': rec.file_offset}
            for rec in index.records],
        'gaps': [_gap_json(gap) for gap in index.gaps],
        'timekeeping_mismatches': [
            {'record': m.record_index,
             'channel': m.channel_index,
             'expected_onset': _rational(m.expected_onset),
             'actual_onset': _rational(m.actual_onset)}
            for m in index.timekeeping_mismatches],
        'incomplete_tail': (
            None if index.incomplete_tail is None
            else {'byte_start': index.incomplete_tail.byte_start,
                  'byte_end': index.incomplete_tail.byte_end,
                  'expected_bytes': index.incomplete_tail.expected_bytes}),
    }


def _sample_json(sample):
    return {
        'channel_index': sample.channel_index,
        'record_index': sample.record_index,
        'sample_index': sample.sample_index,
        'time': _rational(sample.time),
        'digital': sample.digital,
        'physical': _rational(sample.physical),
        'source_bytes_hex': sample.source_bytes.hex(),
    }


def _annotation_json(event):
    return {
        'record_index': event.record_index,
        'channel_index': event.channel_index,
        'onset': _rational(event.onset),
        'duration': _rational(event.duration),
        'interval_end': _rational(event.interval_end),
        'texts': list(event.texts),
        'tal_byte_range': [event.tal_byte_start, event.tal_byte_end],
    }


def acquire_json(reader, start, stop, max_samples):
    result = reader.acquire(start, stop)
    plan = result.plan
    samples = []
    truncated = False
    for sample in reader.iter_samples(plan):
        if max_samples is not None and len(samples) >= max_samples:
            truncated = True
            break
        samples.append(_sample_json(sample))
    return {
        'window': {'start': _rational(plan.start),
                   'stop': _rational(plan.stop),
                   'endpoint_rule': 'half-open [start, stop)'},
        'selected_records': list(plan.record_indices),
        'channel_slices': [
            {'channel_index': cp.channel_index,
             'slices': [
                 {'record_index': s.record_index,
                  'sample_start': s.sample_start,
                  'sample_end': s.sample_end}
                 for s in cp.slices]}
            for cp in plan.channels],
        'gaps': [_gap_json(gap) for gap in result.gaps],
        'annotations': [_annotation_json(a) for a in result.annotations],
        'incomplete_tail': (
            None if result.incomplete_tail is None
            else {'byte_start': result.incomplete_tail.byte_start,
                  'byte_end': result.incomplete_tail.byte_end,
                  'expected_bytes': result.incomplete_tail.expected_bytes}),
        'samples': samples,
        'sample_stream_truncated': truncated,
        'validated_range': {
            str(channel): [[lo, hi] for lo, hi in intervals]
            for channel, intervals in reader.validated_range.items()},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='EDF+C/EDF+D结构检查与时间窗采集')
    parser.add_argument('path', help='EDF+文件路径')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('inspect', help='输出结构索引，不解码生理样本')

    acquire = sub.add_parser('acquire', help='按半开时间窗采集')
    acquire.add_argument('start', help='窗口起点（带符号十进制秒）')
    acquire.add_argument('stop', help='窗口终点（不含）')
    acquire.add_argument('--timekeeping', default='consistent',
                         choices=['consistent', 'first-channel'],
                         help='多注释通道timekeeping一致性策略')
    acquire.add_argument('--max-samples', type=int, default=None,
                         help='JSON内样本条数上限；流本身仍按切片计划有界')

    args = parser.parse_args(argv)
    try:
        config = AnnotationConfig(
            timekeeping=getattr(args, 'timekeeping', 'consistent'))
        with EDFPlusReader(args.path, config) as reader:
            if args.command == 'inspect':
                payload = inspect_json(reader)
            else:
                if args.max_samples is not None and args.max_samples < 0:
                    parser.error('--max-samples不能为负')
                payload = acquire_json(reader, args.start, args.stop,
                                       args.max_samples)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except EDFPlusError as exc:
        parser.exit(2, f'采集失败: {exc}\n')


if __name__ == '__main__':
    main()
