# POST /SYSU_BME/face_check 接口变更说明

> **版本**：v2.0（安全加固）  
> **变更日期**：2026-06-11  
> **影响范围**：所有调用 `/SYSU_BME/face_check` 的第三方人脸识别服务

---

## 变更概述

出于安全加固目的，`/face_check` 接口在原有 `FACE_SECRET` 第三方凭据校验的基础上，**新增 JWT 身份鉴权**。

| 项目 | 变更前 | 变更后 |
|------|--------|--------|
| 身份鉴权 | 无 | 必须携带用户 JWT（`Authorization: Bearer <token>`） |
| 用户身份来源 | 请求体 `email` 字段（调用方自填） | 服务端从 JWT 解析，不再信任请求体 `email` |
| 第三方凭据校验 | 保留 | 保留（`token` 字段仍为必填） |

---

## 为什么要做这个变更

原接口仅凭 `FACE_SECRET` 做单因素校验，任何知晓该密钥的调用方均可在请求体中填写任意 `email`，为任意用户伪造签到记录，危害考勤数据完整性。

新方案要求调用方在发起人脸签到前，必须持有**该用户本人**的有效 JWT，从而将"谁在签到"的决定权从调用方转移到服务端，消除伪造风险。

---

## 新版接口规范

### 请求

```
POST /SYSU_BME/face_check
Content-Type: application/json
Authorization: Bearer <用户 JWT>
```

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `status` | string | 是 | 签到动作，枚举值：`check_in` / `check_out` |
| `token` | string | 是 | 第三方人脸服务凭据（即 `FACE_SECRET`） |
| ~~`email`~~ | ~~string~~ | ~~是~~ | **已废弃**，字段将被忽略，用户身份从 JWT 中读取 |

#### 请求示例

```json
POST /SYSU_BME/face_check
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

{
  "status": "check_in",
  "token": "your_face_secret_here"
}
```

### 响应

| HTTP 状态码 | 含义 |
|-------------|------|
| `200` | 人脸签到/签退成功 |
| `401` | 未携带 JWT、JWT 已过期，或 `token` 凭据无效 |
| `403` | 已有未签退记录且未超过 6 小时（禁止重复签到） |
| `404` | JWT 对应用户不存在 |
| `409` | 尝试签退但没有签到记录 |

#### 成功响应示例

```json
HTTP/1.1 200 OK
{
  "message": "人脸签到/签退成功"
}
```

#### 失败响应示例

```json
HTTP/1.1 401 Unauthorized
{
  "msg": "Missing Authorization Header"
}
```

```json
HTTP/1.1 401 Unauthorized
{
  "error": "第三方凭据无效"
}
```

---

## 适配方案

### 推荐调用流程

新版接口要求"**用户已登录**"才能触发人脸签到，因此推荐以下两段式流程：

```
┌──────────────────────────────────────────────────────────────┐
│  第一步：用户在前端应用完成登录，获取 JWT                      │
│                                                              │
│  POST /SYSU_BME/login                                        │
│  { "email": "user@example.com", "password": "..." }          │
│  → 返回 { "access_token": "eyJ..." }                         │
└──────────────────────────────┬───────────────────────────────┘
                               │ 前端将 JWT 传递给人脸识别模块
┌──────────────────────────────▼───────────────────────────────┐
│  第二步：人脸识别完成后，携带 JWT 回调签到接口                  │
│                                                              │
│  POST /SYSU_BME/face_check                                   │
│  Authorization: Bearer eyJ...                                │
│  { "status": "check_in", "token": "<FACE_SECRET>" }          │
└──────────────────────────────────────────────────────────────┘
```

### 代码适配示例（Python）

```python
import requests

BASE_URL = "https://your-server.com/SYSU_BME"
FACE_SECRET = "your_face_secret_here"

# 第一步：用户登录获取 JWT
login_resp = requests.post(f"{BASE_URL}/login", json={
    "email": "user@example.com",
    "password": "user_password"
})
jwt_token = login_resp.json()["access_token"]

# 第二步：人脸识别成功后，携带 JWT 调用签到接口
check_resp = requests.post(
    f"{BASE_URL}/face_check",
    headers={"Authorization": f"Bearer {jwt_token}"},
    json={
        "status": "check_in",   # 或 "check_out"
        "token": FACE_SECRET
    }
)
print(check_resp.json())
```

### 代码适配示例（JavaScript / fetch）

```javascript
const BASE_URL = "https://your-server.com/SYSU_BME";
const FACE_SECRET = "your_face_secret_here";

// 第一步：登录
const loginRes = await fetch(`${BASE_URL}/login`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email: "user@example.com", password: "..." })
});
const { access_token } = await loginRes.json();

// 第二步：签到
const checkRes = await fetch(`${BASE_URL}/face_check`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${access_token}`
  },
  body: JSON.stringify({ status: "check_in", token: FACE_SECRET })
});
console.log(await checkRes.json());
```

---

## 关于 `email` 字段的废弃说明

旧版接口通过请求体 `email` 字段指定签到用户，新版**完全忽略该字段**。

- 如果您的代码仍然传递 `email`，接口不会报错，但该值不会被使用。
- 实际签到用户由服务端从 `Authorization` 头中的 JWT 解析，**无法被调用方篡改**。

---

## 常见问题

**Q：我们的人脸识别设备是独立硬件，无法持有用户 JWT，怎么办？**

A：硬件设备场景需要走独立的服务账号接入方案（目前规划中）。过渡期间，建议在设备识别出用户身份后，由**前端中间层**代为完成登录→获取 JWT→调用签到的流程，设备只负责上报识别结果。如有定制需求请联系后端团队。

**Q：JWT 有效期多长？是否需要刷新？**

A：当前 JWT 有效期为 1 小时，过期后需重新登录获取新 token。若业务场景要求长期驻留，请联系后端团队申请刷新 token（`refresh_token`）支持。

**Q：`FACE_SECRET` 泄露了怎么办？**

A：请立即联系系统管理员轮换 `FACE_SECRET`。新密钥将通过安全渠道下发，旧密钥即时失效。

---

## 联系方式

如有接入问题，请联系后端维护团队。
