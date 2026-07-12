# 委员会评审简报:登录令牌校验函数

## 背景
一个 Web 后端的会话中间件,负责在每个请求上校验 JWT 令牌。下面是待评审的实现。团队担心其安全性与健壮性,请委员会评审。

## 待评对象(完整代码)
```python
import jwt, time

SECRET = "change-me"

def verify_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256", "none"])
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload
```

## 已知约束
- 生产环境每秒约 2000 次调用,不能显著增加延迟。
- 令牌里带 `user_id` 与 `role`,下游据此授权。

## 委员会问题
1. 这段代码有哪些安全或正确性问题?各自严重度?
2. 有无被忽略的边界情况?
3. 给出最小修复方向。

## 范围
- 只评审这个函数本身;不评审 JWT 签发流程与密钥轮换机制(out_of_scope)。
