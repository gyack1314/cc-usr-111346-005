# EDF+采集设计说明

本说明描述 `edf_data/edfplus/` 的读取架构与关键决策。采集只按原通道、真实
记录起点交付样本与注释：不诊断、不重采样、不做生理解释。

## 1. 模块划分

| 模块 | 职责 |
| --- | --- |
| `header.py` | 256字节固定头与逐信号头的ASCII定宽解析；极值关系、头长/信号数、EDF+变体校验 |
| `tal.py` | 注释信号按字节扫描TAL；0x14/0x15/NUL文法、带符号十进制秒、严格UTF-8、TAL字节范围 |
| `reader.py` | 结构索引、时间窗切片计划、有界样本流、注释事件、已验证范围 |
| `cli.py` | `inspect`（只看结构）与 `acquire`（半开时间窗采集）两个命令 |
| `errors.py` | `EDFPlusError` 体系，继承项目既有 `DataError`，失败闭合 |

## 2. 头部与物理映射

- 固定头与逐信号头全部按ASCII定宽字段解析，非ASCII字节直接失败。
- 逐信号头按**字段分块**排列（全部label连续、再全部transducer_type……），
  不按信号分组。
- 失败条件：头长不等于 `256 + 256×信号数`、`dmin >= dmax`、
  `pmin >= pmax`、reserved 不是 `EDF+C`/`EDF+D`、记录数非法、
  记录时长非正、缺少注释信号。
- 物理映射为精确有理线性式，全程 `fractions.Fraction`，不经过浮点：

  `p = pmin + (d - dmin) × (pmax - pmin) / (dmax - dmin)`

  输出同时保留原 digital 值与该值的2字节源字节。单位
  （physical_dimension）、transducer_type、prefiltering 作为原元数据
  原样保留。注释信号按字节读取，不调用物理映射；其物理极值留空（EDF+
  允许）时不参与极值校验。
- 每通道使用各自 `samples_per_record`，禁止用首通道采样率套用全通道。

## 3. TAL与记录起点

- 注释信号按字节扫描NUL分隔的TAL，不假设固定宽度；填充NUL忽略，任何
  非NUL字节都必须属于一个以NUL结束的合法TAL。
- onset 为带符号十进制秒（显式 `+/-`），duration 为无符号十进制，统一
  转 `Fraction`；注释文本严格UTF-8；一个TAL允许多条注释（多个文本段）。
- 每条记录、每个注释通道必须恰好有一个无文本的 timekeeping TAL，其
  onset 即该记录相对文件起点的真实起点，允许负值。
- 起点规则：
  - EDF+C：首记录起点必须为0，相邻记录首尾相接，断档即失败；
  - EDF+D：允许空档（记录间断档作为 `Gap(kind='gap')` 输出），但记录
    起点必须严格递增；重复或重叠起点直接失败，**不**按文件顺序静默修正。
- 多注释通道策略由 `AnnotationConfig` 显式声明：
  - `consistent`（默认）：同记录各注释通道 timekeeping onset 必须一致，
    否则失败；
  - `first-channel`：以最低序号注释通道为准，不一致记入
    `timekeeping_mismatches`。不支持的策略值在构造配置时即拒绝。
- 空文本 timekeeping TAL 不导出为研究事件。

## 4. 结构索引与已验证范围分离

- `StructuralIndex` 只读取头部与各记录注释通道：记录字节偏移、真实起点、
  TAL（含字节范围）、空档、截断尾、注释通道序号。构建过程**不**解码任何
  生理波形样本。
- `ValidatedRange` 只登记实际解码且通过 digital 极值检查的通道-记录
  半开区间，按记录号合并。两者独立声明，索引存在不代表样本已验证。
- `number-of-records = -1` 时，记录数只能由完整数据记录数与真实EOF确定；
  尾部不足一条完整记录时标记 `IncompleteTail`（带字节范围与期望长度），
  截断记录不进入索引。头部声明了记录数时，字节数与声明不符直接失败。

## 5. 时间窗采集

- 窗口为半开 `[start, stop)`，端点规则固定；start/stop 接受十进制秒
  字符串、int 或 Fraction。
- `plan_window` 是纯结构计算：按记录起点选相交记录，再按**每通道自己的
  样本间隔**计算记录内样本切片（整数上界用有理数ceil，不引入浮点）。
- `iter_samples` 按切片逐块 seek 读取小端有符号16位样本，读取后段窗口
  不必解码前面任何生理样本；样本流逐条产出，内存有界。每条样本带通道
  序号、记录号、样本号、精确时间、digital、physical 与源字节。
- 断档输出为无记录区间：记录间断档 `gap`，窗前/窗后无数据 `uncovered`；
  不插零、不跨断点拼成连续波形。
- 注释按 `[onset, onset+duration)` 与窗口的时间相交选择，持续区间不裁
  成采样点；事件保留所属记录、注释通道与TAL字节范围。
- 头部原始日期时间字符串原样保留，解析结果为 naive datetime（不带
  tzinfo），不用本机时区推导UTC。

## 6. 失败闭合

任何结构、极值、TAL文法、UTF-8、连续性或记录数问题都抛出
`EDFPlusError` 子类，读取器不产出部分采集结论、不登记已验证范围。
损坏TAL因此能阻止该文件的完整采集（见 `tests/test_edfplus.py`
`CorruptTALTests` 与 `scripts/edfplus_demo.py` 第4节）。
