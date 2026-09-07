# 安全边界

## 应用会做什么

- 在本机读取随安装包提供的结构化命令知识。
- 将问题与 Top 4 知识片段发送给 DeepSeek（仅当用户配置 Key）。
- 在用户目录的 SQLite 中保存会话。
- 将 DeepSeek Key 以 Windows DPAPI 密文保存。

## 应用不会做什么

- 不执行、验证或自动修改回答中的命令。
- 不打开终端，不向 shell 传递命令。
- 不上传会话到项目自有服务，不采集遥测。
- 不把 API Key 写进源码、示例配置、SQLite、日志或 Renderer。
- 不在运行时访问 GitHub。

## 模型与命令安全

模型输出先经过 Pydantic 结构校验，再由确定性正则规则复核危险命令。风险标签是提示而非沙箱；用户仍需在复制后自行确认占位参数、环境和备份。

## 发布前检查

1. 撤销开发过程中使用过的 Key，并为演示创建低额度独立 Key。
2. 检查 `git grep`、构建日志和安装包解压内容中不存在真实 Key。
3. 保持 Electron `contextIsolation: true`、`sandbox: true`、`nodeIntegration: false`。
4. 仅允许 `https://` 来源链接由系统浏览器打开。
