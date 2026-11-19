# 已有 GitHub 仓库成员切换到 Gitee 指南

> [!IMPORTANT]
> 开始操作前，请先注册并登录 Gitee 账号：<https://gitee.com/signup>。
>
> 注册完成后，请将自己的 **Gitee 用户名或个人主页链接** 发给李馨渝，添加为Gitee `ICV_Cybersecurity_System` 仓库成员。确认已经获得仓库访问权限后，再按照本文后续步骤切换远程仓库。
>

## 改用 Gitee原因

Gitee 是面向国内开发者的代码托管平台，服务主要面向中国大陆网络环境。与 GitHub 相比，国内成员通常可以直接访问 Gitee，无需配置 VPN 或网络代理，执行 `clone`、`pull` 和 `push` 时也更稳定。因此，本项目将 Gitee 作为团队日常协作的主要仓库，并通过仓库镜像将代码同步到 GitHub。


- 成员已经从 GitHub 克隆了 `ICV_Cybersecurity_System`；
- 当前协作分支为 `integration-test-tools`；
- 需要把 Gitee 设置为默认拉取和推送仓库；
- 保留 GitHub 远程地址用于必要时查询历史或同步代码。

Gitee 仓库地址：

```text
https://gitee.com/forever-lee/ICV_Cybersecurity_System.git
```

## 1. 进入项目目录


## 2. 确认当前远程仓库

```cmd
git remote -v
```

切换前预期输出：


```text
origin  https://github.com/forever-lee/ICV_Cybersecurity_System.git (fetch)
origin  https://github.com/forever-lee/ICV_Cybersecurity_System.git (push)
```

## 3. 确认当前分支

```cmd
git branch --show-current
```

预期输出：

```text
integration-test-tools
```

如果当前不是该分支，执行：

```cmd
git switch integration-test-tools
```

预期输出：

```text
Switched to branch 'integration-test-tools'
```

如果已经位于该分支，可能显示：

```text
Already on 'integration-test-tools'
```

## 4. 将原来的 GitHub `origin` 改名

```cmd
git remote rename origin github
```

成功时没有输出。

检查结果：

```cmd
git remote -v
```

预期输出：

```text
origin  https://github.com/forever-lee/ICV_Cybersecurity_System.git (fetch)
origin  https://github.com/forever-lee/ICV_Cybersecurity_System.git (push)
```

## 5. 将 Gitee 添加为新的 `origin`

```cmd
git remote add origin https://gitee.com/forever-lee/ICV_Cybersecurity_System.git
```

成功时没有输出。

检查结果：

```cmd
git remote -v
```

预期输出：

```text
github  git@github.com:forever-lee/ICV_Cybersecurity_System.git (fetch)
github  git@github.com:forever-lee/ICV_Cybersecurity_System.git (push)
origin  https://gitee.com/forever-lee/ICV_Cybersecurity_System.git (fetch)
origin  https://gitee.com/forever-lee/ICV_Cybersecurity_System.git (push)
```

## 6. 获取 Gitee 分支信息

```cmd
git fetch origin
```

首次获取时，输出类似：

```text
From https://gitee.com/forever-lee/ICV_Cybersecurity_System
 * [new branch]      main                   -> origin/main
 * [new branch]      integration-test-tools -> origin/integration-test-tools
```

实际显示的分支数量可能更多。

首次通过 HTTPS 访问时，可能要求身份认证：

```text
Username for 'https://gitee.com':
Password for 'https://用户名@gitee.com':
```

请使用成员自己的 Gitee 账号：


## 7. 将本地分支关联到 Gitee

```cmd
git branch --set-upstream-to=origin/integration-test-tools integration-test-tools
```

预期输出：

```text
branch 'integration-test-tools' set up to track 'origin/integration-test-tools'.
```

## 8. 检查分支关联结果

```cmd
git branch -vv
```

预期当前分支包含类似内容：

```text
* integration-test-tools 31f94e3 [origin/integration-test-tools] 提交说明
```

提交号和提交说明可能不同，重点是当前分支后面显示：

```text
[origin/integration-test-tools]
```

## 9. 测试拉取

```cmd
git pull
```

如果没有新提交，预期输出：

```text
Already up to date.
```

如果 Gitee 存在新提交，输出类似：

```text
Updating 31f94e3..abcdef1
Fast-forward
 static/app.js | 10 +++++-----
 1 file changed, 5 insertions(+), 5 deletions(-)
```

如果发生冲突或提示分支已经分叉，请停止操作，不要使用 `--force`，应先确认双方提交并解决冲突。

## 10. 测试推送

```cmd
git push
```

如果没有需要推送的新提交，预期输出：

```text
Everything up-to-date
```

如果存在新提交，输出类似：

```text
Enumerating objects: 5, done.
Counting objects: 100% (5/5), done.
Writing objects: 100% (3/3), done.
To https://gitee.com/forever-lee/ICV_Cybersecurity_System.git
   abcdef1..abcdef2  integration-test-tools -> integration-test-tools
```

## 日常开发命令

完成一次切换后，日常操作如下：

```cmd
git pull
git add -A
git commit -m "修改说明"
git push
```

推荐在开始修改前先执行：

```cmd
git switch integration-test-tools
git pull
```

## 异常情况：`github` 远程仓库已经存在

如果执行：

```cmd
git remote rename origin github
```

出现以下错误：

```text
error: remote github already exists.
```

说明本地已经配置了名为 `github` 的远程仓库。此时不要继续重命名，执行：

```cmd
git remote set-url origin https://gitee.com/forever-lee/ICV_Cybersecurity_System.git
git fetch origin
git branch --set-upstream-to=origin/integration-test-tools integration-test-tools
```

然后检查：

```cmd
git remote -v
git branch -vv
```

## 重要注意事项

1. 成员需要先加入 Gitee 仓库，并获得相应的代码访问权限。
3. 不要执行 `git push origin --all`。
4. 本地 `backup/before-zip-cleanup` 分支包含大型环境压缩包，不能推送到 Gitee。
5. 不要使用 `git push --force` 覆盖其他成员提交。
6. 团队应统一向 Gitee 提交代码，由 Gitee Push 镜像同步到 GitHub。
7. 不建议成员继续直接向 GitHub 推送，否则可能与 Gitee 镜像产生冲突或被覆盖。

