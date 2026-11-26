# 对白试镜·样带留存到期

拓扑排定依赖删除和匿名化操作，受合同保留和共享引用约束。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 样带目录
- 合同留存
- 删除预演
- 匿名归档
- 档案交接册（`/handover-books`）

## 档案交接册

合约组可在“匿名归档”页申请档案交接册，也可调用 `POST /handover-books`。申请时把当前最大执行批次号记为快照高水位；之后新受理的批次号更大，不会并入该册。制作按执行批次 id 键集分页，每页事务写入 `handover_book_items` 并推进 `cursor_run_id`，因此内存只与页大小有关。

`(book_id, run_id)` 唯一约束保证中断续跑或并发重试不会重复收录批次。重试会重新核对已收录批次的执行状态、证据摘要和签署状态；只有快照内全部批次均已完工、存在摘要一致的擦除结论且已签署，交接册才是 `ready`，`GET /handover-books/{id}/download` 才返回 JSON（加 `?format=csv` 可下载 CSV），否则返回 `409`。

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18303`
- FastAPI 文档 `http://127.0.0.1:19303/docs`
- MySQL 8.0 端口 `127.0.0.1:17803`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | MySQL 8.0，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
