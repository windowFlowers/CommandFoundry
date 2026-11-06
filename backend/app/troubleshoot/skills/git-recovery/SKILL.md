---
name: git-recovery
description: Git 提交、分支或改动丢失，仓库卡在合并状态，游离 HEAD 时使用。
---
# Git 恢复

先用 git_inspect 的 status 确认当前状态，再按情况处理：

- 提交不见了：看 reflog，找到丢失前的提交，用 git_action 的 reset_hard 回到它。
- 未提交的改动不见了：看 stash，有记录就用 stash_pop。
- 卡在合并中且用户放弃合并：用 merge_abort。
- 在游离 HEAD 上写的提交：看 reflog 找到提交，切回目标分支后用 cherry_pick 把它拿回来。
- 分支被删：看 reflog 找到分支最后的提交，checkout 这个提交，create_branch 恢复分支名，再 checkout 回原分支。

操作后用 git_inspect 的 log 或 status 确认结果，确认后 finish。不要停留在游离 HEAD 上。
