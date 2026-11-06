---
name: upstream-error
description: 接口返回 502、503，进程还活着但依赖的上游服务连不上时使用。
---
# 上游依赖错误

1. 用 http_check 看健康地址，响应内容一般写着连不上的上游地址和端口。
2. 用 check_port 查这个端口有没有进程监听。
3. 没有监听时分两种情况：
   - 上游服务停了：用 service_status 确认后 service_restart 上游服务。
   - 上游服务在别的端口运行：用 list_processes 和 check_port 找到真实端口，改本服务的配置，不要改上游服务。
4. 改了配置要 service_restart 本服务。
5. 再用 http_check 确认返回 2xx，然后 finish。
