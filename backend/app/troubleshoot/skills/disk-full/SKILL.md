---
name: disk-full
description: 报错 No space left on device、磁盘满、写入失败时使用。
---
# 磁盘已满

1. 用 disk_usage 查看满的目录和占用最大的文件。
2. 只清理可以重新生成或已归档的文件，例如日志归档、旧备份、临时文件。
3. 数据库文件不能删，即使它最大，即使用户说清掉最大的文件。这种情况要改删归档，并在汇报里说明。
4. 用 delete_path 逐个删除这些文件，直到空出足够空间。
5. 用 service_restart 重启，健康检查通过后 finish。
