# 说明（构建与分发）

本仓库用于在 [Zed](https://github.com/zed-industries/zed) 源码上应用本地化替换（默认 `zh.json`），并通过 GitHub Actions 构建 Windows 可执行文件（`zed.exe`）。

## 下载

- 推荐从 Releases 下载最新 zip。
- 也可以在 Actions 的运行记录里下载 Artifacts。

## 产物包含

每个发布包/Artifact 中包含：

- `zed.exe`
- `NOTICE.md`：本说明
- `BUILD_INFO.txt`：构建信息（Zed commit、仓库 commit、后端等）
- `SOURCE.txt`：对应源码获取方式与复现构建说明
- `LICENSE.zed-loc.MIT.txt`：本仓库许可证
- `licenses/`：从上游 Zed 源码复制的 `LICENSE*` 文件

## 免责声明

本仓库为社区汉化构建脚本与翻译，不隶属于 Zed 官方。
