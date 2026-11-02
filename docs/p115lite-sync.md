# 115Lite 上游镜像与手动同步

本仓库同步 [madrays/MoviePilot-Plugins 的 115Lite](https://github.com/madrays/MoviePilot-Plugins/tree/main/plugins.v3/p115lite)，保留插件 ID `P115Lite`、配置前缀 `p115lite_`、版本和作者。原始实现另包括 DDSRem / DDSRem-Dev 与 zkmydgth 的贡献。许可证与作者声明分别保存在插件及前端目录的 `LICENSE`、`NOTICE`；本仓库负责镜像、兼容检查及打包，原作者不为这些打包调整背书。

## 安装和与 MetaTube 共存

- 本次镜像：115Lite **3.0.3**；源提交、上游 ZIP SHA256、全部文件校验值见 [来源记录](../upstream/p115lite.json)。
- 适用环境：MoviePilot **V3 >=3.0.0**、Python **>=3.14,<3.15**、Linux/glibc **x86_64**。ARM、macOS 及其他 Python 版本不在此安装包支持范围内。
- 在 MoviePilot 添加本仓库地址 `https://github.com/zcs19960929/MoviePilot-Plugins`，选择 115Lite 安装。市场使用本仓库 Release 整包，包含 `dist/`、迁移文件及三个 CPython 3.14 Linux wheel；不能只复制 Python 入口文件。
- 这是相同插件 ID 的另一个来源，不是第二个独立实例。已有 115Lite 切换来源时先保留配置，避免重复整理或监控。
- MetaTube 保持 **1.1.5**、原有代码和配置。两款插件都固定 `p115client==0.0.9.6.5.1` 与 `python-concurrenttools==0.1.8`，不要单独升级其中一项。
- Cookie 可以使用同一份，但由两款插件分别保存；不要让两款插件同时整理同一网盘目录，输出到不同本地目录。MetaTube 的 115 Cookie 直连模式不通知 115Lite 生成 STRM。

安装不会替换容器 Python。安装前检查容器实际 Python/架构；版本不符时不要强行安装此包。离线测试使用宿主桩和模拟网盘，不代表已在真实 MoviePilot 完整整理过文件。

## 手动同步

需要 Python 3.11+；推荐使用隔离的 Python 3.14。默认仅预检，不写入插件或市场文件：

```bash
python3.14 scripts/sync_p115lite.py --ref main
python3.14 scripts/sync_p115lite.py --ref <固定提交或标签> --apply
python3.14 scripts/sync_p115lite.py --verify --build-dir .superpowers/artifacts
```

只会更新本工具登记的 115Lite 后端、前端、来源记录和 `P115Lite` 市场条目；不删除其他插件，不覆盖本地已改动的镜像文件。没有变化就不产生内容更新。

遇到以下情况会拒绝继续：SDK 版本不匹配、Python 支持范围变化、缺少匹配 wheel、上游源码与 Release 不一致、原包校验失败，或同版本内容被修改。需要人工审查，不能通过修改 MetaTube 的依赖来绕过检查。

## GitHub Actions：先检查，再审核发布

1. 打开 Actions → **115Lite compatibility and manual sync** → Run workflow。
2. `action=sync`、`repository_ref=main`，`upstream_ref` 填源分支、标签或提交。
3. 工作流在 Linux x86_64 / Python 3.14 中分别按两种顺序安装依赖，运行 `pip check`、上游测试、MetaTube 回归和 SDK 共存检查。通过后创建待审核 PR；无变化不创建 PR。
4. 审核差异及测试产物。准备发布时再运行同一工作流，选择 `action=publish`，`repository_ref` 填审核后的 PR 分支；该分支会重新通过全部检查后发布 Release。
5. 确认本仓库 `P115Lite_v<版本>` Release 中的 ZIP 和 SHA256 文件可下载，再合并市场 PR。工作流不自动批准或合并 PR，也没有定时任务。

手动同步需要仓库允许 GitHub Actions 创建 PR。工作流默认只读，只有创建 PR 的任务授予 `contents/pull-requests: write`，只有发布任务授予 `contents: write`；测试任务不持有写权限，检出的 Git 凭据不持久保存。由 `GITHUB_TOKEN` 创建的 PR 不会自动触发另一次 PR 工作流，因此同步流程自身先完成同样的两组测试，并保留测试产物。

已有相同版本 Release 时只核对内容一致，不替换资产或移动标签。若内容不同，发布失败，需要先厘清版本归属。

## 测试边界

上游测试所带宿主桩缺少存储导入所需的 `Response`、`StorageUsage`、`StorageQueryError`，本仓库仅在测试目录补齐导入用替身；不修改上游运行时代码，不将替身打进安装包。SDK 缓存和临时数据都放在隔离目录。

共存检查实际加载两款插件和固定 SDK，阻止对外连接；模拟列目录与播放取链，并验证启停 115Lite 的 SDK 补丁不会改变 MetaTube 的 Cookie、Token 或请求 User-Agent。实际网盘重命名、移动、删除、MoviePilot 配置保存和端到端播放不由这些离线测试触发。
