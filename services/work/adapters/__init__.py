"""业务投影适配器组装点。

新业务域接入三步：① 在本目录写一个适配器文件（实现 exists/summarize，
权限口径由该域自定）；② 在下方 import 它；③ 无第三步——核心与前端自动
获得该类型的关联/看板/认领能力（stewardable=True 才有认领）。
"""
from . import course            # noqa: F401
from . import camp_session      # noqa: F401
from . import feedback_ticket   # noqa: F401
