# 官方版本、许可与镜像锁定

核验日期：2026-10-10。选择固定版本，未使用 latest。下列镜像已在本机实际拉取，
Compose 已同时固定 tag 和 digest；这是此次构建基线，不代表自动获得未来安全更新。

| 组件 | 固定版本 | 实际镜像 digest | 许可/依据 |
|---|---|---|---|
| PostgreSQL | 17.11-bookworm | sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 | [PostgreSQL License](https://www.postgresql.org/about/licence/) |
| Valkey | 8.1.10-alpine | sha256:081c2f5cb575efc901aa80ff9cdbd1ec6a301682fd35e1ebb4b0990a4a4a8507 | [BSD-3-Clause](https://github.com/valkey-io/valkey/blob/8.1.10/COPYING) |
| nginx | 1.30.5-alpine | sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94 | [nginx license](https://nginx.org/LICENSE) |
| Prometheus | 3.13.4 | sha256:87861b8cf91579109319ebc300f3f1060e6da9c05d6ae8ad15a20c879e84e32e | [Apache-2.0 / 官方下载](https://prometheus.io/download/) |

PostgreSQL 官方列出 17.11 为 17 系当前小版本，支持至 2029-11-08；
应持续核查后续小版本，而不长期停留在旧补丁。[官方支持周期](https://www.postgresql.org/support/versioning/)
Valkey 官方列出 8.1.10 为 8 系受支持版本，并提供精确 Docker tags。[官方版本](https://valkey.io/download/)
nginx 官方列出 1.30.5 稳定版本。[官方下载](https://nginx.org/en/download.html)
Prometheus 3.13 LTS 支持到 2027-07-31；原候选 3.5 已结束支持，故未选择。
[官方支持周期](https://prometheus.io/docs/introduction/release-cycle/)

应用镜像沿用已核验的 Node 24.21.0、Python 3.12.10 与 Poetry 1.8.5 固定工具链，
Dockerfile 同时锁定两基础镜像实际 digest。Holmes 上游许可证复制到 `/app/LICENSE`，
新增企业模块与上游功能在各自源码目录和提交记录中区分，主 Poetry lock 保留。
企业额外 Python 包精确版本在 `requirements.txt`，前端依赖在现有 package-lock 中。
