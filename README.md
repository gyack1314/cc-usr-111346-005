# EDF+多采样率资料的断续记录与注释时间采集

本项目保存研究协作采集所需的本地资料，提供内容摘要、记录身份与登记关系检查。当前读取入口只返回表数量及身份检查结果，不解释原始格式或获取远端资料。

资料表包含signals、windows。字段单位和关系见 docs/data-contract.md。data/normal.json登记常规资料，data/boundary.json登记特殊业务情形，data/broken-digest.json用于检查资料损坏的拒绝行为。JSONL文件按清单内SHA256和字节长度绑定。sources/含合成原始资料及独立构造的原值记录，sources/manifest.json登记其长度及摘要。当前只核验这些文件的完整性，不解析其业务格式。

运行环境为 Python 3.11及以上，当前仅使用标准库，无需额外服务。包内io.py负责资料读取，domain.py负责表集合与引用检查，cli.py提供本地检查入口。

```sh
python -m unittest discover -s tests -v
python -m compileall -q edf_data tests
python -m edf_data.cli data/normal.json
python -m edf_data.cli data/boundary.json
```

容器内使用同样的检查入口：

```sh
docker build -t edf-data .
docker run --rm edf-data
```

## EDF+采集（EDF+C/EDF+D）

`edf_data/edfplus/` 在不改动既有清单校验的前提下，增加EDF+文件读取：
按原通道与真实记录起点交付样本和注释，不诊断、不重采样。固定头与逐信号
头按ASCII定宽字段解析，信号数据为小端有符号16位，各通道可有不同
samples-per-record；物理值按digital/physical min-max做精确有理
（Fraction）映射并保留原digital值，单位、transducer、prefilter作为原
元数据。注释信号按字节解析TAL，EDF+C强制连续、EDF+D允许空档，重复或
重叠起点直接失败；结构索引与样本已验证范围分开声明，时间窗按记录起点
seek切片，读取后段窗口不解码前面的生理样本。设计细节见
[docs/edfplus-design.md](docs/edfplus-design.md)。

```sh
# 结构索引（记录起点、空档、截断尾、注释通道），不解码生理样本
python -m edf_data.edfplus <文件> inspect
# 半开时间窗[start,stop)采集：切片计划、空档、注释、有界样本流
python -m edf_data.edfplus <文件> acquire 2 4 --max-samples 100
# 端到端核对：两种采样率+断档、多文本/负起点、未知记录数、损坏TAL拒绝
python -I scripts/edfplus_demo.py
```

注：本环境的 `python -I` 不把cwd加入sys.path，模块方式在普通模式下
直接可用；隔离模式下运行测试用 `python -I -m unittest`（发现器自行
注入目录），CLI可用演示脚本中的显式sys.path引导方式。
