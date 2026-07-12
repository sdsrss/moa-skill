# 评审简报:用"重试 3 次 + 固定 sleep(2s)"处理第三方支付回调超时

## 背景
我们的订单服务在收到用户下单后,同步调用第三方支付网关的 `POST /charge`。该接口偶发超时(P99 约 8s,超时阈值设 5s)。现提议:超时就重试,最多 3 次,每次之间 `time.sleep(2)`。

## 待评方案(完整)
```python
def charge(order_id, amount):
    for attempt in range(3):
        try:
            resp = gateway.post("/charge", json={"order_id": order_id, "amount": amount}, timeout=5)
            return resp.json()
        except TimeoutError:
            time.sleep(2)          # 固定退避,重试
    raise ChargeFailed(order_id)   # 三次都超时,抛异常
```

## 已知约束
- `/charge` 不保证幂等:同一 `order_id` 多次成功调用会重复扣款。
- 订单服务是同步 HTTP handler,`charge()` 阻塞在请求线程里。
- 支付网关文档说"超时不代表未成功——可能已扣款但响应丢失"。

## 委员会问题
1. 这个重试方案有没有你会直接标 blocker 的正确性问题?给出可复现的失效路径。
2. `time.sleep(2)` 阻塞同步 handler,在高并发下会导致什么?
3. 有没有更简单或更稳的做法达到 80% 效果?
