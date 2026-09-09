# pod-history-api 开发规范

## 提交原则

1. **每次变更必须更新 CHANGELOG.md**：在 `[Unreleased]` 区块下按 Added / Changed / Fixed / Removed 分类记录变更内容，格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。
2. **所有代码合入必须走 PR**：禁止直接 push 到 main。流程：feature 分支 → 提 PR → review → squash merge 到 main。main 开启分支保护，需 PR + review，不允许直推。
3. **镜像只能用 main 分支代码构建**：先 PR 合入 main，再从 main（最新 commit）构建镜像、打 tag（`YYYYMMDD-HHMM`）。禁止用未合入的 feature 分支构建线上镜像，确保镜像内容 = main 已合入代码；部署前确认 deploy.yaml 里的镜像 tag 对应的镜像来自 main。
