# Windows 迁移运行说明

该压缩包不包含 `.venv`。Python 虚拟环境中保存了创建电脑的绝对路径，不能复制到另一台电脑使用。

## 首次安装

1. 解压本压缩包，路径尽量不要包含特殊符号。
2. 双击 `setup-env.bat`，等待依赖安装和环境检查完成。

目标电脑不需要预装 Python。安装脚本会在缺少 uv 时将其安装到项目的 `.tools` 目录，再由 uv 自动安装和管理 64 位 Python 3.10、创建 `.venv` 并安装 `requirements.txt`。也可通过环境变量 `RPA_PYTHON_VERSION` 选择 3.11 或 3.12。

安装脚本默认使用清华大学 PyPI 镜像：`https://pypi.tuna.tsinghua.edu.cn/simple`。

## 开始采集

1. 登录微信并打开搜一搜窗口。
2. 设置 `MONGO_URI`，并确保目标电脑可以访问对应 MongoDB；未设置时使用 `mongodb://127.0.0.1:27017/`。
3. 双击 `start-rpa.bat`。
4. 运行期间不要操作鼠标和键盘，也不要锁屏或改变分辨率。

启动脚本会从 `weixin.collection_target` 读取所有公众号，采集今天和昨天的文章，只识别转发数，并写入 `weixin.article`。日志、截图、JSONL 和 CSV 保存在 `output` 目录。

需要停止时，在运行窗口按 `Ctrl+C`。
