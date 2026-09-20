# 微信网络转发数采集

网络采集是“仅转发数”模式的可选增强。采集器只读取经过身份校验的本机缓存；没有命中时仍会使用 UIA、OCR 和已配置的视觉模型，不会自动修改系统代理、证书或微信进程。

## 启用方式

为抓包进程和控制台设置同一个绝对缓存目录：

```powershell
$env:WECHAT_CAPTURE_CACHE_DIR = 'C:\wechat-rpa-cache'
```

默认模式为 `prefer`：网络缓存未命中时使用原有识别路径。需要在验证环境中强制确认网络链路时，可设置：

```powershell
$env:WECHAT_CAPTURE_MODE = 'strict'
```

`strict` 模式未取得网络统计时会记录采集失败，不会回退到 UIA、OCR 或视觉模型，因此只建议在已经完成抓包环境配置的验证任务中使用。

## 抓包插件

仓库中的 `wechat_capture_addon.py` 是 mitmproxy 脚本。请在独立、可信的测试环境中按 mitmproxy 官方文档配置监听地址、证书和（如有）上游代理，然后以该脚本启动 mitmdump。控制台和 mitmdump 必须使用同一个 `WECHAT_CAPTURE_CACHE_DIR`。

不要开启完整流量导出，也不要将 Cookie、认证参数、证书私钥或完整文章 HTML 写入仓库。采集缓存只保留文章身份、标题、公众号、发布时间、转发数和采集时间。

## 校验与回退

缓存仅接受微信文章地址，并校验文章标识、标题、公众号和发布时间。过期、身份不符、失败响应或数值冲突的记录都会被忽略。日志中的 `article_network_metrics_lookup` 事件显示本次是否采用网络统计；`hit_after_refresh` 表示刷新页面后命中新的响应。

停止测试时，先恢复系统代理，再关闭抓包进程。移除环境变量并重启控制台即可关闭网络采集。
