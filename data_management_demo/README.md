# Web 数据管理后台演示

该目录是独立的方案汇报页面，不依赖也不修改当前 V-SHIELD 平台的前后端代码。

## 启动

在项目根目录执行：

```powershell
python data_management_demo/run_demo.py
```

浏览器访问：

```text
http://127.0.0.1:8012
```

如需修改端口，可设置环境变量 `DATA_ADMIN_DEMO_PORT` 后重新启动。

## 文件说明

- `index.html`：管理后台页面结构。
- `styles.css`：独立页面样式。
- `app.js`：演示数据、筛选、页面切换及模拟回放交互。
- `run_demo.py`：基于 Python 标准库的独立静态服务器。

当前页面使用方案文档中的模拟数据，尚未连接 MySQL、PCAP 文件或当前平台接口。
