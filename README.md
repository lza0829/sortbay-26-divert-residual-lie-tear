# 分拣过机台（Sortbay）

骨架：过机扫描装载 → 口门路由裁决 → 错分回流批次 → 封袋班次对账。

- 前端 http://127.0.0.1:6600
- API http://127.0.0.1:11600

## 错分检出的全局口径

本仓库只贯彻一种残态架构：

> **检出即扣减原口门在口；活动袋关联在同一事务内作废，但原袋编号、回流批次、领域事件与审计全部留痕；错分件继续开放“再裁决”。**

因此：

1. `已路由` / `已入袋` 才能错分检出。
2. 检出成功后扫描状态为 `错分回流`，总览“错分桶”点亮。
3. 口门在口按 `已路由 + 已入袋` 实时计算；检出同事务写入 `-1` 占用事件，原口门立即扣减，不允许检出后仍占口。
4. `scans.bag_id` 是活动袋关联，检出时置空；`scans.original_bag_id` 与 `bag_relations` 保留原袋。未再裁决前，原袋入口不能把该件直接收成 `已入袋`。
5. `scans.divert_batch_id` 挂载回流批次号；再裁决后仍保留该批次关联。
6. `/api/错分检出`、`/api/回流批次`、`/api/扫描/{id}/错分检出` 是同一用例入口；所有写入走 `BEGIN IMMEDIATE` 事务和条件更新，避免检出/入袋对撞产生双终态。
7. `/api/审计/重放?扫描编号=<id>` 或 `/api/错分检出/{id}/重放` 可重放领域事件、占用事件和袋关联，并对照当前落账状态。

## 常用 API

```bash
# 过机、路由、检出
curl -X POST http://127.0.0.1:11600/api/扫描 \
  -H 'Content-Type: application/json' \
  -d '{"条码":"YB001","目的地码":"110"}'
curl -X POST http://127.0.0.1:11600/api/扫描/1/路由
curl -X POST http://127.0.0.1:11600/api/错分检出 \
  -H 'Content-Type: application/json' \
  -d '{"扫描编号":1}'

# 再裁决与重放
curl -X POST http://127.0.0.1:11600/api/扫描/1/再裁决 \
  -H 'Content-Type: application/json' -d '{"目的地码":"210"}'
curl 'http://127.0.0.1:11600/api/审计/重放?扫描编号=1'
```

## 测试与构建

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
pytest

npm ci --prefix frontend
npm run build --prefix frontend
```

并发对撞、检出后占用、原袋关联留痕、半截事务回滚和再裁决收口夹具位于
`backend/tests/test_divert_residual.py`。
