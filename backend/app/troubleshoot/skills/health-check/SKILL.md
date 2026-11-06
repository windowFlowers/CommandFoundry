---
name: health-check
description: 用户只是询问服务是否正常、怀疑有问题、看到日志有报错但没有明确故障时使用。
---
# 确认服务状态

1. 用 service_status 查看服务，用 http_check 查健康地址。
2. 服务运行且健康检查返回 2xx，就说明当前正常。日志里的旧报错是历史记录，看时间判断。
3. 这时直接 finish，说明当前正常，不要做任何修改。
4. 检查失败才算真实故障，加载 service-start-failure 或 upstream-error 继续。
