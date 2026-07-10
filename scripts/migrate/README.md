# 数据库迁移脚本（生产上线用）

本目录承载 **2025-06-12 基线 → 座位 / 营期 / RBAC 版本** 的全部 schema 与权限迁移。
全部**幂等**，可重复运行。脚本自带 sys.path 修正，**必须在项目根目录执行**。

## 执行顺序（生产原地升级老库）

```bash
cd /opt/BME_platform_flask          # 项目根
python scripts/migrate/migrate_01_user_role.py          # user 加 role 列 + admin→super_admin 回填
python scripts/migrate/migrate_02_camp_schema.py        # 建 6 营期表 + 4 老表加 6 列
python scripts/migrate/migrate_03_feature_camp.py       # camp_session.is_featured + camp_join_request 表
python scripts/migrate/migrate_04_join_selected_days.py # camp_join_request.selected_days
python scripts/migrate/migrate_05_perms.py              # 17 下划线权限 + super_admin 授权
python init_seats.py                                    # 建 study_room/seat 表 + 106 房 40 座位
```

## 顺序依赖
- `migrate_05` 按 `role='super_admin'` 授权 → 必须在 `migrate_01`（加列 + 回填）之后。
- `migrate_03/04` 依赖 `migrate_02` 建出 camp_session / camp_join_request。
- 全部幂等，顺序错了也不报错，但推荐按序。

## 不要跑
- `seed.py` 会灌测试账号 / 演示课程 / 测试通知，生产**禁整体跑**。权限已由 `migrate_05` 处理。
- 本目录**不含**测试 / 清理脚本（`dev_camp_dashboard_test.py` / `dev_test_checkin.py` / `dev_normalize_camp_members.py`，仍 gitignored 留本地）。

## 回滚
- 加列均 nullable / 有默认值，回滚用 `ALTER TABLE ... DROP COLUMN ...`，不伤老数据。
- 新表回滚 `DROP TABLE ...`。
- **升级前务必 `mysqldump` 备份**，这是回滚底气。

## 各脚本做了什么
| 脚本 | 动作 |
|---|---|
| 01_user_role | `ALTER user ADD role VARCHAR(20) NOT NULL DEFAULT 'student'` + `UPDATE user SET role='super_admin' WHERE user_mode='admin'` |
| 02_camp_schema | create_all 建 6 营期表 + 给 user_course / medal_user / check_record / notification 加 6 列（INT NULL） |
| 03_feature_camp | `camp_session.is_featured` + 建 camp_join_request 表 |
| 04_join_selected_days | `camp_join_request.selected_days TEXT` |
| 05_perms | 17 下划线权限 + 删点号旧权限 + super_admin 全授权 |
