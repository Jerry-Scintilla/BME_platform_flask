"""身份域事务工具：冲突保存点。

begin_nested() 开保存点前会无条件 flush 会话挂起对象（_take_snapshot 不看
autoflush）——若把待插对象先 add 再开保存点，INSERT 会落在保存点之外，失败即
毒化整个会话事务（PendingRollbackError），破坏「冲突只回滚保存点、外层事务
仍可用」的可恢复语义（规格 8.2.4）。

用法约定：conflict_savepoint 先开保存点（此时会话应无挂起插入），体内在保存
点内 add + flush；冲突异常由调用方捕获，保存点自动回滚、外层事务保命。
回滚后保存点内新增的对象由 session 自动移出，无须手动 expunge。
MySQL/SQLite 通用（SQLite 测试栈另需 pysqlite savepoint 兼容配方，见测试文件）。
"""
from contextlib import contextmanager

from exts import db


@contextmanager
def conflict_savepoint():
    sp = db.session.begin_nested()
    try:
        yield sp
    except Exception:
        sp.rollback()
        raise
    sp.commit()
