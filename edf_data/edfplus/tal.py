"""EDF Annotations信号的TAL（时间标记注释）按字节解析。

TAL结构（规范定义）：
  "+" onset ["-" duration] 0x14 {注释文本 0x14} 0x00
多段TAL以NUL(0x00)分隔，注释信号的样本区以NUL填充。本模块：
- 不假设固定宽度，逐字节扫描NUL边界；
- onset为带符号十进制秒，duration为无符号十进制，统一转Fraction；
- 0x14(DC4)为字段分隔、0x15(NAK)为时长标记，位置非法即失败；
- 注释文本严格UTF-8解码；
- 保留每个TAL在文件中的字节范围，供注释溯源。
"""
from dataclasses import dataclass
from fractions import Fraction
import re

from .errors import EDFTALError

FIELD_SEP = 0x14
DURATION_MARK = 0x15
NUL = 0x00

# onset规范要求显式符号（+/-）；duration不允许符号与指数写法
_ONSET_RE = re.compile(r'^[+-]\d+$|^[+-]\d+\.\d+$')
_DURATION_RE = re.compile(r'^\d+$|^\d+\.\d+$')


@dataclass(frozen=True)
class TAL:
    onset: Fraction
    duration: Fraction | None
    texts: tuple[str, ...]
    byte_start: int   # TAL首字节相对文件起点的绝对偏移
    byte_end: int     # TAL结束NUL之后的偏移（半开区间）

    @property
    def interval_end(self):
        """注释持续区间终点；无duration时与onset相等（点事件）。"""
        return self.onset + (self.duration if self.duration is not None
                             else Fraction(0))

    @property
    def is_timekeeping(self):
        """空文本TAL为该数据记录的timekeeping TAL。"""
        return len(self.texts) == 0


def _parse_timing(token):
    """解析TAL首个计时token: onset[0x15 duration]，入参为ASCII字节。"""
    marker = bytes([DURATION_MARK])
    if marker in token:
        onset_bytes, duration_bytes = token.split(marker, 1)
        if marker in duration_bytes:
            raise EDFTALError('TAL计时字段出现多个时长标记0x15')
        duration_text = duration_bytes.decode('ascii')
        if not _DURATION_RE.match(duration_text):
            raise EDFTALError(f'TAL duration文法非法: {duration_text!r}')
        duration = Fraction(duration_text)
    else:
        onset_bytes, duration = token, None
    onset_text = onset_bytes.decode('ascii')
    if not _ONSET_RE.match(onset_text):
        raise EDFTALError(f'TAL onset必须为带符号十进制秒: {onset_text!r}')
    try:
        onset = Fraction(onset_text)
    except (ValueError, ZeroDivisionError) as exc:
        raise EDFTALError(f'TAL onset数值非法: {onset_text!r}') from exc
    return onset, duration


def parse_tals(data, absolute_base):
    """解析一条注释信号在单个数据记录中的全部TAL。

    data为该注释信号在该记录内的原始字节；absolute_base为这些字节在
    文件中的绝对偏移。返回tuple[TAL, ...]。填充用NUL被忽略；任何非NUL
    字节都必须属于一个以NUL结束的合法TAL，否则判失败。
    """
    tals = []
    cursor = 0
    length = len(data)
    while cursor < length:
        if data[cursor] == NUL:
            cursor += 1
            continue
        end = data.find(NUL, cursor)
        if end == -1:
            raise EDFTALError(
                f'偏移{absolute_base + cursor}处的TAL缺少结束NUL')
        chunk = data[cursor:end]
        if FIELD_SEP not in chunk:
            raise EDFTALError(
                f'偏移{absolute_base + cursor}处的TAL缺少字段分隔0x14')
        # 以0x14切分：最后一个元素必须为空（终止分隔符）
        parts = chunk.split(bytes([FIELD_SEP]))
        if not parts or parts[-1] != b'':
            raise EDFTALError(
                f'偏移{absolute_base + cursor}处的TAL未以0x14结束')
        timing_token = parts[0]
        if not timing_token or timing_token.startswith(bytes([DURATION_MARK])):
            raise EDFTALError(
                f'偏移{absolute_base + cursor}处的TAL计时字段非法')
        onset, duration = _parse_timing(timing_token)

        texts = []
        # parts[1:-1]为文本段；timekeeping TAL可写作"+0\x14\x00"
        # （无文本段）或"+0\x14\x14\x00"（一个空文本段），二者均归一为空
        for raw_text in parts[1:-1]:
            if raw_text == b'':
                continue
            if bytes([DURATION_MARK]) in raw_text:
                raise EDFTALError(
                    f'偏移{absolute_base + cursor}处的注释文本含非法0x15')
            try:
                texts.append(raw_text.decode('utf-8'))
            except UnicodeDecodeError as exc:
                raise EDFTALError(
                    f'偏移{absolute_base + cursor}处的注释不是合法UTF-8'
                ) from exc

        tals.append(TAL(
            onset=onset,
            duration=duration,
            texts=tuple(texts),
            byte_start=absolute_base + cursor,
            byte_end=absolute_base + end + 1,
        ))
        cursor = end + 1
    return tuple(tals)


def timekeeping_onset(tals, record_index):
    """从一条注释信号的单记录TAL中确定该记录的timekeeping起点。

    规范允许一个TAL携带多条注释，timekeeping TAL是其中无文本的TAL。
    每个注释信号、每条记录必须恰好有一个timekeeping TAL，且其duration
    必须缺省。
    """
    keepers = [tal for tal in tals if tal.is_timekeeping]
    if len(keepers) != 1:
        raise EDFTALError(
            f'记录{record_index}的timekeeping TAL数量为{len(keepers)}，'
            '必须恰好为1')
    keeper = keepers[0]
    if keeper.duration is not None:
        raise EDFTALError(
            f'记录{record_index}的timekeeping TAL不允许携带duration')
    return keeper.onset
