# 把 F3 保存到 GitHub

这个 ZIP 是完整源码包。建议仓库名为 `okx-funding-f3-paper`，先建立私有仓库。上传只保存代码，不会启动服务器，也不会建立交易所订单。本程序不需要任何欧易 API 密钥。

## 1. 解压并检查

在 Mac 解压，把得到的 `okx-funding-f3-paper` 文件夹放到桌面。接下来使用 **Mac 本地终端**，不要在登录服务器的 SSH 窗口里执行：

```bash
cd "$HOME/Desktop/okx-funding-f3-paper"
python3 tools/check_repo.py
```

这个小工具只检查常见的私有文件误上传，不联网，不删除文件。归档只应包含代码、文档、公开的参数、`.env.example` 和测试；不应有 `.env`、订阅 YAML、真实运行的 `var/`、日志或导出的账本压缩包。

`validation/` 是原项目的测试摘要，含公开历史数据验证结果，不是你的服务器账户账本。

## 2. 新建 GitHub 仓库

在 GitHub 选择 **New repository**，名称填 `okx-funding-f3-paper`，选择 **Private**。不要勾选自动创建 README、`.gitignore` 或许可证；源码包自带前两项，是否选择许可证由你决定。

## 3. 上传源码

在 Mac 的这个项目目录执行：

```bash
git init -b main
git add .
python3 tools/check_repo.py --staged
git status --short
git diff --cached --stat
```

`git add` 只是将文件放进“准备提交”的列表；`--staged` 检查的是这个列表中的实际内容。确认没有订阅、账本或本机 `.env` 后再继续：

```bash
git commit -m "Import F3 paper strategy and user service guides"
git remote add origin https://github.com/YOUR_GITHUB_NAME/okx-funding-f3-paper.git
git push -u origin main
```

把 `YOUR_GITHUB_NAME` 换成自己的 GitHub 用户名。`commit` 在本地记录版本，`push` 才上传。如果是第一次用 Git，按提示设置提交者姓名和邮箱；推送按 GitHub 的认证提示登录，HTTPS 推送不能使用账户密码。[GitHub 官方导入说明](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)

## 4. 也可以网页上传

在仓库中使用 **Add file → Upload files**，上传解压后的目录内容。直接上传 ZIP 只会保存压缩包，不会自动变成展开的源码仓库。

Finder 按 `Command + Shift + .` 显示隐藏文件。要保留 `.gitignore`、`.gitattributes`、`.env.example` 和 `.github`；使用 Git 命令能减少遗漏。

## 5. 以后编辑和更新

在 Mac 的干净源码副本修改，然后运行离线测试，检查改动再提交：

```bash
python3 -B -m unittest discover -s tests -q
python3 tools/check_repo.py
git add .
python3 tools/check_repo.py --staged
git diff --cached
git commit -m "Describe the change"
git push
```

测试需要 Python 3.10 或以上。`git diff --cached` 是查看具体改动；按 `q` 退出。检查失败时不要继续提交。检查工具不能识别所有秘密；`.gitignore` 也不能清除已经提交到 Git 历史的敏感内容。

**GitHub 更新不会自动更新服务器。** 正在运行的账本保存了核心代码与配置身份，不要在运行目录直接 `git pull` 或覆盖代码。换用户服务请按[迁移说明](MIGRATE.md)仅更新部署文件；策略代码升级属于另一件事。

## 6. 自动测试与备份

`.github/workflows/offline-tests.yml` 会在 GitHub Actions 运行离线测试、安装脚本语法检查和源码文件检查，使用 Python 3.10/3.13。不需要添加 GitHub Secrets，也不部署或启动任何模拟服务。交付时实际运行结果见 [TEST-REPORT.md](../TEST-REPORT.md)，GitHub 的结果要在上传后查看。

GitHub 是代码存档；服务器运行数据应按[日常操作中的导出方法](OPERATIONS.md)单独保存，不要将 `.env`、代理订阅或 `var/` 推送进去。重装时只有源码并不能恢复此前的虚拟持仓与收益记录。
