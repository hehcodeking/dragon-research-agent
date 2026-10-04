# 发布到 GitHub

发布前先确认项目目录中没有密钥和私人数据：

```bash
python3 -m unittest discover -s tests -v
./run.command demo
git status --short --ignored
```

`.gitignore` 已排除 `.env`、`reports/`、`state/`、构建文件和常见密钥文件。忽略规则只能避免新的误提交；如果密钥曾进入 Git 历史，应撤销旧密钥，而不是只删除文件。

新建空 GitHub 仓库后，可在项目目录执行：

```bash
git init
git add .
git commit -m "Initial public release: Dragon Research Agent v0.3.0"
git branch -M main
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

建议首个 Release 使用标签 `v0.3.0`，说明它是研究和教学项目，真实行情只读，实盘路由关闭，不包含有效性或收益承诺。
