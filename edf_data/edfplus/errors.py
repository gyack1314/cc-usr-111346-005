"""EDF+读取错误类型。

所有EDF+相关错误都继承项目既有的DataError，调用方既有的DataError捕获
逻辑可以继续生效。读取采取失败闭合策略：结构不合法即抛出，不产出部分
采集结论。
"""
from ..io import DataError


class EDFPlusError(DataError):
    """EDF+读取的基础错误。"""


class EDFHeaderError(EDFPlusError):
    """固定头、逐信号头或其极值关系不合法。"""


class EDFConfigError(EDFPlusError):
    """读取配置不在声明支持的范围内。"""


class EDFStructureError(EDFPlusError):
    """数据记录、记录起点或文件尾结构不合法。"""


class EDFTALError(EDFStructureError):
    """注释通道TAL字节结构、时间文法或UTF-8文本不合法。"""
