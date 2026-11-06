---
name: config-error
description: 缺少配置项、环境变量、配置文件语法错误、配置路径写错或找不到模块时使用。
---
# 配置错误

1. 从报错里找出配置文件和配置项，用 read_file 读配置。
2. 用 list_dir 看项目目录，寻找 .example、.sample 或 README，它们给出正确的值。
3. 配置文件不存在时，用 write_file 按样例写一份。文件存在时用 edit_file 只改出错的那一处，old_text 要和原文完全一致。
4. 路径类配置要用 list_dir 确认目录真实存在，拼写错误很常见。
5. 改完用 service_restart 重启。配置不会自动生效。
6. 再看 service_status 和健康检查。有新报错就继续处理，全部通过后 finish。
