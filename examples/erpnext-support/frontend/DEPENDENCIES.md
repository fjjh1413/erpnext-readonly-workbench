# 前端新增依赖与许可证记录

以下版本来自本次实际安装的 `package.json`、锁文件与各包根目录许可证文件。没有修改 HolmesGPT 上游许可证，也没有将 ERPNext/Frappe 的许可证归为前端依赖许可。

| 直接依赖 | 锁定版本 | 本地许可证文件 | 许可 |
|---|---|---|---|
| Vue | 3.5.43 | `node_modules/vue/LICENSE` | MIT |
| Element Plus | 2.14.7 | `node_modules/element-plus/LICENSE` | MIT |
| Vite | 8.3.2 | `node_modules/vite/LICENSE.md` | MIT |
| @vitejs/plugin-vue | 6.0.9 | `node_modules/@vitejs/plugin-vue/LICENSE` | MIT |
| Vitest | 5.0.3 | `node_modules/vitest/LICENSE.md` | MIT |
| @vue/test-utils | 2.5.1 | `node_modules/@vue/test-utils/LICENSE` | MIT |
| jsdom | 30.1.2 | `node_modules/jsdom/LICENSE.txt` | MIT |
| Playwright（仅测试） | 1.63.0 | `node_modules/playwright/LICENSE` | Apache-2.0 |
| TypeScript（仅开发） | 5.9.3 | `node_modules/typescript/LICENSE.txt` | Apache-2.0 |
| vue-tsc（仅开发） | 3.3.12 | `node_modules/vue-tsc/LICENSE` | MIT |

企业 TypeScript 升级的实际安装日期为 2026 年 10 月 10 日，保留原版本并新增上述两个开发依赖。npm 当次统计 141 个已审计包、0 个已知漏洞；这是当时的包审计结果，不代表完整生产安全审查。传递依赖使用锁文件复现，各包随安装保留自身许可证。分发前端构建或依赖包时须保留实际使用包的许可证与版权声明；本记录没有替代依赖中的完整许可文件。

浏览器测试使用本机已安装的 Microsoft Edge 154.0.4258.53。未打包、复制或分发 Edge，也未下载或覆盖用户浏览器。测试使用临时隔离配置，不连接日常用户配置。
