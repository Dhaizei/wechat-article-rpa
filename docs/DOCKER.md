# Docker MongoDB 部署

Docker 只承载 MongoDB。微信客户端、视觉采集器、OCR 和控制台仍然运行在 Windows 交互式桌面中，不能放进 Linux 容器。

## 一键启动

确认 Docker Desktop 已启动：

    docker version
    docker compose version

在项目根目录执行：

    .\mongodb.bat setup
    .\mongodb.bat start
    .\mongodb.bat status

setup 会生成被 Git 忽略的 .env.mongo，包含两套随机密码：

- mongo_root：仅用于容器初始化与健康检查；
- wechat_rpa：仅拥有 weixin 数据库的 readWrite 权限，采集器使用此账号。

控制台启动脚本会自动读取 .env.mongo，因此通常不需要再手工设置 MONGO_URI。

## 默认网络

    127.0.0.1:27019 -> 容器 27017

默认 27019 是为了避免与开发电脑已有的 27017 MongoDB 冲突。如果需要修改，只编辑本地 .env.mongo：

    MONGO_BIND_IP=127.0.0.1
    MONGO_PORT=27020

修改端口后执行：

    .\mongodb.bat stop
    .\mongodb.bat start

不要把未启用来源限制的 MongoDB 暴露到公网。若采集器与 MongoDB 不在同一台机器，设置 MONGO_BIND_IP=0.0.0.0 后，还必须通过防火墙只允许采集虚拟机 IP 访问。

## 数据持久化

MongoDB 数据保存在 Docker 命名卷：

    wechat_article_mongo_data

执行 mongodb.bat stop 只删除容器网络，不删除数据卷。不要运行以下命令，除非明确要永久清空全部数据：

    docker compose --env-file .env.mongo down --volumes

## 备份

    .\mongodb.bat backup

备份文件保存在：

    output\mongodb-backups\weixin-YYYYMMDD-HHMMSS.archive.gz

output 已被 Git 忽略，备份不会进入代码仓库。请把重要备份复制到另一块磁盘或备份服务器。

## 恢复

恢复会覆盖备份中存在的同名集合，所以要求 -Force：

    .\mongodb.ps1 restore -BackupFile .\output\mongodb-backups\weixin-20260803-120000.archive.gz -Force

恢复前建议再执行一次 backup，并确保没有采集任务正在运行。

## 初始化脚本的执行时机

docker/mongo-init.js 只会在 MongoDB 数据卷第一次创建时执行。它负责：

- 创建 wechat_rpa 应用账号；
- 创建 collection_target、article、collection_runs 索引；
- 建立标准化 URL 唯一约束。

如果已经用旧配置创建过数据卷，后来才修改密码或初始化脚本，重启容器不会重新执行初始化。此时应使用现有密码迁移，或者先备份，再明确删除旧卷并重新初始化。

## 常见问题

### 端口被占用

编辑 .env.mongo：

    MONGO_PORT=27020

然后重新启动。

### 容器 unhealthy

    .\mongodb.bat logs

重点检查 root 密码、初始化脚本和数据卷是否来自另一套旧配置。

### 如何查看连接串

    .\mongodb.bat connection

连接串中包含应用密码，不要把输出粘贴到 Issue、日志或截图中。
