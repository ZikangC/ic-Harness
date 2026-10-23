# 上传到现有 ic-Harness

本次只整理本地备份，没有代为提交或 push。

已只读核查：

- 本机已有 remote：`git@github.com:ZikangC/ic-Harness.git`。
- 仓库可访问，默认分支 `codex/baseline`，检查时 HEAD 为 `2f4c660bc36fa6042fa94253175c45c812cf53d5`。
- 用户最终确认使用 `ic-Harness`；带下划线的 `ic_harness` 不是本次目标。

## 直接建立独立备份分支

`github_up` 目前是已整理的普通目录。以下命令会建立一个独立的备份历史并将完整快照上传为新分支；不需要改动远端默认分支，也不需要 force push。此方式用于版本备份，不用于合并到旧代码。

```bash
cd /Users/chenzikang/code/work/github_up
git init -b codex/backup-v19-20261023
git remote add origin git@github.com:ZikangC/ic-Harness.git
git add .gitignore README.md UPLOAD.md harness RTLskill input examples backup
git status --short
git commit -m "Backup validated v19 harness and skills before regional restructuring"
git push -u origin codex/backup-v19-20261023
```

上传后，在 GitHub 的分支选择器中选择 `codex/backup-v19-20261023` 查看。无需合并 PR 即可保存这个备份。

只运行一次 `git init`/`remote add`；若中途退出，先用 `git status`、`git remote -v` 查看已有状态，再继续剩余命令。分支名已存在时选择新的 `codex/backup-v19-日期`，不要强推覆盖。

## 如果希望保留旧分支祖先关系

可以改用 clone 后在新分支中添加一个独立快照目录：

```bash
git clone git@github.com:ZikangC/ic-Harness.git /Users/chenzikang/code/work/ic-Harness-backup
cd /Users/chenzikang/code/work/ic-Harness-backup
git switch -c codex/backup-v19-20261023
mkdir -p backups/v19-20261023
rsync -a --exclude=.git /Users/chenzikang/code/work/github_up/ backups/v19-20261023/
git add backups/v19-20261023
git commit -m "Archive validated v19 harness and skills"
git push -u origin codex/backup-v19-20261023
```

两种方式任选一种。第二种需要 clone 现有仓库；实际 EDA 输出仍应留在服务器。
