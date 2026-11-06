---
name: permission-denied
description: 报错 Permission denied、unable to open、文件或目录没有权限时使用。
---
# 权限错误

1. 从报错里找到具体路径。没有路径时用 service_status 看完整日志。
2. 用 list_dir 查看它所在目录的权限。
3. 用 chmod_path 改成最小够用的权限：
   - 普通文件 644，密钥文件 600
   - 目录和可执行脚本 755
   不要使用 777 或 666。
4. 用 service_restart 重启，这一步不能省。
5. 健康检查通过后 finish。
