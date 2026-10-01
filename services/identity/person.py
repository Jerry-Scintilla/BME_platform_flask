"""人员档案服务（D2 人员层）：provisional 建立、幂等获取、归并骨架。

锁序（规格 8.2.1）：user 按 id 升序先锁，再 person 按 id。D2 只有 create/get
路径，并发竞争由唯一约束裁决（user.email UNIQUE / person_primary_account
UNIQUE(user_id)+PK(person_id)），不需要显式持锁；D3 归并流程再引入完整锁序。

provisional 语义（规格 3.4）：存量普通账号回填与新注册各建 provisional Person，
全部 unverified——自填姓名/学号不构成核验依据；verified 只能经 D3 核验流程
（registry 登记）写入。系统号不建虚构自然人。
"""
import secrets

from exts import db
from models import PersonModel, PersonPrimaryAccountModel, UserModel


def new_public_id():
    """随机不透明人员编号（展示/引用用，不含任何身份语义）。"""
    return 'p' + secrets.token_hex(8)


def _mint_public_id():
    """生成未占用的 public_id。UNIQUE 是最终裁决，撞库（2^-64 量级）由重试兜底。"""
    for _ in range(3):
        pid = new_public_id()
        if not PersonModel.query.filter_by(public_id=pid).first():
            return pid
    raise RuntimeError('public_id 生成异常：连续碰撞（不应发生）')


def create_provisional(user, *, ensure_primary=True):
    """建 provisional Person + user.person_id + primary 映射（同事务，不 commit）。

    注册路径与回填脚本共用（规格 3.4「不留下无主普通账号」：一个事务内建立
    User→Person→primary 三者）。幂等：user.person_id 已指向时直接返回既有
    Person；primary 行缺失时补建（修复半途状态），不双建。

    并发双发由约束裁决：两人同为该 user 建 primary 时 UNIQUE(user_id) 拒绝后者。
    """
    if user.person_id:
        person = db.session.get(PersonModel, user.person_id)
        if person is not None:
            if ensure_primary and not _primary_of(person.id):
                _add_primary(person, user)
            return person, False

    if user.id is None:  # 注册路径：User 尚未落库，先 flush 拿自增 id
        db.session.flush()
    person = PersonModel(public_id=_mint_public_id())
    db.session.add(person)
    db.session.flush()  # person.id 就位，ppa/user.person_id 才能引用
    user.person_id = person.id
    db.session.flush()  # 先落 user.person_id：复合外键 (user_id,person_id)→user 即时核对此组合
    if ensure_primary:
        _add_primary(person, user)
    return person, True


def get_or_create_provisional(user):
    """幂等入口（回填/运维用）：返回 (person, created)。"""
    return create_provisional(user)


def _add_primary(person, user):
    db.session.add(PersonPrimaryAccountModel(person_id=person.id, user_id=user.id))


def _primary_of(person_id):
    return db.session.get(PersonPrimaryAccountModel, person_id)


def merge_persons(*_args, **_kwargs):
    """归并骨架：实施属 D3 双账号流程（规格 3.4 顺序——持锁保存旧关系清单 →
    处理/暂撤冲突的 primary 与参与锚点 → 更新 user.person_id → 重建存续
    primary/参与锚点与身份归属 → 校验提交；任一步失败全部回滚）。

    D2 只立人员层地基，不提供入口，防止误用。
    """
    raise NotImplementedError('人员归并属 D3 双账号流程，D2 不开放')
