# MongoDB 使用说明

本项目只依赖 MongoDB，不依赖 Redis。MongoDB 用于持久保存采集账号、文章正文和互动历史；Redis 更适合缓存或分布式任务队列，当前单机桌面 RPA 没有必须引入 Redis 的场景。

## 默认连接

    连接串：mongodb://127.0.0.1:27017/
    数据库：weixin
    账号集合：collection_target
    文章集合：article

通过环境变量修改：

    MONGO_URI=mongodb://127.0.0.1:27017/
    MONGO_DATABASE=weixin
    MONGO_ACCOUNT_COLLECTION=collection_target
    MONGO_ARTICLE_COLLECTION=article

如果 MongoDB 位于其他机器，请使用带认证的连接串，并确认 Windows 虚拟机可以访问 MongoDB 端口。不要把未启用认证的 MongoDB 暴露到公网。

## 账号集合

collection_target 是采集任务的账号来源。推荐文档结构：

    {
      id: "MP_WXS_1234567890",
      name: "量子位",
      category: "技术综合类",
      enabled: true
    }

name 是 MongoDB 中的统一公众号名称。微信实际搜索名与统一名称不同时，不要复制一条新账号记录，请在 config/account_aliases.json 中维护一对一别名。

## 文章集合

article 中每个文档代表一篇文章，核心结构：

    {
      account: { id: "MP_WXS_1234567890", name: "量子位" },
      article: {
        title: "文章标题",
        url: "https://mp.weixin.qq.com/s/...",
        urlNormalized: "https://mp.weixin.qq.com/s/...",
        publishDate: ISODate("2026-08-03T08:00:00.000Z"),
        content: { text: "纯文本正文" }
      },
      interactionHistory: [{
        shareCount: 120,
        collectedAt: ISODate("2026-08-03T01:00:00.000Z"),
        recognitionMethod: "template-ocr-share-only",
        source: "wechat-desktop-rpa"
      }],
      source: {
        type: "wechat-desktop-rpa",
        syncedAt: ISODate("2026-08-03T01:00:00.000Z")
      },
      firstCollectedAt: ISODate("2026-08-03T01:00:00.000Z"),
      lastUpdatedAt: ISODate("2026-08-03T01:00:00.000Z")
    }

字段规则：

- article.url 保留采集到的原始链接；
- article.urlNormalized 规范参数顺序并去掉片段，用于幂等去重；
- article.publishDate 沿用项目约定，按公众号页面显示的北京时间保存；
- 已有非空标题、正文和发布时间不会被重复采集覆盖；
- 每次成功读取互动数会追加 interactionHistory 快照，最多保留最近 90 条；
- 仅转发数模式只写 shareCount，不会用空值覆盖其他指标。

## 索引

采集器首次写入时会自动尝试创建 URL 唯一索引：

    db.article.createIndex(
      { "article.urlNormalized": 1 },
      { name: "article_url_normalized_unique", unique: true, sparse: true }
    )

如果历史数据已存在重复 URL，程序会降级创建普通查询索引并继续采集。建议清理重复数据后恢复唯一索引。可额外创建常用只读查询索引：

    db.article.createIndex(
      { "article.publishDate": -1 },
      { name: "article_publish_date_desc" }
    )
    db.article.createIndex(
      { "account.name": 1, "article.publishDate": -1 },
      { name: "account_publish_date_desc" }
    )
    db.collection_target.createIndex(
      { name: 1 },
      { name: "account_name_unique", unique: true }
    )

检查规范化 URL 重复项：

    db.article.aggregate([
      { $match: { "article.urlNormalized": { $type: "string", $ne: "" } } },
      { $group: {
          _id: "$article.urlNormalized",
          count: { $sum: 1 },
          ids: { $push: "$_id" }
      } },
      { $match: { count: { $gt: 1 } } }
    ])

## 初始化账号

在 mongosh 中执行：

    use weixin
    db.collection_target.insertMany([
      { id: "RPA_QUANTUM", name: "量子位", category: "技术综合类" },
      { id: "RPA_JIQIZHIXIN", name: "机器之心", category: "技术综合类" }
    ])

也可以在管理控制台的“公众号管理”页面导入 CSV 或 JSON。导入只修改账号集合，不会删除或覆盖文章正文。

## 常用只读查询

查询某公众号最新文章：

    db.article.find(
      { "account.name": "量子位" },
      {
        "article.title": 1,
        "article.publishDate": 1,
        "article.url": 1,
        interactionHistory: { $slice: -1 }
      }
    ).sort({ "article.publishDate": -1 }).limit(20)

## 备份与恢复

备份整个数据库：

    mongodump --uri="mongodb://127.0.0.1:27017/" --db=weixin --out=backup

恢复：

    mongorestore --uri="mongodb://127.0.0.1:27017/" --db=weixin backup\weixin

迁移到新电脑前，先停止旧机器采集任务，再备份数据库与 config/account_aliases.json。恢复并核对文章数量后，才能启动新机器定时任务，避免两台设备同时写入。
