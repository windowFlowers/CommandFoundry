---
name: database-error
description: 数据库报错，例如 no such column、no such table、结构不匹配、发版后数据库异常时使用。
---
# 数据库错误

1. 从日志确认报错。缺列或缺表一般是迁移没有执行，不是数据坏了。
2. 读项目 README，找到官方的迁移或升级命令。
3. 用 run_script 运行这个命令，参数按 README 写。一次不成功就重新读 README，不要猜参数。
4. 绝不要删除或重建数据库，也不要运行 reset、recreate 这类脚本。
5. 用 service_restart 重启，健康检查通过后 finish。
