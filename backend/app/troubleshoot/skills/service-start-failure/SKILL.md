---
name: service-start-failure
description: 服务起不来、启动后马上退出、重启无效或进程不在时使用。
---
# 服务起不来

1. 用 service_status 看最新日志，按最后的报错选方向：
   - Address already in use：加载 port-conflict
   - 缺配置项、配置解析失败、找不到模块：加载 config-error
   - Permission denied：加载 permission-denied
   - No space left：加载 disk-full
   - no such column、unable to open database：加载 database-error
   - 提示已有实例或锁文件：读出锁文件路径和 PID，用 list_processes 确认这个 PID 不存在，再用 delete_path 删除锁文件
2. 修完一处就用 service_restart 重启。启动服务只能用 service_restart，不要找其他启动脚本。
3. 重启后看 service_status，再用 http_check 查健康地址。地址可以在项目 README 或配置里找到。
4. 出现新报错就回到第 1 步，故障可能不止一个。
5. 服务运行且健康检查返回 2xx，立刻调用 finish。
