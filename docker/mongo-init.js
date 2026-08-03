/* global db */

const databaseName = process.env.MONGO_INITDB_DATABASE || "weixin";
const applicationUsername = process.env.MONGO_APP_USERNAME || "wechat_rpa";
const applicationPassword = process.env.MONGO_APP_PASSWORD;

if (!applicationPassword) {
  throw new Error("缺少 MONGO_APP_PASSWORD，拒绝创建无密码应用账号");
}

const database = db.getSiblingDB(databaseName);

// 采集器只获得业务数据库读写权限，不使用拥有全局权限的 root 账号。
database.createUser({
  user: applicationUsername,
  pwd: applicationPassword,
  roles: [{ role: "readWrite", db: databaseName }],
});

// 账号名是管理与采集匹配的统一名称。
database.collection_target.createIndex(
  { name: 1 },
  { name: "account_name_unique", unique: true, sparse: true },
);

// 标准化文章链接用于幂等写入，避免同一文章因追踪参数不同而重复。
database.article.createIndex(
  { "article.urlNormalized": 1 },
  { name: "article_url_normalized_unique", unique: true, sparse: true },
);
database.article.createIndex(
  { "article.publishDate": -1 },
  { name: "article_publish_date_desc" },
);
database.article.createIndex(
  { "account.name": 1, "article.publishDate": -1 },
  { name: "account_publish_date_desc" },
);

database.collection_runs.createIndex(
  { runId: 1 },
  { name: "run_id_unique", unique: true, sparse: true },
);
database.collection_runs.createIndex(
  { startedAt: -1 },
  { name: "run_started_at_desc" },
);
