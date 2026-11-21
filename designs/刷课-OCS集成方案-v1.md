# 刷课 OCS 集成方案（agent 内代刷 · v1）

> 2026-11-21 · 摸鱼Agent工作台 阶段2 · 决策：OCS 负责播放，bridge/工作台负责编排监控 · 用户已授权 agent 拥有并操作浏览器、登录在 agent 浏览器内完成
> 关联：designs/摸鱼Agent-四版块落地-harness-design-v2.md（U1/U2/K4 边界不变）· lib/course.js bridge 契约（:11437 /tick /progress）

## 一、目标与架构倒置

OCS（ocsjs，MIT）负责超星 DOM 播放（成熟适配，不自己逆向）；bridge 角色收缩为"监控 + 编排 + 门禁"：

```
你: "把这门课刷到 10/10"
        │ U2 授权 token
        ▼
工作台 agent 循环 (course.* 工具 + evaluator 终止裁决)
        │
        ▼
bridge :11437（监控/编排层）
   ├─ /progress  读平台真实进度（oracle，只读）
   ├─ /tick      语义 = 确保播放/推进（OCS 连播时近乎 no-op）
   └─ /status    检测验证卡住/掉线 → awaiting_human
        │ 控制持久化 Chrome profile（channel=chrome，免 chromium 下载）
        ▼
Chrome（登录态 + 油猴 + OCS 脚本）──▶ 超星页面自动连播
```

agent 循环（lib/agent.js）获得课程级工具，浏览器细节藏在工具背后；U1 边界是能力层面强制（注册表不存在改成绩类工具）。

## 二、OCS 集成路线对比

| 路线 | 做法 | 优势 | 劣势 | 结论 |
|---|---|---|---|---|
| **A. 油猴 + OCS 用户脚本** | bridge 启动持久化 Chrome 挂载油猴扩展（--load-extension），一次性交互安装 OCS 脚本 | 单浏览器单 profile，bridge 全控 | 扩展装载需一次手动安装 | ✅ 推荐 |
| B. 脚本猫(ScriptCat) | 同 A，换开源脚本管理器 | 扩展源码开源可自持 | 用户群小 | 备选 |
| C. ocs-desktop 桌面端 | 官方 Electron 启动器 + CDP 附着读状态 | 部署最省事 | 双 app 割裂、bridge 控制力弱 | 兜底 |

## 三、分阶段交付

| 阶段 | 内容 | 验收 | 预估 |
|---|---|---|---|
| P2.1 装载链路 | bridge 挂载油猴扩展；下载 OCS 脚本（pin 版本）；一次性安装流程 | OCS 面板出现在超星课程页 | 半天 |
| P2.2 进度读取 | /progress 读平台真实进度，接 evaluator 三态 | bridge 模式能出 PASS/FAIL | 1~2 天 |
| P2.3 编排语义 | /tick=确保播放/跳过已完成；验证码→awaiting_human；断点续播 | 卡顿/验证码转人工 ✓ | 半天 |
| P2.4 agent 代刷循环 | course.* 工具注册 agent loop；U2 授权；"Agent 代刷"按钮 + 流式轨迹 | mock 全链 → bridge 真机 | 1 天 |
| P2.5 真机验收 | 真实推进一章，evaluator PASS，双入口一致 | 端到端 | 视真机 |

## 四、边界与风险

1. 自动答题默认关（U1/K4）：不配置题库、不开启 OCS AI 答题；如需开启单独授权。
2. 账号风控：验证码一律转人工，不做绕过；自动化推进度可能违反学校/平台规则，由用户知情使用。
3. OCS 版本 pin 当前 release（快照 v5.4.0），升级前核对 release notes。
4. bridge 重启必须先 POST /close 优雅关闭（硬杀丢 Cookie，2026-11-21 实测教训）。
5. 许可：OCS MIT ✓。

## 五、已就绪的基础

- bridge 骨架（tools_python/course_bridge.py）：持久化 profile / 登录管理 /login /login/status /bind /close /debug:goto,pageinfo；/tick /progress 显式 501 待本方案 P2.2/P2.3 实现
- 登录流程已走通两次（Cookie 落盘与恢复的教训：优雅关闭 + 不硬杀）
- 工作台 3081 已可切 MOYU_COURSE_BACKEND=bridge
- 待做：agent 代刷循环（course.* 工具注册进 agent loop）

## 六、用户输入

- 确认路线 A（或 B）
- 一门课程页地址（P2.2 真机核对）
- P2.1 装载时在弹出窗口点两次"安装"（油猴 + OCS 脚本）

## 七、路线 C 实施细则（选定 · 2026-11-22 源码实证）

> 已读 ocsjs/ocs-desktop 源码（GitHub API，main 分支 267 文件，TypeScript monorepo，MIT@package）：

**实证结论**：
1. 每个刷课浏览器 = `ScriptWorker` 实例（packages/app/src/worker/index.ts）：playwright-core `launchPersistentContext`，加载油猴/脚本猫扩展（formatExtensionArguments --load-extension）+ 注入 OCS 脚本（cdn.ocsjs.com/index.js）
2. **附着点**：playwright 启动的 Chromium 在 profile 目录写 `DevToolsActivePort` 文件（CDP 端口）——bridge 监听该目录即可发现并 CDP 附着，无需 ocs-desktop 配合。其内部已用 CDPSession 做 screencast 推流（官方自用，稳定性经过验证）
3. 托管 profile 位置：electron-store 配置 `%APPDATA%/OCS Desktop/config.json` 的 `paths.userDataDirsFolder`（productName=OCS Desktop，appId=ocs.enncy.cn），可直接读取枚举分身
4. 超星自动化在其 `scripts/automation/wk/cx.ts`；本地 express :15319（DEFAULT_SERVER_PORT）带随机内存 token，不对我们开放——**不需要它，走 CDP 只读监控**
5. 许可：app package.json MIT ✓；仓库根无 LICENSE 文件（API 查证）——U3 记录在案，依赖链 MIT

**bridge P2.C 改造**：
- 监听 `userDataDirsFolder` 下各分身目录的 `DevToolsActivePort` → CDP 附着（python 需补 `websockets` 依赖）→ 枚举 targets 匹配 chaoxing 学习页
- `/progress`：Runtime.evaluate 读平台真实进度（选择器待真机一次核对）
- `/status`：验证码签名检测 → captcha:true → 工作台 awaiting_human 转人工
- `/tick`：最小干预原则（重试播放/下一章），不做答题主/不做绕过
- 工作台/agent 循环/evaluator 全部不变（只换 bridge 内核）

**验收顺序（更新）**：
1. 【用户】下载安装 OCS Desktop release → 登录超星 → 开一门课让 OCS 连播
2. 【我】bridge 发现 DevToolsActivePort + CDP 附着 + 枚举页面（纯读）
3. 【我】真机核对进度 DOM → 实现 /progress
4. 【我】evaluator 三态接线 + agent 代刷循环
5. 【双】真机端到端验收

**残余未知**：超星学习页进度 DOM 选择器（真机一次核对）；Electron 主窗口页面与分身浏览器 targets 的区分（按域名过滤，低风险）。
