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
