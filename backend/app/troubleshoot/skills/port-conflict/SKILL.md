---
name: port-conflict
description: 报错 Address already in use、端口被占用或端口冲突时使用。
---
# 端口冲突

1. 先读服务配置，确认服务应该监听的端口，再用 README 核对这个端口是否写对。
2. 用 check_port 查这个端口的监听进程。
3. 用 list_processes 和 service_status 判断占用者是谁：
   - 占用者是另一个受管服务：结束它是错的。通常是本服务的端口配置写错了，用 edit_file 改回正确端口。
   - 占用者是遗留进程，不属于任何服务：用 kill_process 结束它。
   - 端口没人占用：用户说的原因不成立，回到日志继续查。
4. 用 service_restart 重启，再做健康检查，通过后 finish。
