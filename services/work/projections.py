"""内部工作台·业务投影注册表（跨组方案 §4.3 通用化，X2）。

工作台核心不认识任何具体业务：营期/课程/工单……都以「适配器」形式向本注册表
注册，提供统一契约（存在性校验 + 安全摘要）。核心代码（integrations/boards/
前端）只面向契约盲渲染，新业务域接入 = 写一个适配器文件 + 在 adapters/__init__
加一行 import，**工作台核心零改动**（开闭原则，验收标准）。

红线（继承基础方案 §11.3）：投影只读；summarize 只回安全字段（None=不可访问
占位，不泄露标题）；适配器不得提供任何写操作；域内统计留在业务模块自己那里。

契约要点：summarize(user, source_id) 返回的 dict 必须含 'title' 键（看板排序
与显示依赖）；provider.stewardable 决定该类型能否被工作区「认领维护责任」。
"""
from services.work.access import WorkApiError

PROVIDERS = {}


class WorkProjectionProvider:
    """业务域投影适配器基类。子类实现四个属性/方法后调 register_projection。"""

    source_type = ''        # 与 business_link 白名单同键：course / camp_session / ...
    label = ''              # 显示名：课程 / 营期 / 工单
    stewardable = False     # 是否可被工作区认领（责任物=True；流程性对象= False）

    def exists(self, source_id):
        """建关联时的目标存在性校验。"""
        raise NotImplementedError

    def summarize(self, user, source_id):
        """安全摘要 {字段: 值}（必含 'title'）；不可访问返回 None。"""
        raise NotImplementedError


def register_projection(provider):
    """注册适配器；同 source_type 重复注册视为装配错误（启动期暴露）。"""
    st = provider.source_type
    if not st or not provider.label:
        raise ValueError('投影适配器缺少 source_type/label')
    if st in PROVIDERS:
        raise ValueError(f'投影适配器冲突：{st} 已注册为 {PROVIDERS[st].label}')
    PROVIDERS[st] = provider


def get_provider(source_type):
    return PROVIDERS.get(source_type)


def registered_types():
    """可关联类型清单（business_link 白名单的数据源）。"""
    return tuple(PROVIDERS.keys())


def unknown_source_error():
    """统一错误信息（含当前已注册类型，便于调用方理解）。"""
    return WorkApiError(400, f"source_type 仅支持 {'/'.join(registered_types())}")
