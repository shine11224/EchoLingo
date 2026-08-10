"""计费门面：路由/服务层唯一入口。单用户或公开库（webapp.auth 缺失）完全 no-op。

只判断运行模式并转发到 webapp.auth.credits；不在这里实现账务逻辑。

Task 7 增加 operation context：
- use_operation() 把父 operation（如 course_build_media）放入 contextvar；
  db.spawn_with_db_context 用 copy_context 传播，后台线程自动继承。
- 处于父 operation 上下文中的内部调用（翻译/导航/对齐/TTS）一律视为 bundled，
  charge() 直接返回 None，不产生子扣费。
- 后台 worker 在核心成功/失败点调 settle_current()/release_current()，
  离开上下文时 use_operation 的 finally 负责 reset，不泄漏到复用线程。
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

from webapp.runtime.access import multiuser_enabled

_MAX_KEY_LEN = 128


class InsufficientCredits(Exception):
    """余额不足：携带 operation_type/required/available 供 API 层构造统一 402。

    定义在本模块（而非 webapp.auth.credits）：credits 会被测试 importlib.reload
    重建类对象，路由层按旧引用 except 会漏捕；本模块不被 reload，身份稳定。
    """

    def __init__(self, operation_type: str, required: int, available: int):
        super().__init__(f"积分不足：{operation_type} 需要 {required}，可用 {available}")
        self.operation_type = operation_type
        self.required = required
        self.available = available


class OperationConflictError(Exception):
    """幂等语义冲突：Idempotency-Key 被不同操作类型/业务对象占用，或业务引用
    已被其他 operation 拥有。路由层显式映射 409；detail 可携带结构化信息。
    与 InsufficientCredits 同样定义在本模块保证 reload 后类身份稳定。"""

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def require_operation_identity(op: dict | None, *, operation_type: str,
                               reference_type: str | None = None,
                               reference_id: str | None = None) -> None:
    """重放语义身份校验（中央唯一入口，fail closed）。

    (username, idempotency_key) 只保证 key 唯一，不保证语义一致：
    key 跨 operation_type 复用、或同类型但指向不同 lesson/upload 时，
    必须 409，绝不得静默返回/结算/释放不相关的原 operation。
    reference_id 只在两侧都已知时比较（未绑定的 recovery 窗口不误判）。
    """
    if op is None:
        return
    if op.get("operation_type") != operation_type:
        raise OperationConflictError(
            f"Idempotency-Key 已被操作 {op.get('operation_type')} 占用，"
            f"不能用于 {operation_type}")
    if reference_type is not None:
        actual_type = op.get("reference_type")
        if actual_type is not None and actual_type != reference_type:
            raise OperationConflictError(
                f"Idempotency-Key 绑定的引用类型 {actual_type} 与 {reference_type} 不符")
    if reference_id is not None:
        actual_id = op.get("reference_id")
        if actual_id is not None and str(actual_id) != str(reference_id):
            raise OperationConflictError(
                "Idempotency-Key 已绑定到其他业务对象，请换用新的 key")

# 当前线程/上下文的父 credit operation（dict，来自 credits.reserve/get_operation_by_key）
_current_operation: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "elt_credit_operation", default=None)


def _credits():
    """多用户模式下返回 credits 模块，否则 None（完全 no-op）。"""
    if not multiuser_enabled():
        return None
    try:
        from webapp.auth import credits
    except ImportError:
        return None
    return credits


def mode() -> str:
    c = _credits()
    return c.mode() if c else "off"


def quote(operation_type: str, **kwargs):
    c = _credits()
    if c is None or c.mode() == "off":
        return None
    return c.quote(operation_type, **kwargs)


def reserve(username: str, operation_type: str, **kwargs):
    c = _credits()
    if c is None:
        return None
    return c.reserve(username, operation_type, **kwargs)


def settle(operation_id: str, **kwargs):
    """多用户模式返回底层结算结果；单用户/公开库或空 id 返回 None。"""
    c = _credits()
    if c is None or not operation_id:
        return None
    return c.settle(operation_id, **kwargs)


def release(operation_id: str, *, reason: str):
    """多用户模式返回底层释放结果；单用户/公开库或空 id 返回 None。"""
    c = _credits()
    if c is None or not operation_id:
        return None
    return c.release(operation_id, reason=reason)


# ---------- operation 上下文（Task 7）----------

def current_operation() -> dict | None:
    """当前上下文的父 operation；无则 None。内部 bundled 调用据此跳过计费。"""
    return _current_operation.get()


@contextmanager
def use_operation(op: dict | None):
    """把父 operation 放入 contextvar；finally 中 reset，异常也不泄漏。

    路由在 reserve 成功后进入本上下文再 enqueue 后台任务：
    copy_context 会把本 contextvar 一起带进后台线程。
    """
    token = _current_operation.set(op)
    try:
        yield op
    finally:
        _current_operation.reset(token)


def charge(username: str, operation_type: str, **kwargs):
    """路由层统一计费入口。

    - 单用户/公开库/off：返回 None（no-op）。
    - 处于父 operation 上下文（bundled 内部调用）：返回 None，不产生子扣费。
    - 其余转发 credits.reserve；重复 (username, idempotency_key) 返回原 operation。
    """
    if current_operation() is not None:
        return None
    return reserve(username, operation_type, **kwargs)


def settle_current(**kwargs):
    """后台 worker 核心成功点结算当前 operation；无上下文时 no-op。

    系统/自动重试共用同一 idempotency key → 同一 operation：settle 幂等；
    若 operation 已进入终态（如 released 后又被旧线程触发），非法状态转换
    降级为 no-op，绝不让账务异常反过来摧毁已完成的字幕/媒体。"""
    op = current_operation()
    if op is None:
        return None
    try:
        return settle(op["id"], **kwargs)
    except ValueError:
        return None


def release_current(*, reason: str):
    """后台 worker 核心失败点释放当前 operation；无上下文时 no-op。
    与 settle_current 同样容忍终态重复调用，保证系统重试不重复扣分也不报错。"""
    op = current_operation()
    if op is None:
        return None
    try:
        return release(op["id"], reason=reason)
    except ValueError:
        return None


def require_idempotency_key(value: str | None) -> str:
    """多用户可计费入口必须携带 Idempotency-Key；非法时路由层转 400。"""
    v = (value or "").strip()
    if not v or len(v) > _MAX_KEY_LEN:
        raise ValueError("Idempotency-Key header required (1–128 chars)")
    return v


# ---------- 同步 AI 路由计费（Task 8）----------

def billing_active() -> bool:
    """shadow/enforce 为计费激活；off 或单用户/公开库为 False（完全 no-op）。"""
    return mode() in ("shadow", "enforce")


def begin_sync_operation(request, operation_type: str, *, quantity: float = 1,
                         char_count: int | None = None,
                         reference_type: str | None = None,
                         reference_id: str | None = None,
                         estimated_usage: dict | None = None):
    """同步 AI 路由统一计费开始。返回 (op, replay_response)；未激活返回 (None, None)。

    计费激活时强制 Idempotency-Key（缺失 → ValueError，路由转 400）；
    同 (username, key) 重放先做语义身份校验（跨类型/跨对象 → OperationConflictError）：
    - reserved/shadow（in-flight）→ 409 operation_in_flight，绝不发起第二次 provider 调用；
    - released → 409 key_released（前端换新 key 重试）；
    - settled 且有可重放响应 → 返回 (existing_op, response)：路由直接回放，
      一次真实 API 成本、一次扣分、同一响应；
    - settled 但无可重放响应 → 409 replay_unavailable（拒绝免费重调 provider）。"""
    if not billing_active():
        return None, None
    username = str(request.scope.get("elt_username") or "")
    key = require_idempotency_key(request.headers.get("Idempotency-Key", ""))
    existing = get_operation_by_key(username, key)
    require_operation_identity(existing, operation_type=operation_type,
                               reference_type=reference_type, reference_id=reference_id)
    if existing is not None:
        status = existing.get("status")
        if status == "released":
            raise OperationConflictError(
                "该 Idempotency-Key 对应的操作已失败释放，请换用新的 key 重试",
                detail={"code": "key_released"})
        if status in ("reserved", "shadow"):
            raise OperationConflictError(
                "相同 Idempotency-Key 的操作正在进行中，请等待完成",
                detail={"code": "operation_in_flight"})
        response = existing.get("response")
        if response is None:
            raise OperationConflictError(
                "该操作结果不可重放，请换用新的 Idempotency-Key 重新发起",
                detail={"code": "replay_unavailable"})
        return existing, response
    op = reserve(username, operation_type, idempotency_key=key, quantity=quantity,
                 char_count=char_count, reference_type=reference_type,
                 reference_id=reference_id, estimated_usage=estimated_usage)
    return op, None


def settle_sync(op: dict | None, *, actual_usage: dict | None = None,
                response: dict | None = None):
    """同步路由成功点结算：恰好一次；response 为可重放响应载荷（随 settle 原子持久化）。
    重放/终态降级 no-op，不让账务异常摧毁结果。"""
    if not op:
        return None
    try:
        return settle(op["id"], actual_usage=actual_usage, response=response)
    except ValueError:
        return None


def release_sync(op: dict | None, *, reason: str):
    """同步路由失败点释放：AI/网络异常与格式校验失败都必须走这里；终态重复安全。"""
    if not op:
        return None
    try:
        return release(op["id"], reason=reason)
    except ValueError:
        return None


def usage_from_response(resp, *, model: str = "", extra: dict | None = None) -> dict:
    """从 OpenAI 风格响应提取真实用量：model/input/output tokens（provider 暴露时）。"""
    usage: dict = {"model": model or str(getattr(resp, "model", "") or "")}
    u = getattr(resp, "usage", None)
    if u is not None:
        prompt_tokens = getattr(u, "prompt_tokens", None)
        completion_tokens = getattr(u, "completion_tokens", None)
        if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool):
            usage["input_tokens"] = prompt_tokens
        if isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool):
            usage["output_tokens"] = completion_tokens
    if extra:
        usage.update(extra)
    return usage


def billing_error(exc: Exception) -> tuple[int, object]:
    """计费异常 → (status, detail)：不足 402（统一载荷）、语义冲突 409、缺 key 400。"""
    if isinstance(exc, InsufficientCredits):
        return 402, insufficient_payload(exc)
    if isinstance(exc, OperationConflictError):
        return 409, exc.detail or str(exc)
    return 400, str(exc)


def insufficient_payload(exc) -> dict:
    """统一 402 错误体（计划 §8.3）：HTTPException(detail=insufficient_payload(e))。"""
    return {
        "code": "insufficient_credits",
        "operation_type": exc.operation_type,
        "required": exc.required,
        "available": exc.available,
    }


def get_operation_by_key(username: str, idempotency_key: str):
    c = _credits()
    if c is None:
        return None
    return c.get_operation_by_key(username, idempotency_key)


def attach_reference(operation_id: str, reference_type: str, reference_id: str):
    c = _credits()
    if c is None:
        return None
    return c.attach_reference(operation_id, reference_type, reference_id)


def try_consume_free_retry(username: str, capability: str, reference_id: str, *,
                           reason: str = "") -> bool:
    """附属能力首次免费重试；单用户/公开库一律 False（本来就不计费）。"""
    c = _credits()
    if c is None:
        return False
    return c.try_consume_free_retry(username, capability, reference_id, reason=reason)


# ---------- upload 建课归属认领（Task 7，先于 consume/create）----------

def get_build_claim(username: str, upload_id: str):
    c = _credits()
    if c is None:
        return None
    return c.get_build_claim(username, upload_id)


def claim_build_upload(username: str, upload_id: str, operation_id: str,
                       idempotency_key: str):
    c = _credits()
    if c is None:
        return None
    return c.claim_build_upload(username, upload_id, operation_id, idempotency_key)


def promote_build_claim(username: str, upload_id: str, operation_id: str,
                        lesson_id: int):
    c = _credits()
    if c is None:
        return None
    return c.promote_build_claim(username, upload_id, operation_id, lesson_id)


def fail_build_claim(username: str, upload_id: str, operation_id: str, *,
                     reason: str) -> None:
    c = _credits()
    if c is None:
        return
    c.fail_build_claim(username, upload_id, operation_id, reason=reason)
