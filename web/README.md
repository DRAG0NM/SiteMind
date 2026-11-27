# B34 指挥工作台

## 本地查看

使用 Node.js 20.19+（20 系列）、22.12+（22 系列）或 24+。在仓库的 `web/` 中执行 `npm ci`、`npm run dev`，打开终端给出的地址。默认使用 `contracts/examples/day.sample.json`；可播放、拖动时间轴和切换倍速。仓库样例只包含早高峰与下午指令；页面在内存中追加明确标记为 synthetic 的第二幕事件和模拟处置，不修改原样例，也不代表真实运行。抽查队列尚未接入。数据性质由事件标注，账本金额只累加播放头之前已结束的分钟，不能当作全天费用。回放边界和初始 08:00 按 `day.timezone` 计算，夏令时日期的范围按实际日长计算。

要读取导出的运行，把通过 `python -m plantkg.world.scenario export-day --out ...` 生成的 day.json 保存为 `runtime/days/<run_id>.json`，在仓库根目录执行 `python -m plantkg --profile mock` 启动后端，然后在页面输入同一运行 ID。要演示确认/批准写路径，改用 `python -m plantkg --profile demo` 启动：它开启存储并填入复核员，启动时把本机运维令牌写到 `runtime/demo/operator.token`，浏览器不保存任何 Key；库里还没有待确认动作时，demo 装配会写入一条标 synthetic 的演示动作（已有待确认动作则不重复写），`python -m plantkg --profile demo actions list` 可直接看到它并用 `... confirm <action_id> --actor dev-reviewer --reason ...` 带令牌确认。文件名和输入框必须使用导出 JSON 顶层的 `run_id`：它是接续链末端的 ID，可能不同于导出命令传入的父运行 ID。`GET /api/v1/demo/day/{run_id}` 校验 DayRecord 和运行 ID；文件不存在时返回 404，不匹配时返回 400 并提示文件中的实际 ID。Vite 开发代理默认将 `/api` 发给仓库默认服务地址 `127.0.0.1:8700`；如自行覆盖后端端口，也须修改 `vite.config.ts` 中的代理。部署时需在同源代理中映射到 plantkg。

`export-day` 已导出专家提案、决策与 Agent 运行轨迹（#182），真实导出可以展示提案与运行记录；`qa` 和知识侧指标尚无生产端（B36/B33 接入），候选方案表与运行记录抽屉由 B49 补齐。专家卡仍可根据事件单显示参与领域与受阻状态；未关联事件单、也没有带时间绑定的提案时显示“无运行记录”。样例包含上述部分记录，其展示效果不能作为真实导出链路已完成的证据。

主 Agent 输入框通过 `POST /api/v1/agent/run` 消费网关的 UIEvent SSE；请求按当前播放头带上 `as_of`，主 Agent 看到的是回放时刻而不是服务端默认时刻。需在服务端启用并配置 Nexent，浏览器不保存其密钥。`dev` 默认关闭 `security.default_ctx`，调用 Agent 时需手动在配置文件中开启它，或启用存储并使用运维令牌；`security` 是受保护配置，不能通过 `--set` 修改。正式身份认证及统一确认界面由后续任务接入。抽查数据源未就绪时显示“尚未接入”，操作工单只读，确认操作由 B35/B47 完成。实时网关事件与 day 回放保存在同一 Zustand store；网关的回答流不会冒充场景事件或修改回放记录。加载或网关失败时页面显示后端错误体里的具体原因（包括文件中的实际 `run_id`），不只显示状态码。

本机开发时把运维令牌写到仓库根目录 `runtime/operator.token`：`npm run dev`（控制台）和 `npm run dev:showcase`（回放页）的 `/api` 开发代理会给转发到 `127.0.0.1:8700` 的请求带上 `X-Plantkg-Operator-Token`，Actor 默认 `u-reviewer`，可用环境变量 `PLANTKG_ACTOR` 覆盖。代理每个请求都重读令牌文件，所以先起 dev 后起后端、或后端重启换了令牌都不用重启 dev；文件不存在时不加这两个请求头，只打一条调试提示。后端配置 `actions.reviewers` 必须包含代理使用的 Actor，否则批准/驳回仍会 403：`--profile demo` 默认填的是 `dev-reviewer`，用 demo 数据时要设 `PLANTKG_ACTOR=dev-reviewer`，或把 `u-reviewer` 加进 `actions.reviewers`。B35 登录接入后删除这段开发代理逻辑。

## 验证

`npm test` 测试样例回放、时区边界（含 Santiago/Havana 午夜夏令时跳变）、网关事件流（含 CRLF 跨块）、按播放头传 `as_of`、错误详情，以及空绑定提案和连续 Enter 提交的回归场景；`npm run build` 检查类型和生产构建。后端冒烟：`python -m pytest tests/test_b34_day_api.py -q`。真实 Nexent 连通需要本地服务及凭据，默认测试不调用外部模型。

## B38 回放展示页（showcase）

给评委的静态回放页，与上面的指挥工作台共用 `web/`、互不依赖。`npm run dev:showcase` 开发预览（打开终端给出的 `/showcase.html`），`npm run build:showcase` 产出单文件 `web/dist-showcase/index.html`，直接拷到任意静态托管；文件内已内联脚本和样式，双击离线也能打开。

静态托管时页面首屏自动读同目录的 `day.json`；没有该文件，或离线用 `file://` 打开（浏览器不允许 fetch 相邻文件）时，回退到 `contracts/examples/day.sample.json` 加一层明确标记为 synthetic 的补全（事件、工单、知识版本、小时时序），页头“数据来源”会区分 `day.json · 日期`、“导入 · 文件名”与“标准演示场景 · 含合成补全”。离线双击打开后，点右上角“导入 day.json”选择 B36 导出的文件即可回放；托管时也可以把 day.json 与 index.html 放同一目录自动加载。day.json 按 `contracts/day.schema.json` 的必填字段与时间格式校验（手写校验器，`runs` 至少一条、时间必须带 `Z` 或 `±hh:mm`、时区须能被浏览器 `Intl` 接受，错误带字段路径），可选小节缺省为空数组。拿到 404/网络失败时回退到标准演示场景并保留“标准演示场景”标识；拿到 5xx 时显示“后端出错（HTTP 5xx）”错误条与“重试”，不替换已加载的数据，也不用样例掩盖后端故障；拿到 200 但内容不合法时显示可读错误条与“导入 day.json / 重试”按钮，不替换当前数据。回放不联网、不调用模型；播放条、标签页与知识版本徽章在所有标签页常驻。

动效取舍（#218-3）：展示页是评委演示，默认强制播放动效，不跟随系统“减弱动效/关闭动画效果”；地址加 `?motion=reduce` 时才真正减弱。控制台没有这个标记，始终跟随系统设置。


开发时在地址后加 `?run_id=<运行 ID>` 进入在线模式：页面先请求 `GET /api/v1/demo/day/{run_id}`，拿到后“数据来源”显示 `后端 · 运行 <run_id>`；证据抽屉的「Agent 运行记录」改从 `GET /api/v1/evidence?run_id=…` 读真实证据链，按运行分组，点某组可只看那一次运行。后端没跑或没有这次运行时回退到 `day.json`／标准演示场景，并在来源旁用一句白话写清原因。`vite.showcase.config.ts` 已把 `/api` 代理到 `127.0.0.1:8700`；用 `file://` 双击打开时浏览器连不上后端，也会给出对应提示。

